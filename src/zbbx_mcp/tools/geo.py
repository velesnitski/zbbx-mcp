"""Geo-level VPN monitoring: block detection, traffic trends, availability."""

from __future__ import annotations

import asyncio
import time as _time
from statistics import median

import httpx

from zbbx_mcp.classify import classify_host as _classify_host
from zbbx_mcp.data import (
    build_value_map,
    extract_country,
    fetch_trends_batch,
)
from zbbx_mcp.resolver import InstanceResolver


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()) -> None:

    if "detect_geo_blocks" not in skip:

        @mcp.tool()
        async def detect_geo_blocks(
            period: str = "1d",
            baseline_days: int = 7,
            drop_threshold: float = 50.0,
            country_threshold: float = 50.0,
            min_servers: int = 2,
            instance: str = "",
        ) -> str:
            """Detect country-level VPN blocks by analyzing traffic drops across all servers in each country.

            When >50% of servers in a country show >50% traffic drop vs baseline,
            flags it as a potential geo-block (ISP-level VPN blocking).

            Args:
                period: Current period to analyze (default: 1d)
                baseline_days: Days for baseline comparison (default: 7)
                drop_threshold: % traffic drop per server to flag (default: 50%)
                country_threshold: % of servers in country affected (default: 50%)
                min_servers: Minimum servers in country to analyze (default: 2)
                instance: Zabbix instance name (optional)
            """
            try:
                client = resolver.resolve(instance)

                hosts = await client.call("host.get", {
                    "output": ["hostid", "host"],
                    "selectGroups": ["name"],
                    "selectInterfaces": ["ip"],
                    "filter": {"status": "0"},
                })

                # Group by country
                countries: dict[str, list[dict]] = {}
                for h in hosts:
                    ctry = extract_country(h["host"])
                    if ctry:
                        countries.setdefault(ctry, []).append(h)

                # Filter to countries with enough servers
                countries = {c: hs for c, hs in countries.items() if len(hs) >= min_servers}
                if not countries:
                    return "No countries with enough servers for analysis."

                # Get trends for all hosts
                all_ids = [h["hostid"] for c_hosts in countries.values() for h in c_hosts]
                trend_rows, _ = await fetch_trends_batch(client, all_ids, ["cpu", "traffic"], f"{baseline_days}d")

                # Also get VPN health status
                vpn1_items = await client.call("item.get", {
                    "hostids": all_ids,
                    "output": ["hostid", "lastvalue"],
                    "filter": {"key_": "vpn_primary_check[{HOST.IP}]", "status": "0"},
                })
                vpn1_map = build_value_map(vpn1_items, lambda v: int(float(v)))

                # Build per-host metrics
                host_metrics: dict[str, dict] = {}
                for r in trend_rows:
                    host_metrics.setdefault(r.hostid, {})[r.metric] = r

                # Analyze each country
                blocked = []
                healthy = []

                for ctry, c_hosts in sorted(countries.items()):
                    affected = 0
                    total = len(c_hosts)
                    drops = []
                    vpn_down = 0

                    for h in c_hosts:
                        hm = host_metrics.get(h["hostid"], {})
                        traffic = hm.get("traffic")
                        vpn1 = vpn1_map.get(h["hostid"])

                        if vpn1 == 0:
                            vpn_down += 1

                        if traffic and traffic.avg > 1:
                            drop_pct = ((traffic.avg - traffic.current) / traffic.avg * 100) if traffic.avg > 0 else 0
                            if drop_pct >= drop_threshold:
                                affected += 1
                                drops.append(drop_pct)
                        elif traffic and traffic.avg < 1 and traffic.peak > 10:
                            # Was active, now dead
                            affected += 1
                            drops.append(100.0)

                    pct_affected = (affected / total * 100) if total > 0 else 0

                    if pct_affected >= country_threshold:
                        avg_drop = sum(drops) / len(drops) if drops else 0
                        severity = "CRITICAL" if pct_affected >= 80 else "WARNING"
                        blocked.append({
                            "country": ctry,
                            "total": total,
                            "affected": affected,
                            "pct": pct_affected,
                            "avg_drop": avg_drop,
                            "vpn_down": vpn_down,
                            "severity": severity,
                            "hosts": c_hosts,
                        })
                    else:
                        healthy.append(ctry)

                if not blocked:
                    return f"No geo-blocks detected across {len(countries)} countries ({sum(len(v) for v in countries.values())} servers)."

                parts = [
                    f"**Geo-Block Detection: {len(blocked)} countries affected**\n",
                    "| Country | Servers | Affected | Drop Avg | VPN DOWN | Severity |",
                    "|---------|---------|----------|----------|----------|----------|",
                ]
                for b in sorted(blocked, key=lambda x: -x["pct"]):
                    parts.append(
                        f"| {b['country']} | {b['total']} | "
                        f"{b['affected']}/{b['total']} ({b['pct']:.0f}%) | "
                        f"-{b['avg_drop']:.0f}% | {b['vpn_down']} | {b['severity']} |"
                    )

                # Detail per blocked country
                for b in blocked:
                    parts.append(f"\n### {b['country']} — {b['severity']}")
                    for h in b["hosts"]:
                        hm = host_metrics.get(h["hostid"], {})
                        traffic = hm.get("traffic")
                        vpn1 = vpn1_map.get(h["hostid"])
                        t_now = f"{traffic.current:.1f}" if traffic else "N/A"
                        t_avg = f"{traffic.avg:.1f}" if traffic else "N/A"
                        vpn = "DOWN" if vpn1 == 0 else ("OK" if vpn1 == 1 else "?")
                        parts.append(f"- {h['host']}: {t_now} Mbps (was {t_avg}) | VPN: {vpn}")

                if healthy:
                    parts.append(f"\n**Healthy countries:** {', '.join(sorted(healthy))}")

                return "\n".join(parts)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

    if "get_geo_traffic_trends" not in skip:

        @mcp.tool()
        async def get_geo_traffic_trends(
            period: str = "30d",
            aggregation: str = "daily",
            min_servers: int = 2,
            min_traffic: float = 0.1,
            instance: str = "",
        ) -> str:
            """Per-country traffic trends over time — detect usage growth or decline per region.

            Args:
                period: Time period (default: 30d)
                aggregation: 'summary' or 'daily' (default: daily)
                min_servers: Minimum servers per country (default: 2)
                min_traffic: Minimum avg Gbps to include country (default: 0.1)
                instance: Zabbix instance name (optional)
            """
            try:
                client = resolver.resolve(instance)

                hosts = await client.call("host.get", {
                    "output": ["hostid", "host"],
                    "filter": {"status": "0"},
                })

                countries: dict[str, list[str]] = {}
                host_map: dict[str, str] = {}
                for h in hosts:
                    ctry = extract_country(h["host"])
                    if ctry:
                        countries.setdefault(ctry, []).append(h["hostid"])
                        host_map[h["hostid"]] = h["host"]

                countries = {c: ids for c, ids in countries.items() if len(ids) >= min_servers}
                if not countries:
                    return "No countries with enough servers."

                all_ids = [hid for ids in countries.values() for hid in ids]
                trend_rows, _ = await fetch_trends_batch(client, all_ids, ["traffic"], period)

                # Aggregate by country
                country_data: dict[str, dict] = {}
                for r in trend_rows:
                    for ctry, ids in countries.items():
                        if r.hostid in ids:
                            cd = country_data.setdefault(ctry, {
                                "servers": len(ids), "avg": 0, "current": 0,
                                "trend": "", "daily": {},
                            })
                            cd["avg"] += r.avg
                            cd["current"] += r.current
                            cd["trend"] = r.trend_dir
                            # Sum daily values
                            for day, val in r.daily.items():
                                cd["daily"][day] = cd["daily"].get(day, 0) + val
                            break

                if not country_data:
                    return f"No traffic trend data for {period}."

                # Filter by min_traffic
                all_countries = len(country_data)
                filtered_data = {c: d for c, d in country_data.items() if d["avg"] / 1000 >= min_traffic}
                skipped = all_countries - len(filtered_data)

                parts = [
                    f"**Geo Traffic Trends ({period}): {len(filtered_data)} countries**\n",
                    "| Country | Servers | Traffic Avg | Traffic Now | Trend | Change |",
                    "|---------|---------|-------------|-------------|-------|--------|",
                ]

                for ctry in sorted(filtered_data, key=lambda c: -filtered_data[c]["avg"]):
                    cd = filtered_data[ctry]
                    avg_gbps = cd["avg"] / 1000
                    now_gbps = cd["current"] / 1000
                    change = ((cd["current"] - cd["avg"]) / cd["avg"] * 100) if cd["avg"] > 0 else 0
                    trend = "dead" if cd["current"] < 1 and cd["avg"] > 10 else cd["trend"]
                    parts.append(
                        f"| {ctry} | {cd['servers']} | {avg_gbps:.1f} Gbps | "
                        f"{now_gbps:.1f} Gbps | {trend} | {change:+.0f}% |"
                    )

                if skipped:
                    parts.append(f"\n*{skipped} countries with <{min_traffic} Gbps omitted*")

                if aggregation == "daily" and country_data:
                    # Show daily for top 5 countries
                    parts.append("\n### Daily Breakdown (top countries)\n")
                    top = sorted(country_data.items(), key=lambda x: -x[1]["avg"])[:5]
                    all_days = sorted(set(
                        d for _, cd in top for d in cd["daily"]
                    ))
                    if all_days:
                        day_cols = " | ".join(all_days)
                        parts.append(f"| Country | {day_cols} |")
                        parts.append(f"|---------|{'---|' * len(all_days)}")
                        for ctry, cd in top:
                            vals = " | ".join(
                                f"{cd['daily'].get(d, 0)/1000:.1f}" for d in all_days
                            )
                            parts.append(f"| {ctry} (Gbps) | {vals} |")

                return "\n".join(parts)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

    if "get_server_availability_report" not in skip:

        @mcp.tool()
        async def get_server_availability_report(
            country: str = "",
            product: str = "",
            exclude_product: str = "infrastructure,monitoring",
            only_problems: bool = True,
            max_results: int = 50,
            period: str = "30d",
            instance: str = "",
        ) -> str:
            """VPN protocol availability per server — uptime % per protocol.

            Uses Zabbix trend data to calculate hours UP vs DOWN for each protocol.
            By default shows only DOWN/DEGRADED servers and excludes monitoring hosts.

            Args:
                country: Filter by country code (optional)
                product: Filter by product name (optional)
                exclude_product: Comma-separated products to exclude (default: infrastructure,monitoring)
                only_problems: Show only DOWN/DEGRADED servers (default: True)
                max_results: Maximum servers to show (default: 50)
                period: Analysis period (default: 30d)
                instance: Zabbix instance name (optional)
            """
            try:
                client = resolver.resolve(instance)

                hosts = await client.call("host.get", {
                    "output": ["hostid", "host"],
                    "selectGroups": ["name"],
                    "filter": {"status": "0"},
                })

                exclude_set = {p.strip().lower() for p in exclude_product.split(",") if p.strip()} if exclude_product else set()
                filtered = []
                for h in hosts:
                    prod, _ = _classify_host(h.get("groups", []))
                    if product and product.lower() not in (prod or "").lower():
                        continue
                    if exclude_set and any(p in (prod or "").lower() for p in exclude_set):
                        continue
                    if country and extract_country(h["host"]).lower() != country.lower():
                        continue
                    filtered.append(h)

                if not filtered:
                    return "No servers match the filters."

                hostids = [h["hostid"] for h in filtered]

                # Fetch trend data for VPN check items
                now = int(_time.time())
                from zbbx_mcp.data import _parse_period
                time_from = now - _parse_period(period)

                # Get items for VPN protocol checks
                vpn_items = await client.call("item.get", {
                    "hostids": hostids,
                    "output": ["itemid", "hostid", "key_"],
                    "filter": {"key_": [
                        "vpn_primary_check[{HOST.IP}]",
                        "vpn_secondary_check[{HOST.IP}]",
                    ], "status": "0"},
                })

                if not vpn_items:
                    return "No VPN check items found."

                # Fetch trends for VPN items
                item_ids = [i["itemid"] for i in vpn_items]
                trends = await client.call("trend.get", {
                    "itemids": item_ids,
                    "time_from": time_from,
                    "output": ["itemid", "value_avg", "num"],
                    "limit": len(item_ids) * 24 * 31,
                })

                # Map itemid -> (hostid, protocol)
                item_info: dict[str, tuple[str, str]] = {}
                for i in vpn_items:
                    proto = "vpn1" if "vpn1" in i["key_"] else "vpn2"
                    item_info[i["itemid"]] = (i["hostid"], proto)

                # Calculate uptime per host per protocol
                host_uptime: dict[str, dict[str, dict]] = {}  # hostid -> proto -> {up, total}
                for t in trends:
                    info = item_info.get(t["itemid"])
                    if not info:
                        continue
                    hid, proto = info
                    entry = host_uptime.setdefault(hid, {}).setdefault(proto, {"up": 0, "total": 0})
                    entry["total"] += 1
                    try:
                        if float(t["value_avg"]) >= 0.5:
                            entry["up"] += 1
                    except (ValueError, TypeError):
                        pass

                host_map = {h["hostid"]: h["host"] for h in filtered}

                rows = []
                for hid in hostids:
                    hostname = host_map.get(hid, "?")
                    ctry = extract_country(hostname)
                    hu = host_uptime.get(hid, {})

                    vpn1 = hu.get("vpn1", {"up": 0, "total": 0})
                    vpn2 = hu.get("vpn2", {"up": 0, "total": 0})

                    vpn1_pct = (vpn1["up"] / vpn1["total"] * 100) if vpn1["total"] > 0 else None
                    vpn2_pct = (vpn2["up"] / vpn2["total"] * 100) if vpn2["total"] > 0 else None

                    overall = "HEALTHY"
                    if vpn1_pct is not None and vpn1_pct < 50:
                        overall = "DOWN"
                    elif vpn1_pct is not None and vpn1_pct < 90:
                        overall = "DEGRADED"

                    rows.append({
                        "host": hostname, "country": ctry,
                        "vpn1": vpn1_pct, "vpn2": vpn2_pct,
                        "overall": overall, "hours": vpn1["total"],
                    })

                rows.sort(key=lambda r: (r["vpn1"] or 100))

                # Filter and limit
                total_all = len(rows)
                healthy_count = sum(1 for r in rows if r["overall"] == "HEALTHY")
                if only_problems:
                    rows = [r for r in rows if r["overall"] != "HEALTHY"]
                shown = rows[:max_results]
                omitted = len(rows) - len(shown)

                parts = [
                    f"**VPN Availability ({period}): {total_all} servers ({healthy_count} healthy)**\n",
                    "| Server | Country | VPN Primary | VPN Secondary | Status |",
                    "|--------|---------|-------------|-------------|--------|",
                ]
                for r in shown:
                    x = f"{r['vpn1']:.1f}%" if r["vpn1"] is not None else "N/A"
                    k = f"{r['vpn2']:.1f}%" if r["vpn2"] is not None else "N/A"
                    parts.append(f"| {r['host']} | {r['country']} | {x} | {k} | {r['overall']} |")

                if omitted:
                    parts.append(f"\n*{omitted} more servers omitted*")

                # Country summary
                country_stats: dict[str, list] = {}
                for r in rows:
                    if r["country"]:
                        country_stats.setdefault(r["country"], []).append(r)

                if country_stats:
                    parts.append("\n### Country Summary\n")
                    parts.append("| Country | Servers | Avg VPN Uptime | DOWN |")
                    parts.append("|---------|---------|-----------------|------|")
                    for ctry in sorted(country_stats):
                        cs = country_stats[ctry]
                        vpn1_vals = [r["vpn1"] for r in cs if r["vpn1"] is not None]
                        avg_x = f"{median(vpn1_vals):.1f}%" if vpn1_vals else "N/A"
                        down = sum(1 for r in cs if r["overall"] == "DOWN")
                        parts.append(f"| {ctry} | {len(cs)} | {avg_x} | {down} |")

                return "\n".join(parts)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

    if "get_protocol_failure_matrix" not in skip:

        @mcp.tool()
        async def get_protocol_failure_matrix(
            min_servers: int = 2,
            instance: str = "",
        ) -> str:
            """Show which VPN protocol works in which country.

            Aggregates VPN protocol check status per country.
            Helps determine: "user in country X should use protocol Y."

            Args:
                min_servers: Minimum servers per country to include (default: 2)
                instance: Zabbix instance name (optional)
            """
            try:
                client = resolver.resolve(instance)

                hosts = await client.call("host.get", {
                    "output": ["hostid", "host"],
                    "filter": {"status": "0"},
                })

                countries: dict[str, list[dict]] = {}
                for h in hosts:
                    ctry = extract_country(h["host"])
                    if ctry:
                        countries.setdefault(ctry, []).append(h)
                countries = {c: hs for c, hs in countries.items() if len(hs) >= min_servers}

                if not countries:
                    return "No countries with enough servers."

                all_ids = [h["hostid"] for hs in countries.values() for h in hs]

                vpn1_items, vpn2_items, vpn3_items = await asyncio.gather(
                    client.call("item.get", {
                        "hostids": all_ids, "output": ["hostid", "lastvalue"],
                        "filter": {"key_": "vpn_primary_check[{HOST.IP}]", "status": "0"},
                    }),
                    client.call("item.get", {
                        "hostids": all_ids, "output": ["hostid", "lastvalue"],
                        "filter": {"key_": "vpn_secondary_check[{HOST.IP}]", "status": "0"},
                    }),
                    client.call("item.get", {
                        "hostids": all_ids, "output": ["hostid", "lastvalue"],
                        "search": {"key_": "vpn3"}, "searchWildcardsEnabled": True,
                        "filter": {"status": "0"},
                    }),
                )

                vpn1_map = build_value_map(vpn1_items, lambda v: int(float(v)))
                vpn2_map = build_value_map(vpn2_items, lambda v: int(float(v)))
                vpn3_map: dict[str, int] = {}
                for i in vpn3_items:
                    try:
                        val = int(float(i["lastvalue"]))
                        hid = i["hostid"]
                        if val > vpn3_map.get(hid, 0):
                            vpn3_map[hid] = val
                    except (ValueError, TypeError, KeyError):
                        pass

                parts = [
                    "**Protocol Failure Matrix**\n",
                    "| Country | Servers | Proto 1 | Proto 2 | Proto 3 | Recommendation |",
                    "|---------|---------|------|-------|---------|----------------|",
                ]

                for ctry in sorted(countries):
                    hs = countries[ctry]
                    hids = [h["hostid"] for h in hs]
                    total = len(hids)

                    vpn1_up = sum(1 for hid in hids if vpn1_map.get(hid) == 1)
                    vpn2_up = sum(1 for hid in hids if vpn2_map.get(hid) == 1)
                    vpn3_up = sum(1 for hid in hids if vpn3_map.get(hid, 0) >= 1)

                    vpn_primary_checked = sum(1 for hid in hids if hid in vpn1_map)
                    vpn_secondary_checked = sum(1 for hid in hids if hid in vpn2_map)
                    vpn3_checked = sum(1 for hid in hids if hid in vpn3_map)

                    def _status(up: int, checked: int) -> str:
                        if checked == 0:
                            return "N/A"
                        pct = up / checked * 100
                        if pct >= 80:
                            return f"OK ({up}/{checked})"
                        if pct >= 30:
                            return f"PARTIAL ({up}/{checked})"
                        return f"DOWN ({up}/{checked})"

                    x_s = _status(vpn1_up, vpn_primary_checked)
                    k_s = _status(vpn2_up, vpn_secondary_checked)
                    o_s = _status(vpn3_up, vpn3_checked)

                    # Recommendation
                    working = []
                    if "OK" in x_s or "PARTIAL" in x_s:
                        working.append("Proto 1")
                    if "OK" in k_s or "PARTIAL" in k_s:
                        working.append("Proto 2")
                    if "OK" in o_s or "PARTIAL" in o_s:
                        working.append("Proto 3")

                    if not working:
                        rec = "ALL BLOCKED"
                    elif len(working) == 3:
                        rec = "All protocols OK"
                    else:
                        rec = " / ".join(working) + " only"

                    parts.append(f"| {ctry} | {total} | {x_s} | {k_s} | {o_s} | {rec} |")

                return "\n".join(parts)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

    if "get_block_timeline" not in skip:

        @mcp.tool()
        async def get_block_timeline(
            period: str = "30d",
            min_servers: int = 2,
            instance: str = "",
        ) -> str:
            """Show when VPN blocks started per country, using daily trend data.

            Finds the day traffic dropped to near-zero for each blocked country.

            Args:
                period: How far back to look (default: 30d)
                min_servers: Minimum servers per country (default: 2)
                instance: Zabbix instance name (optional)
            """
            try:

                client = resolver.resolve(instance)

                hosts = await client.call("host.get", {
                    "output": ["hostid", "host"],
                    "filter": {"status": "0"},
                })

                countries: dict[str, list[str]] = {}
                for h in hosts:
                    ctry = extract_country(h["host"])
                    if ctry:
                        countries.setdefault(ctry, []).append(h["hostid"])
                countries = {c: ids for c, ids in countries.items() if len(ids) >= min_servers}

                if not countries:
                    return "No countries with enough servers."

                all_ids = [hid for ids in countries.values() for hid in ids]
                trend_rows, _ = await fetch_trends_batch(client, all_ids, ["traffic"], period)

                # Aggregate daily traffic per country
                country_daily: dict[str, dict[str, float]] = {}
                for r in trend_rows:
                    for ctry, ids in countries.items():
                        if r.hostid in ids:
                            cd = country_daily.setdefault(ctry, {})
                            for day, val in r.daily.items():
                                cd[day] = cd.get(day, 0) + val
                            break

                # Find block start date for each country
                blocks = []
                for ctry, daily in country_daily.items():
                    days = sorted(daily)
                    if not days:
                        continue

                    vals = [daily[d] for d in days]
                    peak = max(vals) if vals else 0
                    current = vals[-1] if vals else 0

                    if peak < 10 or current > peak * 0.3:
                        continue  # Not blocked

                    # Find the day traffic dropped
                    block_day = None
                    for i, d in enumerate(days):
                        if daily[d] < peak * 0.2 and (i == 0 or daily[days[i-1]] >= peak * 0.2):
                            block_day = d
                            break

                    if block_day:
                        duration_days = len(days) - days.index(block_day)
                        blocks.append({
                            "country": ctry,
                            "servers": len(countries[ctry]),
                            "block_start": block_day,
                            "duration": duration_days,
                            "pre_block_gbps": round(peak / 1000, 2),
                            "current_gbps": round(current / 1000, 2),
                        })

                if not blocks:
                    return f"No VPN blocks detected in {period} across {len(countries)} countries."

                blocks.sort(key=lambda b: -b["duration"])

                parts = [
                    f"**Block Timeline ({period})**\n",
                    "| Country | Servers | Block Started | Duration | Pre-block Traffic | Current |",
                    "|---------|---------|--------------|----------|-------------------|---------|",
                ]
                for b in blocks:
                    parts.append(
                        f"| {b['country']} | {b['servers']} | {b['block_start']} | "
                        f"{b['duration']}d | {b['pre_block_gbps']} Gbps | {b['current_gbps']} Gbps |"
                    )

                return "\n".join(parts)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"
