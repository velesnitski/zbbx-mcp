"""Executive reporting: KPI dashboards, risk scoring, period comparison."""

from __future__ import annotations

import asyncio
import json
import os
import time as _time
from datetime import datetime, timezone

import httpx

from zbbx_mcp.classify import classify_host as _classify_host
from zbbx_mcp.classify import detect_provider
from zbbx_mcp.data import (
    KEY_service_PRIMARY,
    build_value_map,
    extract_country,
    fetch_cpu_map,
    fetch_enabled_hosts,
    fetch_host_dashboards,
    fetch_traffic_map,
    fetch_trends_batch,
    group_by_country,
    host_ip,
)
from zbbx_mcp.resolver import InstanceResolver
from zbbx_mcp.utils import safe_output_path


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()) -> None:

    
    if "get_executive_dashboard" not in skip:

        @mcp.tool()
        async def get_executive_dashboard(
            period: str = "30d",
            instance: str = "",
        ) -> str:
            """Single-call KPI summary for leadership — totals, health, growth, risks.

            Args:
                period: Lookback for trends (default: 30d)
                instance: Zabbix instance name (optional)
            """
            try:
                client = resolver.resolve(instance)

                hosts = await fetch_enabled_hosts(client)
                all_ids = [h["hostid"] for h in hosts]

                traffic_map, cpu_map, (trend_rows, _) = await asyncio.gather(
                    fetch_traffic_map(client, all_ids),
                    fetch_cpu_map(client, all_ids),
                    fetch_trends_batch(client, all_ids, ["traffic"], period),
                )

                # Totals
                by_country = group_by_country(hosts)
                products = set()
                providers = set()
                for h in hosts:
                    prod, _ = _classify_host(h.get("groups", []))
                    if prod and prod != "Unknown":
                        products.add(prod)
                    ip = host_ip(h)
                    if ip:
                        prov = detect_provider(ip)
                        if prov not in ("Unknown", "Other"):
                            providers.add(prov)

                total_traffic_gbps = round(sum(traffic_map.values()) / 1000, 1)
                avg_cpu = round(sum(cpu_map.values()) / len(cpu_map), 1) if cpu_map else 0

                # Growth: compare first vs last week of trend data
                growth_by_country: dict[str, tuple[float, float]] = {}
                for tr in trend_rows:
                    cc = extract_country(tr.hostname)
                    if not cc or not tr.daily:
                        continue
                    days = sorted(tr.daily.keys())
                    if len(days) < 7:
                        continue
                    first_week = sum(tr.daily[d] for d in days[:7]) / 7
                    last_week = sum(tr.daily[d] for d in days[-7:]) / 7
                    old = growth_by_country.get(cc, (0, 0))
                    growth_by_country[cc] = (old[0] + first_week, old[1] + last_week)

                growing = []
                for cc, (first, last) in growth_by_country.items():
                    if first > 0:
                        pct = (last - first) / first * 100
                        growing.append((cc, pct))
                growing.sort(key=lambda x: -x[1])

                # Health: high CPU countries
                high_cpu_countries = []
                for cc, cc_hosts in by_country.items():
                    cpus = [cpu_map[h["hostid"]] for h in cc_hosts if h["hostid"] in cpu_map]
                    if cpus and sum(cpus) / len(cpus) > 70:
                        high_cpu_countries.append(cc)

                lines = [
                    f"**Fleet:** {len(hosts)} servers, {len(by_country)} countries, "
                    f"{len(products)} products, {len(providers)} providers",
                    f"**Traffic:** {total_traffic_gbps} Gbps | **Avg CPU:** {avg_cpu}%",
                ]

                if high_cpu_countries:
                    lines.append(f"**High CPU (>70%):** {', '.join(sorted(high_cpu_countries))}")

                if growing[:5]:
                    top = ", ".join(f"{cc} +{pct:.0f}%" for cc, pct in growing[:5] if pct > 5)
                    if top:
                        lines.append(f"**Growth ({period}):** {top}")

                declining = [x for x in growing if x[1] < -20]
                if declining[:3]:
                    drop = ", ".join(f"{cc} {pct:.0f}%" for cc, pct in declining[:3])
                    lines.append(f"**Declining:** {drop}")

                return "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

    
    if "get_month_over_month" not in skip:

        @mcp.tool()
        async def get_month_over_month(
            days: int = 30,
            instance: str = "",
        ) -> str:
            """Compare current vs previous period — traffic, CPU, countries.

            Args:
                days: Period length in days (default: 30 — compares last 30d vs prior 30d)
                instance: Zabbix instance name (optional)
            """
            try:
                client = resolver.resolve(instance)
                hosts = await fetch_enabled_hosts(client)
                all_ids = [h["hostid"] for h in hosts]

                # Fetch two separate periods (avoids Zabbix 500 on large ranges)
                rows_a, _ = await fetch_trends_batch(client, all_ids, ["traffic"], f"{days * 2}d")

                def _agg(rows, recent: bool):
                    """Split daily data: recent=True for period B, False for A."""
                    cutoff = _time.time() - days * 86400
                    current_year = datetime.now(timezone.utc).year

                    traffic_by_host: dict[str, float] = {}
                    country_set: set[str] = set()
                    for r in rows:
                        if r.metric != "traffic" or not r.daily:
                            continue
                        total = 0.0
                        count = 0
                        for day_str, val in r.daily.items():
                            try:
                                dt = datetime.strptime(day_str, "%b %d").replace(year=current_year, tzinfo=timezone.utc)
                            except ValueError:
                                continue
                            if (dt.timestamp() >= cutoff) == recent:
                                total += val
                                count += 1
                        if count > 0:
                            traffic_by_host[r.hostid] = total / count
                        cc = extract_country(r.hostname)
                        if cc:
                            country_set.add(cc)

                    traffic_gbps = round(sum(traffic_by_host.values()) / 1000, 1)
                    return {"traffic_gbps": traffic_gbps, "countries": len(country_set)}

                a = _agg(rows_a, recent=False)
                b = _agg(rows_a, recent=True)

                # CPU from current snapshot (no trend needed)
                cpu_map = await fetch_cpu_map(client, all_ids)
                avg_cpu = round(sum(cpu_map.values()) / len(cpu_map), 1) if cpu_map else 0

                def _delta(va, vb):
                    if va == 0:
                        return "–"
                    pct = (vb - va) / abs(va) * 100
                    return f"{pct:+.1f}%"

                lines = [
                    f"**Period comparison: prior {days}d vs last {days}d**\n",
                    "| Metric | Period A | Period B | Delta |",
                    "|--------|----------|----------|-------|",
                    f"| Traffic Gbps | {a['traffic_gbps']} | {b['traffic_gbps']} | {_delta(a['traffic_gbps'], b['traffic_gbps'])} |",
                    f"| Avg CPU % | – | {avg_cpu} | – |",
                    f"| Countries | {a['countries']} | {b['countries']} | {_delta(a['countries'], b['countries'])} |",
                    f"| Servers | {len(hosts)} | {len(hosts)} | – |",
                ]
                return "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

    
    if "get_fleet_risk_score" not in skip:

        @mcp.tool()
        async def get_fleet_risk_score(
            region: str = "ALL",
            min_servers: int = 1,
            max_results: int = 20,
            instance: str = "",
        ) -> str:
            """Composite risk assessment per country — provider concentration, capacity, protocol diversity.

            Args:
                region: LATAM, APAC, EMEA, NA, CIS, ALL (default: ALL)
                min_servers: Minimum servers in country (default: 1)
                max_results: Maximum results (default: 20)
                instance: Zabbix instance name (optional)
            """
            try:
                client = resolver.resolve(instance)

                hosts = await fetch_enabled_hosts(client)
                by_country = group_by_country(hosts, region=region)

                all_ids = [h["hostid"] for hs in by_country.values() for h in hs]
                traffic_map, cpu_map = await asyncio.gather(
                    fetch_traffic_map(client, all_ids),
                    fetch_cpu_map(client, all_ids),
                )

                rows = []
                for cc, cc_hosts in by_country.items():
                    if len(cc_hosts) < min_servers:
                        continue

                    score = 0
                    risks = []

                    # Provider concentration
                    provs = [detect_provider(host_ip(h)) for h in cc_hosts if host_ip(h)]
                    if provs:
                        top_prov_pct = max(provs.count(p) for p in set(provs)) / len(provs) * 100
                        if top_prov_pct > 80:
                            score += 30
                            risks.append("single provider")
                        elif top_prov_pct > 60:
                            score += 15

                    # Capacity: avg CPU
                    cpus = [cpu_map[h["hostid"]] for h in cc_hosts if h["hostid"] in cpu_map]
                    avg_cpu = sum(cpus) / len(cpus) if cpus else 0
                    if avg_cpu > 80:
                        score += 30
                        risks.append("CPU >80%")
                    elif avg_cpu > 60:
                        score += 15

                    # Redundancy
                    if len(cc_hosts) == 1:
                        score += 25
                        risks.append("no redundancy")
                    elif len(cc_hosts) == 2:
                        score += 10

                    # Traffic concentration
                    traffics = [traffic_map.get(h["hostid"], 0) for h in cc_hosts]
                    traffic_total = sum(traffics)
                    if traffics and traffic_total > 0:
                        top_pct = max(traffics) / traffic_total * 100
                        if top_pct > 80 and len(cc_hosts) > 1:
                            score += 15
                            risks.append("traffic concentrated")

                    rows.append({
                        "cc": cc, "score": min(score, 100),
                        "servers": len(cc_hosts),
                        "risk": risks[0] if risks else "OK",
                        "rec": "add servers" if "no redundancy" in risks else
                               "diversify providers" if "single provider" in risks else
                               "upgrade capacity" if "CPU >80%" in risks else "monitor",
                    })

                rows.sort(key=lambda x: -x["score"])
                shown = rows[:max_results]

                lines = [f"**Fleet Risk Score — {region}** ({len(rows)} countries)\n"]
                lines.append("| Country | Score | Servers | Top Risk | Action |")
                lines.append("|---------|-------|---------|----------|--------|")
                for r in shown:
                    lines.append(f"| {r['cc']} | {r['score']}/100 | {r['servers']} | {r['risk']} | {r['rec']} |")

                if len(rows) > max_results:
                    lines.append(f"\n*{len(rows) - max_results} more omitted*")
                return "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

    
    if "get_sla_dashboard" not in skip:

        @mcp.tool()
        async def get_sla_dashboard(
            period: str = "30d",
            product: str = "",
            country: str = "",
            max_results: int = 20,
            instance: str = "",
        ) -> str:
            """Uptime % aggregated by product and country, weighted by traffic.

            Args:
                period: Analysis period (default: 30d)
                product: Filter by product (optional)
                country: Filter by country code (optional)
                max_results: Maximum rows (default: 20)
                instance: Zabbix instance name (optional)
            """
            try:
                client = resolver.resolve(instance)
                hosts = await fetch_enabled_hosts(client)

                # service primary check items
                service_map: dict[str, int] = {}
                if KEY_service_PRIMARY:
                    service_items = await client.call("item.get", {
                        "hostids": [h["hostid"] for h in hosts],
                        "output": ["hostid", "lastvalue"],
                        "filter": {"key_": KEY_service_PRIMARY, "status": "0"},
                    })
                    service_map = build_value_map(service_items, lambda v: int(float(v)))

                # Get trend data for uptime estimation
                all_ids = [h["hostid"] for h in hosts]
                traffic_map = await fetch_traffic_map(client, all_ids)

                # Aggregate by product + country (only servers WITH service check item)
                agg: dict[str, dict] = {}
                for h in hosts:
                    hid = h["hostid"]
                    if hid not in service_map:
                        continue  # skip servers without service check item
                    prod, _ = _classify_host(h.get("groups", []))
                    if product and product.lower() not in (prod or "").lower():
                        continue
                    cc = extract_country(h["host"])
                    if country and cc and cc.lower() != country.lower():
                        continue
                    if not cc or not prod:
                        continue

                    key = f"{prod}|{cc}"
                    entry = agg.setdefault(key, {"product": prod, "cc": cc, "up": 0, "total": 0, "traffic": 0})
                    entry["total"] += 1
                    if service_map[hid] == 1:
                        entry["up"] += 1
                    entry["traffic"] += traffic_map.get(hid, 0)

                if not agg:
                    return "No servers match the filters."

                rows = []
                for entry in agg.values():
                    uptime = (entry["up"] / entry["total"] * 100) if entry["total"] > 0 else 0
                    rows.append({
                        "product": entry["product"], "cc": entry["cc"],
                        "uptime": round(uptime, 1), "servers": entry["total"],
                        "traffic_gbps": round(entry["traffic"] / 1000, 2),
                        "down": entry["total"] - entry["up"],
                    })

                rows.sort(key=lambda x: x["uptime"])
                shown = rows[:max_results]

                lines = [f"**SLA Dashboard ({period})**\n"]
                lines.append("| Product | Country | Uptime% | Servers | Down | Traffic Gbps |")
                lines.append("|---------|---------|---------|---------|------|-------------|")
                for r in shown:
                    lines.append(
                        f"| {r['product']} | {r['cc']} | {r['uptime']}% | "
                        f"{r['servers']} | {r['down']} | {r['traffic_gbps']} |"
                    )

                if len(rows) > max_results:
                    lines.append(f"\n*{len(rows) - max_results} more omitted*")
                return "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

    
    if "get_report_snapshot" not in skip:

        @mcp.tool()
        async def get_report_snapshot(
            output: str = "json",
            instance: str = "",
        ) -> str:
            """Save current KPI state as JSON for historical comparison.

            Args:
                output: Output format: 'json' (return JSON) or file path to save
                instance: Zabbix instance name (optional)
            """
            try:
                client = resolver.resolve(instance)
                hosts = await fetch_enabled_hosts(client)
                all_ids = [h["hostid"] for h in hosts]

                traffic_map, cpu_map = await asyncio.gather(
                    fetch_traffic_map(client, all_ids),
                    fetch_cpu_map(client, all_ids),
                )

                by_country = group_by_country(hosts)
                products = set()
                for h in hosts:
                    prod, _ = _classify_host(h.get("groups", []))
                    if prod and prod != "Unknown":
                        products.add(prod)

                snapshot = {
                    "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                    "servers": len(hosts),
                    "countries": len(by_country),
                    "products": len(products),
                    "traffic_gbps": round(sum(traffic_map.values()) / 1000, 1),
                    "avg_cpu": round(sum(cpu_map.values()) / len(cpu_map), 1) if cpu_map else 0,
                    "top_countries": sorted(
                        [
                            {"cc": cc, "servers": len(hs), "traffic_mbps": round(sum(traffic_map.get(h["hostid"], 0) for h in hs), 1)}
                            for cc, hs in by_country.items()
                        ],
                        key=lambda x: -x["traffic_mbps"],
                    )[:10],
                }

                result = json.dumps(snapshot, indent=2)

                if output != "json" and output:
                    out_dir = os.path.dirname(os.path.expanduser(output)) or "~/Downloads"
                    out_name = os.path.basename(output)
                    path = safe_output_path(out_dir, out_name)
                    with open(path, "w") as f:
                        f.write(result)
                    return f"Snapshot saved to {path}"

                return result
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

    
    if "get_peak_analysis" not in skip:

        @mcp.tool()
        async def get_peak_analysis(
            country: str = "",
            host_id: str = "",
            period: str = "7d",
            max_results: int = 10,
            instance: str = "",
        ) -> str:
            """Peak vs off-peak traffic by hour-of-day from trend data.

            Args:
                country: Aggregate all servers in country (optional)
                host_id: Single host ID or hostname (optional)
                period: Lookback period (default: 7d)
                max_results: Max countries/hosts (default: 10)
                instance: Zabbix instance name (optional)
            """
            try:
                client = resolver.resolve(instance)
                from zbbx_mcp.data import TRAFFIC_IN_KEYS

                # Resolve target hosts
                if host_id:
                    if not host_id.isdigit():
                        lookup = await client.call("host.get", {
                            "output": ["hostid", "host"],
                            "filter": {"host": [host_id]},
                        })
                        if not lookup:
                            return f"Host '{host_id}' not found."
                        hosts = lookup
                    else:
                        hosts = [{"hostid": host_id, "host": host_id}]
                else:
                    hosts = await fetch_enabled_hosts(client, groups=False, interfaces=False)
                    if country:
                        hosts = [h for h in hosts if extract_country(h["host"]).lower() == country.lower()]

                if not hosts:
                    return "No hosts match the filter."

                hids = [h["hostid"] for h in hosts]

                # Get traffic item IDs
                items = await client.call("item.get", {
                    "hostids": hids,
                    "output": ["itemid", "hostid", "key_"],
                    "filter": {"key_": TRAFFIC_IN_KEYS, "status": "0"},
                })

                # Pick best (highest value) item per host
                best_items: dict[str, str] = {}
                for it in items:
                    best_items.setdefault(it["hostid"], it["itemid"])
                item_ids = list(best_items.values())

                if not item_ids:
                    return "No traffic items found."

                # Parse period
                days = int(period.rstrip("d")) if period.endswith("d") else 7
                time_from = int(_time.time()) - days * 86400

                # Fetch hourly trends
                trends = await client.call("trend.get", {
                    "itemids": item_ids,
                    "time_from": time_from,
                    "output": ["itemid", "clock", "value_avg", "value_max"],
                    "limit": len(item_ids) * 24 * days,
                })

                if not trends:
                    return f"No trend data for {period}."

                if country or not host_id:
                    # Aggregate by hour-of-day across all hosts
                    hourly: dict[int, list[float]] = {h: [] for h in range(24)}
                    hourly_max: dict[int, float] = {h: 0 for h in range(24)}
                    for t in trends:
                        dt = datetime.fromtimestamp(int(t["clock"]), tz=timezone.utc)
                        hour = dt.hour
                        avg = float(t.get("value_avg", 0)) * 8 / 1_000_000  # bytes/s → Mbps
                        mx = float(t.get("value_max", 0)) * 8 / 1_000_000
                        hourly[hour].append(avg)
                        hourly_max[hour] = max(hourly_max[hour], mx)

                    label = country.upper() if country else "Fleet"
                    lines = [f"**Peak Analysis — {label}** ({len(hids)} servers, {period})\n"]
                    lines.append("| Hour UTC | Avg Mbps | Peak Mbps | Samples |")
                    lines.append("|----------|---------|----------|---------|")

                    peak_hour, peak_val = 0, 0.0
                    trough_hour, trough_val = 0, float("inf")
                    for h in range(24):
                        vals = hourly[h]
                        if not vals:
                            continue
                        avg = sum(vals) / len(vals)
                        mx = hourly_max[h]
                        if avg > peak_val:
                            peak_hour, peak_val = h, avg
                        if avg < trough_val:
                            trough_hour, trough_val = h, avg
                        lines.append(f"| {h:02d}:00 | {avg:.0f} | {mx:.0f} | {len(vals)} |")

                    ratio = round(peak_val / trough_val, 1) if trough_val > 0 else 0
                    lines.append(f"\n**Peak:** {peak_hour:02d}:00 ({peak_val:.0f} Mbps) | "
                                 f"**Trough:** {trough_hour:02d}:00 ({trough_val:.0f} Mbps) | "
                                 f"**Ratio:** {ratio}x")

                else:
                    # Single host — show raw hourly
                    hourly_vals: list[tuple[str, float, float]] = []
                    for t in sorted(trends, key=lambda x: x["clock"]):
                        dt = datetime.fromtimestamp(int(t["clock"]), tz=timezone.utc)
                        avg = float(t.get("value_avg", 0)) * 8 / 1_000_000
                        mx = float(t.get("value_max", 0)) * 8 / 1_000_000
                        hourly_vals.append((dt.strftime("%m-%d %H:00"), avg, mx))

                    lines = [f"**Peak Analysis — {host_id}** ({period})\n"]
                    lines.append("| Time | Avg Mbps | Peak Mbps |")
                    lines.append("|------|---------|----------|")
                    # Show last 48 hours max to keep token-efficient
                    for ts, avg, mx in hourly_vals[-48:]:
                        lines.append(f"| {ts} | {avg:.0f} | {mx:.0f} |")

                    if hourly_vals:
                        avgs = [v[1] for v in hourly_vals]
                        lines.append(f"\n**Avg:** {sum(avgs)/len(avgs):.0f} Mbps | "
                                     f"**Peak:** {max(avgs):.0f} Mbps | "
                                     f"**Min:** {min(avgs):.0f} Mbps")

                return "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

    
    if "get_product_audit" not in skip:

        @mcp.tool()
        async def get_product_audit(
            product: str = "",
            instance: str = "",
        ) -> str:
            """Audit servers for a product — categorize as active, dead, infra, or idle.

            Args:
                product: Product name to audit (required)
                instance: Zabbix instance name (optional)
            """
            if not product:
                return "product is required."
            try:
                client = resolver.resolve(instance)
                hosts = await fetch_enabled_hosts(client, extra_output=["status"])
                all_ids = [h["hostid"] for h in hosts]

                traffic_map, cpu_map = await asyncio.gather(
                    fetch_traffic_map(client, all_ids),
                    fetch_cpu_map(client, all_ids),
                )

                from zbbx_mcp.data import fetch_service_status
                service_map = await fetch_service_status(client, all_ids)

                # Dashboard lookup
                dash_map = await fetch_host_dashboards(client)

                # Build cluster primary lookup: base hostname → primary's traffic
                # Pattern: "srv01" is primary, "srv01 node3" is secondary
                primary_traffic: dict[str, float] = {}
                for h in hosts:
                    hostname = h["host"]
                    if " " not in hostname:  # no space = potential primary
                        primary_traffic[hostname] = traffic_map.get(h["hostid"], 0)

                matched = []
                for h in hosts:
                    prod, tier = _classify_host(h.get("groups", []))
                    if not prod or product.lower() not in prod.lower():
                        continue
                    hid = h["hostid"]
                    traffic = traffic_map.get(hid, 0)
                    cpu = cpu_map.get(hid, 0)
                    service_val = service_map.get(hid)
                    ip = host_ip(h)
                    prov = detect_provider(ip) if ip else "?"
                    cc = extract_country(h["host"])

                    # For cluster secondaries: check primary's traffic
                    hostname = h["host"]
                    base = hostname.split()[0] if " " in hostname else ""
                    cluster_traffic = primary_traffic.get(base, 0) if base else 0

                    # Categorize
                    if prod == "Infrastructure" or prod == "Monitoring":
                        cat = "INFRA"
                    elif hid not in traffic_map and cluster_traffic > 5:
                        cat = "CLUSTER"  # secondary of active primary
                        traffic = cluster_traffic  # inherit primary's traffic for display
                    elif hid not in traffic_map:
                        cat = "NO DATA"
                    elif traffic < 0.1 and cpu < 2:
                        cat = "DEAD"
                    elif service_val == 0 and traffic < 2:
                        cat = "service DOWN"
                    elif service_val == -1:
                        cat = "DEGRADED"
                    elif traffic < 5:
                        cat = "IDLE"
                    else:
                        cat = "ACTIVE"

                    # Dashboard lookup
                    dash = dash_map.get(hid, "-")

                    matched.append({
                        "host": hostname, "cc": cc,
                        "prov": prov, "traffic": traffic, "cpu": cpu,
                        "service": service_val, "cat": cat, "dash": dash,
                    })

                if not matched:
                    return f"No servers found for '{product}'."

                cats: dict[str, int] = {}
                for m in matched:
                    cats[m["cat"]] = cats.get(m["cat"], 0) + 1

                total_traffic = sum(m["traffic"] for m in matched)
                countries = len({m["cc"] for m in matched if m["cc"]})

                lines = [
                    f"**Product Audit: {product}** ({len(matched)} servers)\n",
                    f"Traffic: {total_traffic / 1000:.1f} Gbps | Countries: {countries}\n",
                    "| Category | Count |",
                    "|----------|-------|",
                ]
                for cat in ["ACTIVE", "DEGRADED", "CLUSTER", "IDLE", "service DOWN", "DEAD", "NO DATA", "INFRA"]:
                    if cat in cats:
                        lines.append(f"| {cat} | {cats[cat]} |")

                for cat in ["INFRA", "DEAD", "NO DATA", "service DOWN", "DEGRADED", "IDLE", "CLUSTER", "ACTIVE"]:
                    servers = [m for m in matched if m["cat"] == cat]
                    if not servers:
                        continue
                    lines.append(f"\n**{cat}** ({len(servers)})")
                    lines.append("| Server | Country | Dashboard | Traffic | CPU | service |")
                    lines.append("|--------|---------|-----------|---------|-----|-----|")
                    for s in sorted(servers, key=lambda x: -x["traffic"])[:12]:
                        service_str = "DOWN" if s["service"] == 0 else ("PARTIAL" if s["service"] == -1 else ("OK" if s["service"] == 1 else "-"))
                        lines.append(f"| {s['host']} | {s['cc'] or '-'} | {s['dash']} | {s['traffic']:.1f} | {s['cpu']}% | {service_str} |")
                    if len(servers) > 12:
                        lines.append(f"*+{len(servers) - 12} more*")

                return "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

    # --- Predictive alerts (#99) ---

    if "get_predictive_alerts" not in skip:

        @mcp.tool()
        async def get_predictive_alerts(
            metric: str = "all",
            days_ahead: int = 30,
            max_results: int = 20,
            instance: str = "",
        ) -> str:
            """Predict upcoming problems — disk full, CPU saturation, memory exhaustion.

            Uses linear regression on 7-day trend data to project when thresholds
            will be crossed. Zero dependencies — pure math.

            Args:
                metric: What to predict: disk, cpu, memory, traffic, or all (default: all)
                days_ahead: Forecast horizon in days (default: 30)
                max_results: Maximum alerts (default: 20)
                instance: Zabbix instance name (optional)
            """
            try:
                client = resolver.resolve(instance)

                hosts = await fetch_enabled_hosts(client, groups=True, interfaces=False,
                                                  extra_output=["name"])
                all_ids = [h["hostid"] for h in hosts]
                host_names = {h["hostid"]: h["host"] for h in hosts}

                # Define metrics to check
                _METRICS = {
                    "disk": {
                        "search": {"key_": "vfs.fs.size["},
                        "filter_key": "pfree",
                        "threshold": 15,  # alert when free% drops below this
                        "direction": "below",
                        "unit": "% free",
                        "label": "Disk Full",
                    },
                    "cpu": {
                        "filter": {"key_": "system.cpu.util[,idle]"},
                        "threshold": 20,  # alert when idle% drops below this (= >80% used)
                        "direction": "below",
                        "unit": "% idle",
                        "label": "CPU Saturation",
                    },
                    "memory": {
                        "filter": {"key_": "vm.memory.size[available]"},
                        "threshold": 500_000_000,  # 500 MB
                        "direction": "below",
                        "unit": "bytes avail",
                        "label": "Memory Exhaustion",
                    },
                }

                metrics_to_check = _METRICS if metric == "all" else {metric: _METRICS[metric]} if metric in _METRICS else {}
                if not metrics_to_check:
                    return f"Unknown metric '{metric}'. Use: disk, cpu, memory, or all."

                now = int(_time.time())
                time_from = now - 14 * 86400  # 14 days of trend data

                alerts = []

                for metric_name, cfg in metrics_to_check.items():
                    # Fetch items
                    params = {
                        "hostids": all_ids,
                        "output": ["itemid", "hostid", "key_", "lastvalue"],
                        "filter": {"status": "0"},
                    }
                    if "search" in cfg:
                        params["search"] = cfg["search"]
                        params["searchWildcardsEnabled"] = True
                        if "filter_key" in cfg:
                            # Post-filter by key substring
                            pass
                    if "filter" in cfg:
                        params["filter"].update(cfg["filter"])

                    items = await client.call("item.get", params)

                    # Handle disk: accept both pfree and pused items
                    if "filter_key" in cfg:
                        pfree_items = [it for it in items if "pfree" in it.get("key_", "")]
                        pused_items = [it for it in items if "pused" in it.get("key_", "")]
                        # Convert pused → pfree equivalent (100 - pused)
                        for it in pused_items:
                            try:
                                it["lastvalue"] = str(100 - float(it.get("lastvalue", 0)))
                                it["_converted"] = True
                            except (ValueError, TypeError):
                                pass
                        items = pfree_items + pused_items

                    # Deduplicate: one item per host (pick the one with lowest current value)
                    best_item: dict[str, dict] = {}
                    for it in items:
                        hid = it["hostid"]
                        try:
                            val = float(it.get("lastvalue", 0))
                        except (ValueError, TypeError):
                            continue
                        if hid not in best_item or val < float(best_item[hid].get("lastvalue", 0)):
                            best_item[hid] = it

                    if not best_item:
                        continue

                    # Fetch 7-day trends for these items
                    item_ids = [it["itemid"] for it in best_item.values()]
                    # Batch to avoid oversized requests
                    all_trends = []
                    for i in range(0, len(item_ids), 200):
                        chunk = item_ids[i:i + 200]
                        trends = await client.call("trend.get", {
                            "itemids": chunk,
                            "time_from": time_from,
                            "output": ["itemid", "clock", "value_avg"],
                            "limit": len(chunk) * 24 * 14,
                        })
                        all_trends.extend(trends)

                    # Group trends by item
                    item_trends: dict[str, list[tuple[int, float]]] = {}
                    for t in all_trends:
                        iid = t["itemid"]
                        try:
                            item_trends.setdefault(iid, []).append(
                                (int(t["clock"]), float(t["value_avg"]))
                            )
                        except (ValueError, TypeError):
                            pass

                    # Linear regression per host
                    threshold = cfg["threshold"]
                    direction = cfg["direction"]

                    for hid, it in best_item.items():
                        iid = it["itemid"]
                        points = sorted(item_trends.get(iid, []))
                        if len(points) < 5:  # need at least 5 data points
                            continue

                        try:
                            current = float(it.get("lastvalue", 0))
                        except (ValueError, TypeError):
                            continue

                        # Simple linear regression: least squares
                        n = len(points)
                        x_vals = [(p[0] - points[0][0]) / 86400 for p in points]  # days
                        y_vals = [p[1] for p in points]
                        x_mean = sum(x_vals) / n
                        y_mean = sum(y_vals) / n
                        num = sum((x - x_mean) * (y - y_mean) for x, y in zip(x_vals, y_vals, strict=False))
                        den = sum((x - x_mean) ** 2 for x in x_vals)
                        if den == 0:
                            continue
                        slope = num / den  # units per day

                        # Project days to threshold
                        if direction == "below":
                            if slope >= 0:
                                continue  # not declining
                            days_to = 0 if current <= threshold else (threshold - current) / slope
                        else:  # above
                            if slope <= 0:
                                continue  # not growing
                            days_to = 0 if current >= threshold else (threshold - current) / slope

                        if days_to < 0 or days_to > days_ahead:
                            continue

                        # Format current value for display
                        if metric_name == "memory":
                            curr_display = f"{current / 1_000_000_000:.1f} GB"
                            rate_display = f"{abs(slope) / 1_000_000:.0f} MB/day"
                        elif metric_name == "disk":
                            curr_display = f"{current:.1f}%"
                            rate_display = f"{abs(slope):.2f}%/day"
                        elif metric_name == "cpu":
                            curr_display = f"{100 - current:.1f}% used"
                            rate_display = f"{abs(slope):.2f}%/day"
                        else:
                            curr_display = f"{current:.1f}"
                            rate_display = f"{abs(slope):.2f}/day"

                        hostname = host_names.get(hid, hid)
                        severity = "CRITICAL" if days_to < 7 else "WARNING" if days_to < 14 else "INFO"

                        alerts.append({
                            "severity": severity,
                            "label": cfg["label"],
                            "host": hostname,
                            "current": curr_display,
                            "rate": rate_display,
                            "days": round(days_to, 1),
                            "days_raw": days_to,
                        })

                if not alerts:
                    return f"No predicted issues within {days_ahead} days."

                # Collapse cluster duplicates: same base hostname + metric + near-identical
                # current/rate values. Cluster secondaries (e.g. "srv-us165 us167") share
                # underlying hardware and produce identical trend data.
                raw_count = len(alerts)
                groups: dict[str, list] = {}
                for a in alerts:
                    base = a["host"].split()[0]
                    key = f"{base}|{a['label']}|{a['current']}|{a['rate']}"
                    groups.setdefault(key, []).append(a)

                deduped = []
                for _key, members in groups.items():
                    rep = members[0]
                    if len(members) > 1:
                        names = [m["host"].split()[-1] for m in members[1:]]
                        rep = {**rep, "host": f"{rep['host']} (+{len(members) - 1}: {', '.join(names[:3])})"}
                    deduped.append(rep)

                deduped.sort(key=lambda a: a["days_raw"])
                shown = deduped[:max_results]
                collapsed = raw_count - len(deduped)

                header_suffix = f" ({collapsed} cluster duplicates collapsed)" if collapsed else ""
                lines = [f"**{len(deduped)} predicted issues**{header_suffix} (next {days_ahead} days)\n"]
                lines.append("| Severity | Issue | Server | Current | Rate | Days Left |")
                lines.append("|----------|-------|--------|---------|------|----------|")
                for a in shown:
                    sev_cls = "CRITICAL" if a["severity"] == "CRITICAL" else "WARNING" if a["severity"] == "WARNING" else "INFO"
                    lines.append(
                        f"| {sev_cls} | {a['label']} | {a['host']} | "
                        f"{a['current']} | {a['rate']} | {a['days']} |"
                    )

                crit = sum(1 for a in deduped if a["severity"] == "CRITICAL")
                warn = sum(1 for a in deduped if a["severity"] == "WARNING")
                if crit:
                    lines.append(f"\n**{crit} CRITICAL** — action needed this week")
                if warn:
                    lines.append(f"**{warn} WARNING** — action needed within 2 weeks")
                if len(deduped) > max_results:
                    lines.append(f"*{len(deduped) - max_results} more omitted*")

                return "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"
