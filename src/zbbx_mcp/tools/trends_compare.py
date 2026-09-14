"""Trend tools: batch metrics, per-server dashboard, side-by-side comparison."""

from collections.abc import Callable
from itertools import groupby

import httpx

from zbbx_mcp.budget import (
    all_names_unknown_message,
    effective_cap,
    fit_host_blocks,
    missing_hosts_note,
    no_match_message,
    parse_host_list,
    render_budget,
)
from zbbx_mcp.classify import classify_host as _classify_host
from zbbx_mcp.classify import detect_provider
from zbbx_mcp.data import (
    TrendRow,
    day_label,
    extract_country,
    fetch_trends_batch,
    host_ip,
    label_matches,
)
from zbbx_mcp.resolver import InstanceResolver
from zbbx_mcp.utils import resolve_group_ids

_UNITS = {"cpu": "%", "traffic": "Mbps", "traffic_out": "Mbps", "load": "", "memory": "GB"}
_COMPACT_COLUMNS = "host|metric|avg|peak|min|now|trend"


def _units_line(metrics: list[str]) -> str:
    """One line stating the unit of every requested metric (compact format)."""
    parts = []
    for m in metrics:
        u = _UNITS.get(m, "")
        parts.append(f"{m}={u}" if u else f"{m}=ratio")
    return "units: " + ", ".join(parts)


def _host_blocks(
    rows: list[TrendRow],
    line: Callable[[TrendRow], str],
) -> list[tuple[str, str]]:
    """``[(host, all of that host's rendered lines)]`` in row order.

    Rows arrive sorted by (host, metric); one block per host is what lets the
    budget fitter show a host with every metric or not at all.
    """
    blocks: list[tuple[str, str]] = []
    for host, group in groupby(rows, key=lambda r: r.hostname):
        blocks.append((host, "\n".join(line(r) for r in group)))
    return blocks


def _summary_table_line(r: TrendRow) -> str:
    u = _UNITS.get(r.metric, "")
    return (
        f"| {r.hostname} | {r.metric} | "
        f"{r.avg} {u} | {r.peak} {u} | {r.min_val} {u} | "
        f"{r.current_text(u)} | {r.trend_dir} |"
    )


def _summary_compact_line(r: TrendRow) -> str:
    now = "n/a" if r.current is None else f"{r.current}"
    return f"{r.hostname}|{r.metric}|{r.avg}|{r.peak}|{r.min_val}|{now}|{r.trend_dir or 'n/a'}"


def _daily_table_line(r: TrendRow, days: list[str]) -> str:
    u = _UNITS.get(r.metric, "")
    vals = " | ".join(
        f"{r.daily.get(d, '')} {u}".strip() if d in r.daily else ""
        for d in days
    )
    return f"| {r.hostname} | {r.metric} | {vals} |"


