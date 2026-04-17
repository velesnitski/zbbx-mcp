"""Geo-level traffic analysis: regional anomalies, trends, drop timelines, expansion gaps."""

from __future__ import annotations

import httpx

from zbbx_mcp.classify import classify_host as _classify_host
from zbbx_mcp.data import (
    KEY_service_PRIMARY,
    build_value_map,
    countries_for_region,
    extract_country,
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
            min_avg_mbps: float = 10.0,
            product: str = "",
            instance: str = "",
        ) -> str:
            """Detect country-level regional traffic anomalies by analyzing traffic drops per country.

            Args:
                period: Current period to analyze (default: 1d)
                baseline_days: Days for baseline (default: 7)
                drop_threshold: % drop per server to flag (default: 50)
                country_threshold: % of servers affected to flag country (default: 50)
                min_servers: Min servers per country (default: 2)
                min_avg_mbps: Min avg country traffic to alert (default: 10 Mbps,
                    filters micro-markets where % drops are statistical noise)
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
                service1_map: dict[str, int] = {}
                if KEY_service_PRIMARY:
                    service1_items = await client.call("item.get", {
                        "hostids": all_ids,
                        "output": ["hostid", "lastvalue"],
                        "filter": {"key_": KEY_service_PRIMARY, "status": "0"},
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
                    country_avg_mbps = 0.0

                    for h in c_hosts:
                        hm = host_metrics.get(h["hostid"], {})
                        traffic = hm.get("traffic")
                        service1_status = service1_map.get(h["hostid"])

                        if service1_status == 0:
                            service_down += 1

                        if traffic:
                            country_avg_mbps += traffic.avg

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

                    # Filter micro-markets — % drops on near-zero traffic are noise
                    if country_avg_mbps < min_avg_mbps:
                        continue

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
                    return f"No regional anomalies detected across {len(countries)} countries ({sum(len(v) for v in countries.values())} servers)."

                parts = [
                    f"**Regional Anomaly Detection: {len(blocked)} countries affected**\n",
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
                        service1_status = service1_map.get(h["hostid"])
                        t_now = f"{traffic.current:.1f}" if traffic else "N/A"
                        t_avg = f"{traffic.avg:.1f}" if traffic else "N/A"
                        service = "DOWN" if service1_status == 0 else ("OK" if service1_status == 1 else "?")
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
                    if len(daily_vals) >= 4 and avg_gbps >= 0.05:
                        q = max(len(daily_vals) // 4, 1)
                        older_avg = sum(v for _, v in daily_vals[:q]) / q
                        recent_avg = sum(v for _, v in daily_vals[-q:]) / q
                        if older_avg > 0:
                            dir_pct = (recent_avg - older_avg) / older_avg * 100
                            trend = "rising" if dir_pct > 15 else "dropping" if dir_pct < -15 else "stable"
                        else:
                            trend = "rising" if recent_avg > 0 else "stable"
                    else:
                        # Too little traffic for meaningful trend
                        trend = "stable" if avg_gbps < 0.05 else cd.get("trend", "stable")
                    # Sanity: trend label must match change direction
                    if change <= -30 and trend in ("stable", "rising"):
                        trend = "dropping"
                    elif change >= 30 and trend in ("stable", "dropping"):
                        trend = "rising"
                    elif (change <= -10 and trend == "rising") or (change > 0 and trend == "dropping"):
                        trend = "stable"
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

    if "get_traffic_drop_timeline" not in skip:

        @mcp.tool()
        async def get_traffic_drop_timeline(
            period: str = "30d",
            min_servers: int = 2,
            instance: str = "",
        ) -> str:
            """Traffic drop timeline per country — when drops started and duration.

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

                # Find traffic drop start date for each country
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
                    return f"No traffic drops detected in {period} across {len(countries)} countries."

                blocks.sort(key=lambda b: -b["duration"])

                parts = [
                    f"**Traffic Drop Timeline ({period})**\n",
                    "| Country | Servers | Drop Started | Duration | Pre-drop Traffic | Current |",
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
            """Coverage gap analysis per country with capacity headroom.

            Args:
                region: LATAM, APAC, EMEA, NA, CIS, ALL (default: ALL)
                min_traffic_mbps: Min traffic to include (default: 0)
                max_results: Max countries to show (default: 40)
                instance: Zabbix instance (optional)
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
                        "status": "OVERLOADED" if density > 3000 else "HIGH" if density > 1500 else "OK" if density > 50 else "LOW",
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
