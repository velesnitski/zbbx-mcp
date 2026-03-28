"""Geo-level monitoring: traffic analysis, traffic trends, availability."""

from __future__ import annotations

import asyncio
import time as _time
from statistics import median

import httpx

from zbbx_mcp.classify import classify_host as _classify_host
from zbbx_mcp.classify import resolve_datacenter
from zbbx_mcp.data import (
    build_value_map,
    countries_for_region,
    extract_country,
    fetch_cpu_map,
    fetch_enabled_hosts,
    fetch_traffic_map,
    fetch_trends_batch,
    group_by_country,
    host_ip,
)
from zbbx_mcp.resolver import InstanceResolver


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()) -> None:

    if "detect_regional_anomalies" not in skip:

        @mcp.tool()
        async def detect_regional_anomalies(
            period: str = "1d",
            baseline_days: int = 7,
            drop_threshold: float = 50.0,
            country_threshold: float = 50.0,
            min_servers: int = 2,
            product: str = "",
            instance: str = "",
        ) -> str:
            """Detect country-level traffic disruptions by analyzing traffic drops per country.

            Args:
                period: Current period to analyze (default: 1d)
                baseline_days: Days for baseline (default: 7)
                drop_threshold: % drop per server to flag (default: 50)
                country_threshold: % of servers affected to flag country (default: 50)
                min_servers: Min servers per country (default: 2)
                product: Filter by product (optional)
                instance: Zabbix instance (optional)
            """
            try:
                client = resolver.resolve(instance)

                hosts = await client.call("host.get", {
                    "output": ["hostid", "host"],
                    "selectGroups": ["name"],
                    "selectInterfaces": ["ip"],
                    "filter": {"status": "0"},
                })

                # Filter by product and group by country
                countries: dict[str, list[dict]] = {}
                for h in hosts:
                    if product:
                        prod, _ = _classify_host(h.get("groups", []))
                        if not prod or product.lower() not in prod.lower():
                            continue
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

                # Also get service health status
                service1_items = await client.call("item.get", {
                    "hostids": all_ids,
                    "output": ["hostid", "lastvalue"],
                    "filter": {"key_": "service_primary_check[{HOST.IP}]", "status": "0"},
                })
                service1_map = build_value_map(service1_items, lambda v: int(float(v)))

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
                    service_down = 0

                    for h in c_hosts:
                        hm = host_metrics.get(h["hostid"], {})
                        traffic = hm.get("traffic")
                        service1 = service1_map.get(h["hostid"])

                        if service1 == 0:
                            service_down += 1

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
                            "service_down": service_down,
                            "severity": severity,
                            "hosts": c_hosts,
                        })
                    else:
                        healthy.append(ctry)

                if not blocked:
                    return f"No regional anomalys detected across {len(countries)} countries ({sum(len(v) for v in countries.values())} servers)."

                parts = [
                    f"**Geo-Block Detection: {len(blocked)} countries affected**\n",
                    "| Country | Servers | Affected | Drop Avg | service DOWN | Severity |",
                    "|---------|---------|----------|----------|----------|----------|",
                ]
                for b in sorted(blocked, key=lambda x: -x["pct"]):
                    parts.append(
                        f"| {b['country']} | {b['total']} | "
                        f"{b['affected']}/{b['total']} ({b['pct']:.0f}%) | "
                        f"-{b['avg_drop']:.0f}% | {b['service_down']} | {b['severity']} |"
                    )

                # Detail per affected country
                for b in blocked:
                    parts.append(f"\n### {b['country']} — {b['severity']}")
                    for h in b["hosts"]:
                        hm = host_metrics.get(h["hostid"], {})
                        traffic = hm.get("traffic")
                        service1 = service1_map.get(h["hostid"])
                        t_now = f"{traffic.current:.1f}" if traffic else "N/A"
                        t_avg = f"{traffic.avg:.1f}" if traffic else "N/A"
                        service = "DOWN" if service1 == 0 else ("OK" if service1 == 1 else "?")
                        parts.append(f"- {h['host']}: {t_now} Mbps (was {t_avg}) | service: {service}")

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
            region: str = "",
            instance: str = "",
        ) -> str:
            """Per-country traffic trends over time — detect usage growth or decline per region.

            Args:
                period: Time period (default: 30d)
                aggregation: 'summary' or 'daily' (default: daily)
                min_servers: Minimum servers per country (default: 2)
                min_traffic: Minimum avg Gbps to include country (default: 0.1)
                region: Filter by region: LATAM, APAC, EMEA, NA, CIS, ALL (optional)
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

                if region:
                    region_codes = countries_for_region(region)
                    if not region_codes:
                        return f"Unknown region '{region}'. Use: LATAM, APAC, EMEA, NA, CIS, ALL."
                    countries = {c: ids for c, ids in countries.items() if c in region_codes}
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
                    # Derive trend from aggregated daily data (not last host)
                    daily_vals = sorted(cd["daily"].items())
                    if len(daily_vals) >= 4:
                        q = len(daily_vals) // 4
                        older_avg = sum(v for _, v in daily_vals[:q]) / q
                        recent_avg = sum(v for _, v in daily_vals[-q:]) / q
                        dir_pct = ((recent_avg - older_avg) / older_avg * 100) if older_avg > 0 else 0
                        trend = "rising" if dir_pct > 15 else "dropping" if dir_pct < -15 else "stable"
                    else:
                        trend = cd["trend"]
                    if cd["current"] < 1 and cd["avg"] > 10:
                        trend = "dead"
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

    if "get_service_uptime_report" not in skip:

        @mcp.tool()
        async def get_service_uptime_report(
            country: str = "",
            product: str = "",
            exclude_product: str = "infrastructure,monitoring",
            only_problems: bool = True,
            max_results: int = 50,
            period: str = "30d",
            instance: str = "",
        ) -> str:
            """service protocol availability per server — uptime % per protocol.

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

                # Fetch trend data for service check items
                now = int(_time.time())
                from zbbx_mcp.data import _parse_period
                time_from = now - _parse_period(period)

                # Get items for service protocol checks
                service_items = await client.call("item.get", {
                    "hostids": hostids,
                    "output": ["itemid", "hostid", "key_"],
                    "filter": {"key_": [
                        "service_primary_check[{HOST.IP}]",
                        "service_secondary_check[{HOST.IP}]",
                    ], "status": "0"},
                })

                if not service_items:
                    return "No service check items found."

                # Fetch trends for service items
                item_ids = [i["itemid"] for i in service_items]
                trends = await client.call("trend.get", {
                    "itemids": item_ids,
                    "time_from": time_from,
                    "output": ["itemid", "value_avg", "num"],
                    "limit": len(item_ids) * 24 * 31,
                })

                # Map itemid -> (hostid, protocol)
                item_info: dict[str, tuple[str, str]] = {}
                for i in service_items:
                    proto = "service1" if "service1" in i["key_"] else "service2"
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

                    service1 = hu.get("service1", {"up": 0, "total": 0})
                    service2 = hu.get("service2", {"up": 0, "total": 0})

                    service1_pct = (service1["up"] / service1["total"] * 100) if service1["total"] > 0 else None
                    service2_pct = (service2["up"] / service2["total"] * 100) if service2["total"] > 0 else None

                    overall = "HEALTHY"
                    if service1_pct is not None and service1_pct < 50:
                        overall = "DOWN"
                    elif service1_pct is not None and service1_pct < 90:
                        overall = "DEGRADED"

                    rows.append({
                        "host": hostname, "country": ctry,
                        "service1": service1_pct, "service2": service2_pct,
                        "overall": overall, "hours": service1["total"],
                    })

                rows.sort(key=lambda r: (r["service1"] or 100))

                # Filter and limit
                total_all = len(rows)
                healthy_count = sum(1 for r in rows if r["overall"] == "HEALTHY")
                if only_problems:
                    rows = [r for r in rows if r["overall"] != "HEALTHY"]
                shown = rows[:max_results]
                omitted = len(rows) - len(shown)

                parts = [
                    f"**service Availability ({period}): {total_all} servers ({healthy_count} healthy)**\n",
                    "| Server | Country | service Primary | service Secondary | Status |",
                    "|--------|---------|-------------|-------------|--------|",
                ]
                for r in shown:
                    x = f"{r['service1']:.1f}%" if r["service1"] is not None else "N/A"
                    k = f"{r['service2']:.1f}%" if r["service2"] is not None else "N/A"
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
                    parts.append("| Country | Servers | Avg service Uptime | DOWN |")
                    parts.append("|---------|---------|-----------------|------|")
                    for ctry in sorted(country_stats):
                        cs = country_stats[ctry]
                        service1_vals = [r["service1"] for r in cs if r["service1"] is not None]
                        avg_x = f"{median(service1_vals):.1f}%" if service1_vals else "N/A"
                        down = sum(1 for r in cs if r["overall"] == "DOWN")
                        parts.append(f"| {ctry} | {len(cs)} | {avg_x} | {down} |")

                return "\n".join(parts)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

    if "get_service_health_matrix" not in skip:

        @mcp.tool()
        async def get_service_health_matrix(
            min_servers: int = 2,
            instance: str = "",
        ) -> str:
            """Show which service protocol works in which country.

            Aggregates service protocol check status per country.
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

                service1_items, service2_items, service3_items = await asyncio.gather(
                    client.call("item.get", {
                        "hostids": all_ids, "output": ["hostid", "lastvalue"],
                        "filter": {"key_": "service_primary_check[{HOST.IP}]", "status": "0"},
                    }),
                    client.call("item.get", {
                        "hostids": all_ids, "output": ["hostid", "lastvalue"],
                        "filter": {"key_": "service_secondary_check[{HOST.IP}]", "status": "0"},
                    }),
                    client.call("item.get", {
                        "hostids": all_ids, "output": ["hostid", "lastvalue"],
                        "search": {"key_": "service3"}, "searchWildcardsEnabled": True,
                        "filter": {"status": "0"},
                    }),
                )

                service1_map = build_value_map(service1_items, lambda v: int(float(v)))
                service2_map = build_value_map(service2_items, lambda v: int(float(v)))
                service3_map: dict[str, int] = {}
                for i in service3_items:
                    try:
                        val = int(float(i["lastvalue"]))
                        hid = i["hostid"]
                        if val > service3_map.get(hid, 0):
                            service3_map[hid] = val
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

                    service1_up = sum(1 for hid in hids if service1_map.get(hid) == 1)
                    service2_up = sum(1 for hid in hids if service2_map.get(hid) == 1)
                    service3_up = sum(1 for hid in hids if service3_map.get(hid, 0) >= 1)

                    service_primary_checked = sum(1 for hid in hids if hid in service1_map)
                    service_secondary_checked = sum(1 for hid in hids if hid in service2_map)
                    service3_checked = sum(1 for hid in hids if hid in service3_map)

                    def _status(up: int, checked: int) -> str:
                        if checked == 0:
                            return "N/A"
                        pct = up / checked * 100
                        if pct >= 80:
                            return f"OK ({up}/{checked})"
                        if pct >= 30:
                            return f"PARTIAL ({up}/{checked})"
                        return f"DOWN ({up}/{checked})"

                    x_s = _status(service1_up, service_primary_checked)
                    k_s = _status(service2_up, service_secondary_checked)
                    o_s = _status(service3_up, service3_checked)

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

    if "get_traffic_drop_timeline" not in skip:

        @mcp.tool()
        async def get_traffic_drop_timeline(
            period: str = "30d",
            min_servers: int = 2,
            instance: str = "",
        ) -> str:
            """Show when traffic disruptions started per country, using daily trend data.

            Finds the day traffic dropped to near-zero for each affected country.

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
                        continue  # No significant drop

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
                            "drop_start": block_day,
                            "duration": duration_days,
                            "pre_drop_gbps": round(peak / 1000, 2),
                            "current_gbps": round(current / 1000, 2),
                        })

                if not blocks:
                    return f"No traffic disruptions detected in {period} across {len(countries)} countries."

                blocks.sort(key=lambda b: -b["duration"])

                parts = [
                    f"**Block Timeline ({period})**\n",
                    "| Country | Servers | Block Started | Duration | Pre-block Traffic | Current |",
                    "|---------|---------|--------------|----------|-------------------|---------|",
                ]
                for b in blocks:
                    parts.append(
                        f"| {b['country']} | {b['servers']} | {b['drop_start']} | "
                        f"{b['duration']}d | {b['pre_drop_gbps']} Gbps | {b['current_gbps']} Gbps |"
                    )

                return "\n".join(parts)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

    
    if "get_expansion_report" not in skip:

        @mcp.tool()
        async def get_expansion_report(
            region: str = "ALL",
            min_traffic_mbps: float = 0,
            max_results: int = 40,
            instance: str = "",
        ) -> str:
            """Coverage gap analysis — countries with/without servers, capacity headroom.

            Shows server density (servers/Gbps) per country, flags overloaded
            countries needing more servers, identifies expansion opportunities.

            Args:
                region: LATAM, APAC, EMEA, NA, CIS, ALL (default: ALL)
                min_traffic_mbps: Minimum traffic to include country (default: 0)
                max_results: Maximum countries to show (default: 40)
                instance: Zabbix instance name (optional)
            """
            try:
                client = resolver.resolve(instance)
                from zbbx_mcp.classify import detect_provider

                region_codes = countries_for_region(region)
                if not region_codes:
                    return f"Unknown region '{region}'. Use: LATAM, APAC, EMEA, NA, CIS, ALL."

                hosts = await fetch_enabled_hosts(client)
                by_country = group_by_country(hosts, region=region)

                all_ids = [h["hostid"] for hs in by_country.values() for h in hs]
                traffic_map = await fetch_traffic_map(client, all_ids)

                rows = []
                for cc, cc_hosts in by_country.items():
                    total_mbps = sum(traffic_map.get(h["hostid"], 0) for h in cc_hosts)
                    if total_mbps < min_traffic_mbps and min_traffic_mbps > 0:
                        continue
                    density = total_mbps / len(cc_hosts) if cc_hosts else 0
                    providers = {detect_provider(host_ip(h)) for h in cc_hosts if host_ip(h)}
                    rows.append({
                        "cc": cc, "servers": len(cc_hosts),
                        "traffic_gbps": round(total_mbps / 1000, 2),
                        "density_mbps": round(density, 1),
                        "providers": len(providers),
                        "status": "OVERLOADED" if density > 500 else "OK" if density > 50 else "LOW",
                    })

                missing = sorted(region_codes - set(by_country.keys()))
                rows.sort(key=lambda x: -x["traffic_gbps"])
                shown = rows[:max_results]

                lines = [f"**Expansion Report — {region}** ({len(rows)} countries with servers)\n"]
                lines.append("| Country | Servers | Traffic Gbps | Mbps/srv | Providers | Status |")
                lines.append("|---------|---------|-------------|----------|-----------|--------|")
                for s in shown:
                    lines.append(
                        f"| {s['cc']} | {s['servers']} | {s['traffic_gbps']} | "
                        f"{s['density_mbps']} | {s['providers']} | {s['status']} |"
                    )

                if len(rows) > max_results:
                    lines.append(f"\n*{len(rows) - max_results} more countries omitted*")

                overloaded = [s for s in rows if s["status"] == "OVERLOADED"]
                if overloaded:
                    lines.append(f"\n**Needs more servers:** {', '.join(s['cc'] for s in overloaded)}")
                if missing:
                    lines.append(f"\n**No servers in region:** {', '.join(missing[:20])}")

                return "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

    
    if "get_regional_density_map" not in skip:

        @mcp.tool()
        async def get_regional_density_map(
            region: str = "ALL",
            country: str = "",
            min_traffic_mbps: float = 0,
            max_results: int = 40,
            instance: str = "",
        ) -> str:
            """Server density by country — count, traffic, CPU, provider mix.

            Highlights countries with only 1 server (no redundancy).

            Args:
                region: LATAM, APAC, EMEA, NA, CIS, ALL (default: ALL)
                country: Filter by specific country code (optional)
                min_traffic_mbps: Minimum total traffic to include (default: 0)
                max_results: Maximum rows (default: 40)
                instance: Zabbix instance name (optional)
            """
            try:
                client = resolver.resolve(instance)

                hosts = await fetch_enabled_hosts(client)
                by_country = group_by_country(hosts, country=country, region=region)

                if not by_country:
                    return "No servers match the filters."

                all_ids = [h["hostid"] for hs in by_country.values() for h in hs]
                traffic_map, cpu_map = await asyncio.gather(
                    fetch_traffic_map(client, all_ids),
                    fetch_cpu_map(client, all_ids),
                )

                rows = []
                for cc, cc_hosts in by_country.items():
                    total_mbps = sum(traffic_map.get(h["hostid"], 0) for h in cc_hosts)
                    if total_mbps < min_traffic_mbps and min_traffic_mbps > 0:
                        continue
                    cpus = [cpu_map[h["hostid"]] for h in cc_hosts if h["hostid"] in cpu_map]
                    avg_cpu = round(sum(cpus) / len(cpus), 1) if cpus else 0
                    dcs = set()
                    for h in cc_hosts:
                        ip = host_ip(h)
                        if ip:
                            prov, city = resolve_datacenter(ip)
                            dcs.add(city if city and city != "Various" else prov)
                    flag = " **!**" if len(cc_hosts) == 1 else ""
                    rows.append({
                        "cc": cc, "servers": len(cc_hosts), "flag": flag,
                        "traffic_gbps": round(total_mbps / 1000, 2),
                        "avg_cpu": avg_cpu,
                        "dcs": ", ".join(sorted(dcs - {""}))[:40] or "?",
                    })

                rows.sort(key=lambda x: -x["traffic_gbps"])
                shown = rows[:max_results]

                lines = [f"**Density Map** ({len(rows)} countries)\n"]
                lines.append("| Country | Servers | Traffic Gbps | Avg CPU% | Datacenters |")
                lines.append("|---------|---------|-------------|----------|-------------|")
                for r in shown:
                    lines.append(
                        f"| {r['cc']}{r['flag']} | {r['servers']} | {r['traffic_gbps']} | "
                        f"{r['avg_cpu']}% | {r['dcs']} |"
                    )

                no_redundancy = [r for r in rows if r["servers"] == 1]
                if no_redundancy:
                    lines.append(f"\n**No redundancy (1 server):** {', '.join(r['cc'] for r in no_redundancy)}")
                if len(rows) > max_results:
                    lines.append(f"\n*{len(rows) - max_results} more countries omitted*")

                return "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

    
    if "get_latency_estimate" not in skip:

        @mcp.tool()
        async def get_latency_estimate(
            client_country: str = "",
            product: str = "",
            max_results: int = 10,
            instance: str = "",
        ) -> str:
            """Estimate nearest server for a given client country by geographic distance.

            Uses haversine between capital coordinates as a rough latency endpoint.
            Useful for expansion planning — "where should we add next server?"

            Args:
                client_country: 2-letter country code (required)
                product: Filter by product name (optional)
                max_results: Maximum results (default: 10)
                instance: Zabbix instance name (optional)
            """
            import math

            from zbbx_mcp.data import CAPITAL_COORDS

            def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
                R = 6371
                dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
                a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
                return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

            if not client_country:
                return "client_country is required (2-letter code, e.g. 'CO')."

            cc = client_country.upper()
            if cc not in CAPITAL_COORDS:
                return f"Unknown country '{cc}'. No coordinates available."

            try:
                client_inst = resolver.resolve(instance)
                hosts = await client_inst.call("host.get", {
                    "output": ["hostid", "host"],
                    "selectGroups": ["name"],
                    "filter": {"status": "0"},
                })

                server_countries: dict[str, int] = {}
                for h in hosts:
                    if product:
                        prod, _ = _classify_host(h.get("groups", []))
                        if not prod or product.lower() not in prod.lower():
                            continue
                    srv_cc = extract_country(h["host"])
                    if srv_cc:
                        server_countries[srv_cc] = server_countries.get(srv_cc, 0) + 1

                if not server_countries:
                    return "No servers found."

                client_lat, client_lon = CAPITAL_COORDS[cc]
                distances = []
                for srv_cc, count in server_countries.items():
                    if srv_cc in CAPITAL_COORDS:
                        srv_lat, srv_lon = CAPITAL_COORDS[srv_cc]
                        dist = _haversine(client_lat, client_lon, srv_lat, srv_lon)
                        distances.append({"cc": srv_cc, "servers": count, "km": round(dist)})

                distances.sort(key=lambda x: x["km"])
                shown = distances[:max_results]

                lines = [f"**Nearest servers for clients in {cc}**\n"]
                lines.append("| Server Country | Servers | Distance km |")
                lines.append("|---------------|---------|------------|")
                for d in shown:
                    marker = " *" if d["cc"] == cc else ""
                    lines.append(f"| {d['cc']}{marker} | {d['servers']} | {d['km']:,} |")

                if cc not in server_countries:
                    nearest = distances[0] if distances else None
                    if nearest:
                        lines.append(f"\n**No servers in {cc}.** Nearest: {nearest['cc']} ({nearest['km']:,} km)")

                return "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"