def _daily_compact_line(r: TrendRow, days: list[str]) -> str:
    vals = "|".join(f"{r.daily[d]}" if d in r.daily else "" for d in days)
    return f"{r.hostname}|{r.metric}|{vals}"


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()):

    if "get_trends_batch" not in skip:

        @mcp.tool()
        async def get_trends_batch(
            country: str = "",
            product: str = "",
            tier: str = "",
            group: str = "",
            hosts: str = "",
            metrics: str = "cpu,traffic,load",
            period: str = "7d",
            aggregation: str = "summary",
            format: str = "table",
            max_results: int = 50,
            instance: str = "",
        ) -> str:
            """Get trend data (avg/peak/min) for multiple servers and metrics.

            The output is cut to the response budget by the tool itself, whole
            hosts at a time, and the last line names the hosts left out and
            the exact hosts= call that fetches them (ADR 142).

            Args:
                country: Country code filter (optional)
                product: Filter by product (optional)
                tier: Filter by tier (optional)
                group: Zabbix host group (optional)
                hosts: Comma-separated exact host names. An explicit list is
                    never cut by max_results, and names Zabbix does not know
                    are reported, not dropped (optional)
                metrics: Comma-separated: cpu, traffic, load, memory (default: cpu,traffic,load)
                period: 1d, 7d, or 30d (default: 7d)
                aggregation: 'summary' or 'daily' (default: summary)
                format: 'table' (markdown, default) or 'compact' — one
                    host|metric|avg|peak|min|now|trend line per host and
                    metric, units stated once in the header
                max_results: Max servers (default: 50)
                instance: Zabbix instance (optional)
            """
            try:
                client = resolver.resolve(instance)
                if format not in ("table", "compact"):
                    return f"Unknown format '{format}'. Use 'table' or 'compact'."

                # Get and filter hosts
                params = {
                    "output": ["hostid", "host"],
                    "selectGroups": ["name"],
                    "filter": {"status": "0"},
                    "sortfield": "host",
                }
                if group:
                    gids = await resolve_group_ids(client, group)
                    if gids is None:
                        return f"Host group '{group}' not found."
                    params["groupids"] = gids
                wanted = parse_host_list(hosts)
                if wanted:
                    params["filter"]["host"] = wanted

                found_hosts = await client.call("host.get", params)

                # A caller who names the hosts has already chosen the set; a cap
                # that trimmed it would drop names silently, which is the bug the
                # parameter exists to avoid.
                cap = effective_cap(max_results, wanted)
                filtered_ids = []
                for h in found_hosts:
                    prod, t = _classify_host(h.get("groups", []))
                    if not label_matches(prod, product):
                        continue
                    if not label_matches(t, tier):
                        continue
                    if country and extract_country(h.get("host", "")).lower() != country.lower():
                        continue
                    filtered_ids.append(h["hostid"])
                    if len(filtered_ids) >= cap:
                        break

                missing, note = missing_hosts_note(
                    wanted, (h.get("host", "") for h in found_hosts),
                )

                if not filtered_ids:
                    if wanted and len(missing) == len(wanted):
                        return all_names_unknown_message(note)
                    return note + no_match_message(found_hosts)

                metric_list = [m.strip() for m in metrics.split(",") if m.strip()]
                trend_rows, host_map = await fetch_trends_batch(
                    client, filtered_ids, metric_list, period,
                )

                if not trend_rows:
                    return f"No trend data for the last {period}."

                server_count = len(set(r.hostid for r in trend_rows))
                compact = format == "compact"
                budget = render_budget()

                if aggregation == "daily":
                    # Collect all unique days across all rows
                    all_days = sorted(set(
                        d for r in trend_rows for d in r.daily
                    ))
                    if not all_days:
                        return "No daily data available."
                    title = f"**Daily Trends ({period}) for {server_count} servers**\n" + note
                    if compact:
                        day_cols = "|".join(day_label(d) for d in all_days)
                        header = "\n".join([
                            title,
                            f"host|metric|{day_cols} ({_units_line(metric_list)})",
                        ])
                        blocks = _host_blocks(
                            trend_rows, lambda r: _daily_compact_line(r, all_days),
                        )
                    else:
                        day_cols = " | ".join(day_label(d) for d in all_days)
                        header = "\n".join([
                            title,
                            f"| Server | Metric | {day_cols} |",
                            f"|--------|--------|{'---|' * len(all_days)}",
                        ])
                        blocks = _host_blocks(
                            trend_rows, lambda r: _daily_table_line(r, all_days),
                        )
                else:
                    title = f"**Trends ({period}) for {server_count} servers**\n" + note
                    if compact:
                        header = "\n".join([
                            title,
                            f"{_COMPACT_COLUMNS} ({_units_line(metric_list)})",
                        ])
                        blocks = _host_blocks(trend_rows, _summary_compact_line)
                    else:
                        header = "\n".join([
                            title,
                            "| Server | Metric | Avg | Peak | Min | Current | Trend |",
                            "|--------|--------|-----|------|-----|---------|-------|",
                        ])
                        blocks = _host_blocks(trend_rows, _summary_table_line)

                # The cut is the tool's own, so it can be stated: whole hosts,
                # in order, and a closing line that names the rest (ADR 142).
                text, _omitted = fit_host_blocks(header, blocks, budget)
                return text
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

    if "get_server_dashboard" not in skip:

        @mcp.tool()
        async def get_server_dashboard(
            host: str,
            period: str = "7d",
            aggregation: str = "daily",
            instance: str = "",
        ) -> str:
            """Get a per-server dashboard showing metric trends.

            Args:
                host: Hostname or host ID
                period: Time period: 1d, 7d, 30d (default: 7d)
                aggregation: 'daily' (default) for per-day table, 'summary' for period totals
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)

                # Resolve hostname
                if not host.isdigit():
                    lookup = await client.call("host.get", {
                        "output": ["hostid", "host"],
                        "filter": {"host": [host]},
                    })
                    if not lookup:
                        lookup = await client.call("host.get", {
                            "output": ["hostid", "host"],
                            "search": {"host": host, "name": host},
                            "searchByAny": True, "searchWildcardsEnabled": True,
                            "limit": 1,
                        })
                    if not lookup:
                        return f"Host '{host}' not found."
                    hostid = lookup[0]["hostid"]
                    hostname = lookup[0]["host"]
                else:
                    hostid = host
                    h = await client.call("host.get", {
                        "hostids": [hostid], "output": ["host"],
                    })
                    hostname = h[0]["host"] if h else hostid

                trend_rows, _ = await fetch_trends_batch(
                    client, [hostid], ["cpu", "traffic", "load", "memory"], period,
                )

                if not trend_rows:
                    return f"No trend data for '{hostname}' in the last {period}."

                units = {"cpu": "%", "traffic": "Mbps", "traffic_out": "Mbps", "load": "", "memory": "GB"}

                parts = [f"# Dashboard: {hostname} (last {period})\n"]

                # Summary line per metric
                for r in trend_rows:
                    u = units.get(r.metric, "")
                    parts.append(
                        f"**{r.metric.title()}:** "
                        f"avg {r.avg} {u} | peak {r.peak} {u} | "
                        f"min {r.min_val} {u} | current {r.current_text(u)} | "
                        f"trend: {r.trend_dir}"
                    )

                # Daily breakdown table
                if aggregation == "daily":
                    all_days = sorted(set(d for r in trend_rows for d in r.daily))
                    if all_days:
                        parts.append("\n## Daily Breakdown\n")
                        day_cols = " | ".join(day_label(d) for d in all_days)
                        parts.append(f"| Metric | {day_cols} |")
                        parts.append(f"|--------|{'---|' * len(all_days)}")
                        for r in trend_rows:
                            u = units.get(r.metric, "")
                            vals = " | ".join(
                                f"{r.daily.get(d, '')}" for d in all_days
                            )
                            parts.append(f"| {r.metric} ({u}) | {vals} |")

                return "\n".join(parts)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

    if "compare_servers" not in skip:

        @mcp.tool()
        async def compare_servers(
            hosts: str,
            metrics: str = "cpu,traffic,load",
            period: str = "7d",
            instance: str = "",
        ) -> str:
            """Compare multiple servers side-by-side with trend data.

            Args:
                hosts: Comma-separated hostnames (e.g., 'srv-tf9001,srv-bv9001')
                metrics: Comma-separated: cpu, traffic, traffic_out, load, memory
                period: Time period: 1d, 7d, 30d (default: 7d)
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                from typing import Any
                client = resolver.resolve(instance)
                host_names = [h.strip() for h in hosts.split(",") if h.strip()]

                if len(host_names) < 2:
                    return "Need at least 2 hostnames to compare."

                lookup = await client.call("host.get", {
                    "output": ["hostid", "host"],
                    "selectInterfaces": ["ip"],
                    "filter": {"host": host_names},
                })
                if len(lookup) < 2:
                    return f"Found only {len(lookup)} hosts. Need at least 2."

                hostids = [h["hostid"] for h in lookup]
                metric_list = [m.strip() for m in metrics.split(",") if m.strip()]
                trend_rows, _ = await fetch_trends_batch(client, hostids, metric_list, period)

                if not trend_rows:
                    return f"No trend data for the last {period}."

                by_metric: dict[str, dict[str, Any]] = {}
                for r in trend_rows:
                    by_metric.setdefault(r.metric, {})[r.hostname] = r

                units = {"cpu": "%", "traffic": "Mbps", "traffic_out": "Mbps", "load": "", "memory": "GB"}
                server_names = [h["host"] for h in lookup]
                cols = " | ".join(server_names)

                parts = [
                    f"**Server Comparison ({period})**\n",
                    f"| Metric | {cols} |",
                    f"|--------|{'---|' * len(server_names)}",
                ]
                for mn in metric_list:
                    if mn not in by_metric:
                        continue
                    u = units.get(mn, "")
                    data = by_metric[mn]
                    vals_avg = [f"{data[n].avg} {u}" if n in data else "N/A" for n in server_names]
                    vals_peak = [f"{data[n].peak} {u}" if n in data else "N/A" for n in server_names]
                    vals_now = [data[n].current_text(u) if n in data else "N/A" for n in server_names]
                    parts.append(f"| {mn} avg | {' | '.join(vals_avg)} |")
                    parts.append(f"| {mn} peak | {' | '.join(vals_peak)} |")
                    parts.append(f"| {mn} now | {' | '.join(vals_now)} |")
                    vals_min = [f"{data[n].min_val} {u}" if n in data else "N/A" for n in server_names]
                    vals_trend = [data[n].trend_dir or "n/a" if n in data else "N/A" for n in server_names]
                    parts.append(f"| {mn} min | {' | '.join(vals_min)} |")
                    parts.append(f"| {mn} trend | {' | '.join(vals_trend)} |")

                # Efficiency metrics
                cpu_data = by_metric.get("cpu", {})
                traffic_data = by_metric.get("traffic", {})
                if cpu_data and traffic_data:
                    parts.append(f"\n| Efficiency | {cols} |")
                    parts.append(f"|------------|{'---|' * len(server_names)}")
                    # CPU per 100 Mbps
                    eff_vals = []
                    for n in server_names:
                        cpu_avg = cpu_data[n].avg if n in cpu_data else 0
                        traffic_avg = traffic_data[n].avg if n in traffic_data else 0
                        if traffic_avg > 0:
                            eff_vals.append(f"{cpu_avg / (traffic_avg / 100):.1f}%")
                        else:
                            eff_vals.append("N/A")
                    parts.append(f"| CPU per 100 Mbps | {' | '.join(eff_vals)} |")
                    # Traffic headroom
                    headroom_vals = []
                    for n in server_names:
                        if n in traffic_data:
                            headroom = 800 - traffic_data[n].peak  # vs BW_MAX
                            headroom_vals.append(f"{headroom:.0f} Mbps")
                        else:
                            headroom_vals.append("N/A")
                    parts.append(f"| BW headroom (vs 800) | {' | '.join(headroom_vals)} |")

                # Provider/country info
                parts.append(f"\n| Info | {cols} |")
                parts.append(f"|------|{'---|' * len(server_names)}")
                provs = []
                for h in lookup:
                    ip = host_ip(h)
                    provs.append(detect_provider(ip) if ip else "?")
                parts.append(f"| Provider | {' | '.join(provs)} |")
                parts.append(f"| Country | {' | '.join(extract_country(h['host']) for h in lookup)} |")

                return "\n".join(parts)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error comparing servers: {e}"
