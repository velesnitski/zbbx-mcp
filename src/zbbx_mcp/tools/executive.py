"""Executive reporting: KPI dashboards, risk scoring, period comparison."""

from __future__ import annotations

import asyncio
import json
import os

import httpx

from zbbx_mcp.classify import classify_host as _classify_host
from zbbx_mcp.data import (
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
                        from zbbx_mcp.classify import detect_provider
                        prov = detect_provider(ip)
                        if prov not in ("Unknown", "Other"):
                            providers.add(prov)

                total_traffic_gbps = round(sum(traffic_map.values()) / 1000, 1)
                avg_cpu = round(sum(cpu_map.values()) / len(cpu_map), 1) if cpu_map else 0

                # Growth: compare first vs last week of trend data
                growth_by_country: dict[str, tuple[float, float]] = {}
                for tr in trend_rows:
                    cc = extract_country(tr.host)
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
            period_a: str = "60d-30d",
            period_b: str = "30d",
            instance: str = "",
        ) -> str:
            """Compare two periods across key metrics — traffic, CPU, server count.

            Args:
                period_a: First period (default: 60d-30d, i.e. previous month)
                period_b: Second period (default: 30d, i.e. current month)
                instance: Zabbix instance name (optional)
            """
            try:
                client = resolver.resolve(instance)
                hosts = await fetch_enabled_hosts(client)
                all_ids = [h["hostid"] for h in hosts]

                # Fetch trends for both periods
                rows_a, _ = await fetch_trends_batch(client, all_ids, ["cpu", "traffic"], period_a)
                rows_b, _ = await fetch_trends_batch(client, all_ids, ["cpu", "traffic"], period_b)

                def _agg(rows):
                    traffic = sum(r.avg for r in rows if r.metric == "traffic") / 1_000_000_000 * 8
                    cpus = [100 - r.avg for r in rows if r.metric == "cpu" and r.avg > 0]
                    avg_cpu = sum(cpus) / len(cpus) if cpus else 0
                    countries = len({extract_country(r.host) for r in rows if extract_country(r.host)})
                    return {"traffic_gbps": round(traffic, 1), "avg_cpu": round(avg_cpu, 1), "countries": countries}

                a = _agg(rows_a)
                b = _agg(rows_b)

                def _delta(va, vb):
                    if va == 0:
                        return "–"
                    pct = (vb - va) / abs(va) * 100
                    return f"{pct:+.1f}%"

                lines = [
                    f"**Period comparison: {period_a} vs {period_b}**\n",
                    "| Metric | Period A | Period B | Delta |",
                    "|--------|----------|----------|-------|",
                    f"| Traffic Gbps | {a['traffic_gbps']} | {b['traffic_gbps']} | {_delta(a['traffic_gbps'], b['traffic_gbps'])} |",
                    f"| Avg CPU % | {a['avg_cpu']} | {b['avg_cpu']} | {_delta(a['avg_cpu'], b['avg_cpu'])} |",
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
                from zbbx_mcp.classify import detect_provider

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
                    if traffics and max(traffics) > 0:
                        top_pct = max(traffics) / sum(traffics) * 100 if sum(traffics) > 0 else 0
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
                from zbbx_mcp.data import build_value_map

                hosts = await fetch_enabled_hosts(client)

                # VPN check items
                vpn1_items = await client.call("item.get", {
                    "hostids": [h["hostid"] for h in hosts],
                    "output": ["hostid", "lastvalue"],
                    "filter": {"key_": "vpn_primary_check[{HOST.IP}]", "status": "0"},
                })
                vpn_map = build_value_map(vpn1_items, lambda v: int(float(v)))

                # Get trend data for uptime estimation
                all_ids = [h["hostid"] for h in hosts]
                traffic_map = await fetch_traffic_map(client, all_ids)

                # Aggregate by product + country
                agg: dict[str, dict] = {}
                for h in hosts:
                    prod, _ = _classify_host(h.get("groups", []))
                    if product and product.lower() not in (prod or "").lower():
                        continue
                    cc = extract_country(h["host"])
                    if country and cc.lower() != country.lower():
                        continue
                    if not cc or not prod:
                        continue

                    key = f"{prod}|{cc}"
                    entry = agg.setdefault(key, {"product": prod, "cc": cc, "up": 0, "total": 0, "traffic": 0})
                    entry["total"] += 1
                    vpn_val = vpn_map.get(h["hostid"])
                    if vpn_val == 1:
                        entry["up"] += 1
                    entry["traffic"] += traffic_map.get(h["hostid"], 0)

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
                from datetime import datetime, timezone

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
                    path = os.path.expanduser(output)
                    with open(path, "w") as f:
                        f.write(result)
                    return f"Snapshot saved to {path}"

                return result
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"
