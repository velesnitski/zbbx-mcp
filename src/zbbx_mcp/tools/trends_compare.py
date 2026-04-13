"""Trend tools: batch metrics, per-server dashboard, side-by-side comparison."""

import httpx

from zbbx_mcp.classify import classify_host as _classify_host
from zbbx_mcp.classify import detect_provider
from zbbx_mcp.data import (
    extract_country,
    fetch_trends_batch,
    host_ip,
)
from zbbx_mcp.resolver import InstanceResolver
from zbbx_mcp.utils import resolve_group_ids


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()):

    if "get_trends_batch" not in skip:

        @mcp.tool()
        async def get_trends_batch(
            country: str = "",
            product: str = "",
            tier: str = "",
            group: str = "",
            metrics: str = "cpu,traffic,load",
            period: str = "7d",
            aggregation: str = "summary",
            max_results: int = 50,
            instance: str = "",
        ) -> str:
            """Get trend data (avg/peak/min) for multiple servers and metrics.

            Args:
                country: Country code filter (optional)
                product: Filter by product (optional)
                tier: Filter by tier (optional)
                group: Zabbix host group (optional)
                metrics: Comma-separated: cpu, traffic, load, memory (default: cpu,traffic,load)
                period: 1d, 7d, or 30d (default: 7d)
                aggregation: 'summary' or 'daily' (default: summary)
                max_results: Max servers (default: 50)
                instance: Zabbix instance (optional)
            """
            try:
                client = resolver.resolve(instance)

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

                hosts = await client.call("host.get", params)

                filtered_ids = []
                for h in hosts:
                    prod, t = _classify_host(h.get("groups", []))
                    if product and product.lower() not in (prod or "").lower():
                        continue
                    if tier and tier.lower() not in (t or "").lower():
                        continue
                    if country and extract_country(h.get("host", "")).lower() != country.lower():
                        continue
                    filtered_ids.append(h["hostid"])
                    if len(filtered_ids) >= max_results:
                        break

                if not filtered_ids:
                    return "No servers match the filters."

                metric_list = [m.strip() for m in metrics.split(",") if m.strip()]
                trend_rows, host_map = await fetch_trends_batch(
                    client, filtered_ids, metric_list, period,
                )

                if not trend_rows:
                    return f"No trend data for the last {period}."

                units = {"cpu": "%", "traffic": "Mbps", "load": "", "memory": "GB"}
                server_count = len(set(r.hostid for r in trend_rows))

                if aggregation == "daily":
                    # Collect all unique days across all rows
                    all_days = sorted(set(
                        d for r in trend_rows for d in r.daily
                    ))
                    if not all_days:
                        return "No daily data available."

                    day_cols = " | ".join(all_days)
                    parts = [
                        f"**Daily Trends ({period}) for {server_count} servers**\n",
                        f"| Server | Metric | {day_cols} |",
                        f"|--------|--------|{'---|' * len(all_days)}",
                    ]
                    for r in trend_rows:
                        u = units.get(r.metric, "")
                        vals = " | ".join(
                            f"{r.daily.get(d, '')} {u}".strip() if d in r.daily else ""
                            for d in all_days
                        )
                        parts.append(f"| {r.hostname} | {r.metric} | {vals} |")

                    return "\n".join(parts)
                else:
                    parts = [
                        f"**Trends ({period}) for {server_count} servers**\n",
                        "| Server | Metric | Avg | Peak | Min | Current | Trend |",
                        "|--------|--------|-----|------|-----|---------|-------|",
                    ]
                    for r in trend_rows:
                        u = units.get(r.metric, "")
                        parts.append(
                            f"| {r.hostname} | {r.metric} | "
                            f"{r.avg} {u} | {r.peak} {u} | {r.min_val} {u} | "
                            f"{r.current} {u} | {r.trend_dir} |"
                        )

                    return "\n".join(parts)
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
                        f"min {r.min_val} {u} | current {r.current} {u} | "
                        f"trend: {r.trend_dir}"
                    )

                # Daily breakdown table
                if aggregation == "daily":
                    all_days = sorted(set(d for r in trend_rows for d in r.daily))
                    if all_days:
                        parts.append("\n## Daily Breakdown\n")
                        day_cols = " | ".join(all_days)
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
                hosts: Comma-separated hostnames (e.g., 'srv-nl01,srv-de01')
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
                    vals_now = [f"{data[n].current} {u}" if n in data else "N/A" for n in server_names]
                    parts.append(f"| {mn} avg | {' | '.join(vals_avg)} |")
                    parts.append(f"| {mn} peak | {' | '.join(vals_peak)} |")
                    parts.append(f"| {mn} now | {' | '.join(vals_now)} |")

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
