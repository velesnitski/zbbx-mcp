"""CEO-grade HTML infrastructure report — single tool, all analytics combined."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

import httpx

from zbbx_mcp.classify import classify_host as _classify_host
from zbbx_mcp.classify import detect_provider
from zbbx_mcp.data import (
    KEY_VPN_PRIMARY,
    build_value_map,
    extract_country,
    fetch_cpu_map,
    fetch_enabled_hosts,
    fetch_traffic_map,
    fetch_trends_batch,
    group_by_country,
    host_ip,
)
from zbbx_mcp.resolver import InstanceResolver

_CSS = """
@page{size:A4;margin:15mm}*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#1a1a2e;background:#f8f9fa;line-height:1.5;font-size:13px}
.page{max-width:1100px;margin:0 auto;padding:40px}
.header{background:linear-gradient(135deg,#1a1a2e 0%,#16213e 50%,#0f3460 100%);color:white;padding:48px 40px;border-radius:16px;margin-bottom:32px;position:relative;overflow:hidden}
.header::after{content:'';position:absolute;top:-50%;right:-20%;width:400px;height:400px;background:radial-gradient(circle,rgba(255,255,255,0.05) 0%,transparent 70%)}
.header h1{font-size:28px;font-weight:700;letter-spacing:-0.5px;margin-bottom:4px}
.header .subtitle{font-size:15px;color:rgba(255,255,255,0.7)}
.header .date{font-size:13px;color:rgba(255,255,255,0.5);margin-top:12px}
.header .kpi-row{display:flex;gap:32px;margin-top:28px;flex-wrap:wrap}
.header .kpi{text-align:center}.header .kpi-value{font-size:32px;font-weight:700}
.header .kpi-label{font-size:11px;text-transform:uppercase;letter-spacing:1px;color:rgba(255,255,255,0.6);margin-top:2px}
.section{background:white;border-radius:12px;padding:28px 32px;margin-bottom:20px;box-shadow:0 1px 3px rgba(0,0,0,0.06)}
.section h2{font-size:18px;font-weight:700;margin-bottom:4px;color:#1a1a2e}.desc{font-size:12px;color:#6b7280;margin-bottom:16px}
h3{font-size:14px;font-weight:600;margin:16px 0 8px;color:#374151}
table{width:100%;border-collapse:collapse;font-size:12px}
th{background:#f3f4f6;color:#6b7280;font-weight:600;text-transform:uppercase;font-size:10px;letter-spacing:0.5px;padding:8px 12px;text-align:left;border-bottom:2px solid #e5e7eb}
td{padding:8px 12px;border-bottom:1px solid #f3f4f6}tr:hover td{background:#f9fafb}.num{text-align:right;font-variant-numeric:tabular-nums}
.badge{display:inline-block;padding:2px 8px;border-radius:9999px;font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:0.3px}
.badge-critical{background:#fef2f2;color:#dc2626}.badge-high{background:#fff7ed;color:#ea580c}
.badge-ok{background:#f0fdf4;color:#16a34a}.badge-dead{background:#f5f5f5;color:#6b7280}
.badge-rising{background:#ecfdf5;color:#059669}.badge-dropping{background:#fef2f2;color:#dc2626}
.badge-stable{background:#f0f9ff;color:#0284c7}.badge-overloaded{background:#fef2f2;color:#dc2626}
.bar-container{width:100%;height:8px;background:#f3f4f6;border-radius:4px;overflow:hidden}
.bar{height:100%;border-radius:4px}.bar-blue{background:linear-gradient(90deg,#3b82f6,#2563eb)}
.bar-green{background:linear-gradient(90deg,#22c55e,#16a34a)}.bar-red{background:linear-gradient(90deg,#ef4444,#dc2626)}
.bar-orange{background:linear-gradient(90deg,#f59e0b,#ea580c)}
.alert{padding:12px 16px;border-radius:8px;margin-bottom:8px;font-size:12px}
.alert-red{background:#fef2f2;border-left:3px solid #dc2626}.alert-orange{background:#fff7ed;border-left:3px solid #ea580c}
.alert-yellow{background:#fffbeb;border-left:3px solid #d97706}.alert-green{background:#f0fdf4;border-left:3px solid #16a34a}
.alert b{color:#1a1a2e}
.grid-2{display:grid;grid-template-columns:1fr 1fr;gap:20px}.grid-3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:20px}
.card{background:#f9fafb;border-radius:8px;padding:16px}.card-title{font-size:11px;text-transform:uppercase;letter-spacing:0.5px;color:#6b7280;font-weight:600;margin-bottom:4px}
.card-value{font-size:24px;font-weight:700;color:#1a1a2e}.card-sub{font-size:11px;color:#9ca3af;margin-top:2px}
.footer{text-align:center;color:#9ca3af;font-size:11px;padding:20px 0 40px}
@media(max-width:800px){.grid-2,.grid-3{grid-template-columns:1fr}}
@media print{body{background:white;font-size:11px}.page{padding:0}.section{box-shadow:none;border:1px solid #e5e7eb;break-inside:avoid}.header{border-radius:0}}
"""

_COUNTRY_NAMES = {
    "DE": "Germany", "NL": "Netherlands", "US": "United States", "RU": "Russia",
    "FR": "France", "TR": "Turkey", "MX": "Mexico", "IN": "India", "IT": "Italy",
    "GB": "United Kingdom", "BR": "Brazil", "JP": "Japan", "AZ": "Azerbaijan",
    "IL": "Israel", "ID": "Indonesia", "CA": "Canada", "KZ": "Kazakhstan",
    "BY": "Belarus", "UA": "Ukraine", "SE": "Sweden", "NO": "Norway",
    "PL": "Poland", "RO": "Romania", "CZ": "Czech Rep.", "GE": "Georgia",
    "AU": "Australia", "SG": "Singapore", "AE": "UAE", "FI": "Finland",
    "AT": "Austria", "CH": "Switzerland", "ES": "Spain", "DK": "Denmark",
    "IE": "Ireland", "HR": "Croatia", "RS": "Serbia", "PE": "Peru",
    "PY": "Paraguay", "AR": "Argentina", "UZ": "Uzbekistan", "AM": "Armenia",
    "BE": "Belgium", "AL": "Albania", "HU": "Hungary", "SK": "Slovakia",
    "GR": "Greece", "EE": "Estonia", "LV": "Latvia", "PT": "Portugal",
}


def _badge(cls: str, text: str) -> str:
    return f'<span class="badge badge-{cls}">{text}</span>'


def _card(title: str, value: str, sub: str = "") -> str:
    return f'<div class="card"><div class="card-title">{title}</div><div class="card-value">{value}</div><div class="card-sub">{sub}</div></div>'


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()) -> None:

    if "generate_ceo_report" not in skip:

        @mcp.tool()
        async def generate_ceo_report(
            period: str = "30d",
            deep_dive_country: str = "",
            output_dir: str = "",
            instance: str = "",
        ) -> str:
            """Generate CEO-grade HTML infrastructure report with all analytics.

            Args:
                period: Trend period (default: 30d)
                deep_dive_country: Force a country deep dive section (2-letter code, optional)
                output_dir: Output directory (default: ~/Downloads)
                instance: Zabbix instance name (optional)
            """
            try:
                client = resolver.resolve(instance)
                now = datetime.now(timezone.utc)
                now_str = now.strftime("%Y-%m-%d %H:%M UTC")
                date_str = now.strftime("%Y-%m-%d")

                                hosts = await fetch_enabled_hosts(client)
                all_ids = [h["hostid"] for h in hosts]

                # Batch trends in chunks to avoid Zabbix 500
                chunk_size = 200
                trend_rows = []
                for i in range(0, len(all_ids), chunk_size):
                    chunk = all_ids[i:i + chunk_size]
                    rows, _ = await fetch_trends_batch(client, chunk, ["traffic"], period)
                    trend_rows.extend(rows)

                traffic_map, cpu_map = await asyncio.gather(
                    fetch_traffic_map(client, all_ids),
                    fetch_cpu_map(client, all_ids),
                )

                # VPN check
                vpn_map: dict[str, int] = {}
                if KEY_VPN_PRIMARY:
                    vpn_items = await client.call("item.get", {
                        "hostids": all_ids,
                        "output": ["hostid", "lastvalue"],
                        "filter": {"key_": KEY_VPN_PRIMARY, "status": "0"},
                    })
                    vpn_map = build_value_map(vpn_items, lambda v: int(float(v)))

                                by_country = group_by_country(hosts)
                total_traffic = round(sum(traffic_map.values()) / 1000, 1)
                avg_cpu = round(sum(cpu_map.values()) / len(cpu_map), 1) if cpu_map else 0
                total_servers = len(hosts)
                total_countries = len(by_country)

                products = set()
                providers = set()
                for h in hosts:
                    prod, _ = _classify_host(h.get("groups", []))
                    if prod and prod != "Unknown":
                        products.add(prod)
                    ip = host_ip(h)
                    if ip:
                        p = detect_provider(ip)
                        if p not in ("Unknown", "Other"):
                            providers.add(p)

                # Country traffic + trends
                country_data: dict[str, dict] = {}
                for cc, cc_hosts in by_country.items():
                    traffic = sum(traffic_map.get(h["hostid"], 0) for h in cc_hosts)
                    cpus = [cpu_map[h["hostid"]] for h in cc_hosts if h["hostid"] in cpu_map]
                    vpn_up = sum(1 for h in cc_hosts if vpn_map.get(h["hostid"]) == 1)
                    vpn_total = sum(1 for h in cc_hosts if h["hostid"] in vpn_map)
                    country_data[cc] = {
                        "servers": len(cc_hosts), "traffic_gbps": round(traffic / 1000, 1),
                        "avg_cpu": round(sum(cpus) / len(cpus), 1) if cpus else 0,
                        "vpn_up": vpn_up, "vpn_total": vpn_total,
                    }

                # Aggregate trends by country using TrendRow.avg and .current
                country_avg: dict[str, float] = {}   # cc -> sum of avg Mbps
                country_now: dict[str, float] = {}   # cc -> sum of current Mbps
                country_daily: dict[str, dict[str, float]] = {}
                for tr in trend_rows:
                    cc = extract_country(tr.hostname)
                    if not cc or tr.metric != "traffic":
                        continue
                    country_avg[cc] = country_avg.get(cc, 0) + tr.avg
                    country_now[cc] = country_now.get(cc, 0) + tr.current
                    if tr.daily:
                        ct = country_daily.setdefault(cc, {})
                        for day, val in tr.daily.items():
                            ct[day] = ct.get(day, 0) + val

                # Compute trend direction + change per country
                for cc, cd in country_data.items():
                    avg_gbps = country_avg.get(cc, 0) / 1000
                    now_gbps = country_now.get(cc, 0) / 1000
                    cd["avg_gbps"] = round(avg_gbps, 1)
                    # Override traffic_gbps with trend-sourced current for consistency
                    if now_gbps > 0:
                        cd["traffic_gbps"] = round(now_gbps, 1)

                    # Change: current vs avg from same data source
                    if avg_gbps > 0:
                        cd["change"] = round((now_gbps - avg_gbps) / avg_gbps * 100)
                    else:
                        cd["change"] = 0

                    ct = country_daily.get(cc, {})
                    if ct:
                        days = sorted(ct.items())
                        if len(days) >= 4 and avg_gbps >= 0.05:
                            q = max(len(days) // 4, 1)
                            older = sum(v for _, v in days[:q]) / q
                            recent = sum(v for _, v in days[-q:]) / q
                            if older > 0:
                                dir_pct = (recent - older) / older * 100
                                cd["trend"] = "rising" if dir_pct > 15 else "dropping" if dir_pct < -15 else "stable"
                            else:
                                cd["trend"] = "rising" if recent > 0 else "stable"
                        else:
                            cd["trend"] = "stable"
                    else:
                        cd["trend"] = "stable"

                    # Sanity: trend label must match change direction
                    change = cd["change"]
                    if change < -30 and cd["trend"] in ("stable", "rising"):
                        cd["trend"] = "dropping"
                    elif change > 30 and cd["trend"] in ("stable", "dropping"):
                        cd["trend"] = "rising"
                    elif change < -10 and cd["trend"] == "rising" or change > 0 and cd["trend"] == "dropping":
                        cd["trend"] = "stable"
                    if cd["traffic_gbps"] < 0.01 and avg_gbps > 0.05:
                        cd["trend"] = "dead"

                sorted_countries = sorted(country_data.items(), key=lambda x: -x[1]["traffic_gbps"])
                top_countries = sorted_countries[:18]
                max_traffic = max((cd["traffic_gbps"] for _, cd in top_countries), default=1)

                                html = [f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Infrastructure Report — {now.strftime("%B %d, %Y")}</title>
<style>{_CSS}</style></head><body><div class="page">

<!-- HEADER -->
<div class="header">
<h1>Infrastructure Status Report</h1>
<div class="subtitle">Server Fleet Overview &amp; Strategic Recommendations</div>
<div class="date">{now_str} &bull; zbbx-mcp v1.3</div>
<div class="kpi-row">
<div class="kpi"><div class="kpi-value">{total_servers}</div><div class="kpi-label">Servers</div></div>
<div class="kpi"><div class="kpi-value">{total_traffic} Gbps</div><div class="kpi-label">Total Traffic</div></div>
<div class="kpi"><div class="kpi-value">{total_countries}</div><div class="kpi-label">Countries</div></div>
<div class="kpi"><div class="kpi-value">{len(products)}</div><div class="kpi-label">Products</div></div>
<div class="kpi"><div class="kpi-value">{len(providers)}</div><div class="kpi-label">Providers</div></div>
<div class="kpi"><div class="kpi-value">{avg_cpu}%</div><div class="kpi-label">Avg CPU</div></div>
</div></div>"""]

                                alerts = []
                # Dead countries
                dead = [(cc, cd) for cc, cd in sorted_countries if cd["trend"] == "dead"]
                for cc, cd in dead:
                    name = _COUNTRY_NAMES.get(cc, cc)
                    alerts.append(("red", f"<b>{name}: Complete blackout.</b> {cd['servers']} servers, 0 Gbps traffic. Investigate or decommission."))

                # VPN failures
                for cc, cd in sorted_countries:
                    if cd["vpn_total"] > 0 and cd["vpn_up"] < cd["vpn_total"] * 0.7 and cd["traffic_gbps"] > 0.5:
                        name = _COUNTRY_NAMES.get(cc, cc)
                        pct = round(cd["vpn_up"] / cd["vpn_total"] * 100)
                        down = cd["vpn_total"] - cd["vpn_up"]
                        alerts.append(("red" if pct < 60 else "orange",
                                       f"<b>{name}: VPN uptime {pct}%.</b> {down}/{cd['vpn_total']} servers DOWN. {cd['traffic_gbps']} Gbps at risk."))

                # Explosive growth
                for cc, cd in sorted_countries:
                    if cd.get("change", 0) > 100 and cd["traffic_gbps"] > 1:
                        name = _COUNTRY_NAMES.get(cc, cc)
                        alerts.append(("orange", f"<b>{name}: Explosive growth +{cd['change']}%.</b> Traffic now {cd['traffic_gbps']} Gbps. Monitor for capacity saturation."))

                # Dropping traffic
                for cc, cd in sorted_countries:
                    if cd.get("change", 0) < -30 and cd.get("avg_gbps", 0) > 1 and cd["trend"] != "dead":
                        name = _COUNTRY_NAMES.get(cc, cc)
                        alerts.append(("yellow", f"<b>{name}: Traffic declining {cd['change']}%.</b> Now {cd['traffic_gbps']} Gbps (avg {cd['avg_gbps']}). Possible geo-blocking."))

                if not alerts:
                    alerts.append(("green", "<b>All systems healthy.</b> No critical issues detected."))

                html.append('<div class="section"><h2>Executive Summary</h2><div class="desc">Key issues requiring attention</div>')
                for cls, text in alerts[:8]:
                    html.append(f'<div class="alert alert-{cls}">{text}</div>')
                html.append('</div>')

                                html.append(f'<div class="section"><h2>Traffic by Country ({period})</h2>')
                html.append(f'<div class="desc">{len(by_country)} countries. Total fleet throughput: {total_traffic} Gbps.</div>')
                html.append('<table><thead><tr><th>Country</th><th class="num">Servers</th><th class="num">Avg</th><th class="num">Now</th><th>Trend</th><th class="num">Change</th><th style="width:200px">Traffic</th></tr></thead><tbody>')

                for cc, cd in top_countries:
                    name = _COUNTRY_NAMES.get(cc, cc)
                    pct = cd["traffic_gbps"] / max_traffic * 100 if max_traffic > 0 else 0
                    trend = cd.get("trend", "stable")
                    change = cd.get("change", 0)
                    bar_cls = "bar-red" if trend == "dead" else "bar-orange" if trend == "dropping" else "bar-green" if change > 50 else "bar-blue"
                    change_color = "#dc2626" if change < -10 else "#16a34a" if change > 5 else "#6b7280"
                    html.append(
                        f'<tr><td><b>{name}</b></td><td class="num">{cd["servers"]}</td>'
                        f'<td class="num">{cd.get("avg_gbps", 0)} Gbps</td><td class="num">{cd["traffic_gbps"]} Gbps</td>'
                        f'<td>{_badge(trend, trend.title())}</td>'
                        f'<td class="num" style="color:{change_color}">{change:+d}%</td>'
                        f'<td><div class="bar-container"><div class="bar {bar_cls}" style="width:{pct:.0f}%"></div></div></td></tr>'
                    )
                html.append('</tbody></table></div>')

                                sla_rows = []
                for cc, cd in sorted_countries:
                    if cd["vpn_total"] > 0:
                        uptime = round(cd["vpn_up"] / cd["vpn_total"] * 100, 1)
                        sla_rows.append((cc, cd, uptime))
                sla_rows.sort(key=lambda x: x[2])

                html.append('<div class="section"><h2>VPN Uptime by Country</h2><div class="desc">Based on VPN primary check status</div>')
                html.append('<table><thead><tr><th>Country</th><th class="num">Uptime</th><th class="num">Servers</th><th class="num">Down</th><th class="num">Traffic</th></tr></thead><tbody>')
                for cc, cd, uptime in sla_rows[:15]:
                    name = _COUNTRY_NAMES.get(cc, cc)
                    down = cd["vpn_total"] - cd["vpn_up"]
                    cls = "critical" if uptime < 50 else "high" if uptime < 80 else "ok" if uptime == 100 else "stable"
                    html.append(
                        f'<tr><td><b>{name}</b></td><td class="num">{_badge(cls, f"{uptime}%")}</td>'
                        f'<td class="num">{cd["vpn_total"]}</td><td class="num">{down}</td>'
                        f'<td class="num">{cd["traffic_gbps"]} Gbps</td></tr>'
                    )
                html.append('</tbody></table></div>')

                                capacity_rows = []
                for cc, cd in sorted_countries:
                    if cd["servers"] > 0 and cd["traffic_gbps"] > 0.1:
                        density = round(cd["traffic_gbps"] * 1000 / cd["servers"], 1)
                        status = "OVERLOADED" if density > 3000 else "HIGH" if density > 1500 else "OK"
                        capacity_rows.append((cc, cd, density, status))
                capacity_rows.sort(key=lambda x: -x[2])

                html.append('<div class="section"><h2>Capacity Planning</h2><div class="desc">Mbps per server — higher = closer to saturation</div>')
                html.append('<table><thead><tr><th>Country</th><th class="num">Servers</th><th class="num">Traffic</th><th class="num">Mbps/srv</th><th>Status</th></tr></thead><tbody>')
                for cc, cd, density, status in capacity_rows[:15]:
                    name = _COUNTRY_NAMES.get(cc, cc)
                    cls = "critical" if status == "OVERLOADED" else "high" if status == "HIGH" else "ok"
                    html.append(
                        f'<tr><td><b>{name}</b></td><td class="num">{cd["servers"]}</td>'
                        f'<td class="num">{cd["traffic_gbps"]} Gbps</td><td class="num">{density}</td>'
                        f'<td>{_badge(cls, status)}</td></tr>'
                    )
                html.append('</tbody></table></div>')

                                risk_rows = []
                for cc, cc_hosts in by_country.items():
                    if len(cc_hosts) < 1:
                        continue
                    score = 0
                    risk = "OK"
                    provs = [detect_provider(host_ip(h)) for h in cc_hosts if host_ip(h)]
                    if provs:
                        top_prov_pct = max(provs.count(p) for p in set(provs)) / len(provs) * 100
                        if top_prov_pct > 80:
                            score += 30
                            risk = "single provider"
                    if len(cc_hosts) == 1:
                        score += 25
                        risk = "no redundancy"
                    cpus = [cpu_map[h["hostid"]] for h in cc_hosts if h["hostid"] in cpu_map]
                    if cpus and sum(cpus) / len(cpus) > 80:
                        score += 30
                        risk = "CPU >80%"
                    risk_rows.append((cc, score, len(cc_hosts), risk))
                risk_rows.sort(key=lambda x: -x[1])

                html.append('<div class="section"><h2>Risk Assessment</h2><div class="desc">Composite score: provider concentration, redundancy, capacity</div>')
                html.append('<table><thead><tr><th>Country</th><th class="num">Score</th><th class="num">Servers</th><th>Top Risk</th></tr></thead><tbody>')
                for cc, score, srvs, risk in risk_rows[:12]:
                    if score < 30:
                        break
                    name = _COUNTRY_NAMES.get(cc, cc)
                    cls = "critical" if score >= 80 else "high" if score >= 55 else "stable"
                    html.append(
                        f'<tr><td><b>{name}</b></td><td class="num">{_badge(cls, f"{score}/100")}</td>'
                        f'<td class="num">{srvs}</td><td>{risk}</td></tr>'
                    )
                html.append('</tbody></table></div>')

                                product_counts: dict[str, dict] = {}
                for h in hosts:
                    prod, tier = _classify_host(h.get("groups", []))
                    if prod and prod != "Unknown":
                        key = prod
                        pc = product_counts.setdefault(key, {"total": 0, "tiers": {}})
                        pc["total"] += 1
                        pc["tiers"][tier] = pc["tiers"].get(tier, 0) + 1

                html.append('<div class="section"><h2>Fleet Composition</h2>')
                html.append(f'<div class="desc">{total_servers} servers across {len(product_counts)} products</div>')
                html.append('<div class="grid-3">')
                for prod in sorted(product_counts, key=lambda p: -product_counts[p]["total"]):
                    pc = product_counts[prod]
                    tier_str = " &bull; ".join(f"{t}: {c}" for t, c in sorted(pc["tiers"].items(), key=lambda x: -x[1]))
                    html.append(_card(prod, str(pc["total"]), tier_str))
                html.append('</div></div>')

                                dead_servers = []
                broken_servers = []
                idle_servers = []
                _SKIP_PRODUCTS = {"Monitoring", "Infrastructure", "Unknown"}
                for h in hosts:
                    hid = h["hostid"]
                    if hid not in traffic_map:
                        continue  # no traffic data — can't classify (cluster secondary, etc.)
                    traffic = traffic_map[hid]
                    cpu = cpu_map.get(hid, 0)
                    vpn_val = vpn_map.get(hid)
                    hostname = h["host"]
                    prod, tier = _classify_host(h.get("groups", []))
                    if prod in _SKIP_PRODUCTS:
                        continue
                    ip = host_ip(h)
                    prov = detect_provider(ip) if ip else "?"

                    if traffic < 0.1 and cpu < 2:
                        dead_servers.append((hostname, prod, prov, cpu, traffic, vpn_val))
                    elif vpn_val == 0 and traffic < 5:
                        broken_servers.append((hostname, prod, prov, cpu, traffic))
                    elif traffic < 5 and traffic > 0 and cpu < 10:
                        idle_servers.append((hostname, prod, prov, cpu, traffic, vpn_val))

                if dead_servers or broken_servers or idle_servers:
                    html.append('<div class="section"><h2>Waste Reduction &mdash; Shutdown Candidates</h2>')
                    html.append(f'<div class="desc">{len(dead_servers) + len(broken_servers) + len(idle_servers)} servers for decommission or investigation</div>')
                    html.append('<div class="grid-3">')
                    html.append(_card("Dead (0 traffic)", f'<span style="color:#dc2626">{len(dead_servers)}</span>', "Can shut down immediately"))
                    html.append(_card("Broken (VPN DOWN)", f'<span style="color:#ea580c">{len(broken_servers)}</span>', "Need fix or shutdown"))
                    html.append(_card("Idle (<5 Mbps)", f'<span style="color:#d97706">{len(idle_servers)}</span>', "Review before shutdown"))
                    html.append('</div>')

                    html.append('<table><thead><tr><th>Category</th><th>Server</th><th>Product</th><th>Provider</th><th class="num">CPU</th><th class="num">Traffic</th><th>VPN</th></tr></thead><tbody>')
                    for hostname, prod, prov, cpu, traffic, vpn_val in dead_servers[:6]:
                        vpn_str = "DOWN" if vpn_val == 0 else ("OK" if vpn_val == 1 else "&ndash;")
                        html.append(f'<tr><td>{_badge("critical", "Dead")}</td><td>{hostname}</td><td>{prod}</td><td>{prov}</td><td class="num">{cpu}%</td><td class="num">{traffic:.1f} Mbps</td><td>{vpn_str}</td></tr>')
                    for hostname, prod, prov, cpu, traffic in broken_servers[:4]:
                        html.append(f'<tr><td>{_badge("high", "Broken")}</td><td>{hostname}</td><td>{prod}</td><td>{prov}</td><td class="num">{cpu}%</td><td class="num">{traffic:.1f} Mbps</td><td>DOWN</td></tr>')
                    for hostname, prod, prov, cpu, traffic, vpn_val in idle_servers[:4]:
                        vpn_str = "OK" if vpn_val == 1 else "&ndash;"
                        html.append(f'<tr><td>{_badge("stable", "Idle")}</td><td>{hostname}</td><td>{prod}</td><td>{prov}</td><td class="num">{cpu}%</td><td class="num">{traffic:.1f} Mbps</td><td>{vpn_str}</td></tr>')
                    remaining = len(dead_servers) + len(broken_servers) + len(idle_servers) - 14
                    if remaining > 0:
                        html.append(f'<tr><td colspan="7" style="color:#9ca3af;font-size:11px">+ {remaining} more not shown</td></tr>')
                    html.append('</tbody></table>')
                    html.append('<div class="alert alert-yellow" style="margin-top:12px"><b>Manual review needed.</b> Verify each server before shutdown &mdash; some may be standby replicas or recently deployed.</div>')
                    html.append('</div>')

                                # Auto-detect countries needing detailed analysis
                deep_dive_countries = []
                for cc, cd in sorted_countries:
                    reasons = []
                    if cd["trend"] == "dead":
                        reasons.append("dead")
                    if cd.get("change", 0) > 100 and cd["traffic_gbps"] > 1:
                        reasons.append("explosive growth")
                    if cd["vpn_total"] > 0 and cd["vpn_up"] < cd["vpn_total"] * 0.7:
                        reasons.append("VPN issues")
                    if cd.get("change", 0) < -30 and cd.get("avg_gbps", 0) > 0.5:
                        reasons.append("traffic drop")
                    # Countries with no VPN monitoring coverage
                    cc_hosts = by_country.get(cc, [])
                    vpn_checked = sum(1 for h in cc_hosts if h["hostid"] in vpn_map)
                    if vpn_checked == 0 and len(cc_hosts) > 2:
                        infra_count = sum(1 for h in cc_hosts if _classify_host(h.get("groups", []))[0] == "Infrastructure")
                        if infra_count == len(cc_hosts):
                            reasons.append("infra only")
                        else:
                            reasons.append("no VPN checks")
                    if reasons:
                        deep_dive_countries.append((cc, cd, reasons, cc_hosts))

                # Force-add requested deep dive country if not already present
                if deep_dive_country:
                    ddc = deep_dive_country.upper()
                    if ddc not in {cc for cc, _, _, _ in deep_dive_countries}:
                        if ddc in country_data:
                            dd_hosts = by_country.get(ddc, [])
                            deep_dive_countries.append((ddc, country_data[ddc], ["requested"], dd_hosts))

                # Sort deep dives: critical issues first, then by traffic
                _REASON_PRIORITY = {"dead": 0, "VPN issues": 1, "traffic drop": 2, "no VPN checks": 3, "infra only": 3, "requested": 3, "explosive growth": 4}
                deep_dive_countries.sort(key=lambda x: (min(_REASON_PRIORITY.get(r, 5) for r in x[2]), -x[1]["traffic_gbps"]))

                if deep_dive_countries:
                    html.append('<div class="section"><h2>Country Deep Dives</h2>')
                    html.append('<div class="desc">Countries requiring detailed analysis</div>')
                    for cc, cd, reasons, cc_hosts in deep_dive_countries[:8]:
                        name = _COUNTRY_NAMES.get(cc, cc)
                        reason_badges = " ".join(_badge("critical" if r in ("dead", "VPN issues") else "high" if r == "traffic drop" else "stable", r) for r in reasons)

                        # Build detail grid
                        html.append(f'<h3>{name} {reason_badges}</h3>')
                        html.append('<div class="grid-3" style="margin-bottom:8px">')
                        html.append(_card("Servers", str(cd["servers"]), f"{cd['traffic_gbps']} Gbps total"))

                        # Provider breakdown for this country
                        cc_provs: dict[str, int] = {}
                        for h in cc_hosts:
                            p = detect_provider(host_ip(h)) if host_ip(h) else "?"
                            cc_provs[p] = cc_provs.get(p, 0) + 1
                        prov_str = ", ".join(f"{p} ({c})" for p, c in sorted(cc_provs.items(), key=lambda x: -x[1])[:3])
                        html.append(_card("Providers", str(len(cc_provs)), prov_str))

                        # VPN status
                        if cd["vpn_total"] > 0:
                            uptime = round(cd["vpn_up"] / cd["vpn_total"] * 100)
                            vpn_sub = f"{cd['vpn_up']}/{cd['vpn_total']} UP"
                            html.append(_card("VPN Uptime", f"{uptime}%", vpn_sub))
                        else:
                            html.append(_card("VPN Status", "N/A", "No VPN check items"))
                        html.append('</div>')

                        # Cluster analysis
                        from collections import defaultdict as _defaultdict
                        clusters: dict[str, list] = _defaultdict(list)
                        for h in cc_hosts:
                            base = h.get("host", "").split()[0]
                            clusters[base].append(h)
                        multi_clusters = {k: v for k, v in clusters.items() if len(v) > 1}

                        if multi_clusters:
                            html.append('<table><thead><tr><th>Cluster</th><th class="num">Members</th><th>Product</th><th class="num">Primary Traffic</th><th>Provider</th></tr></thead><tbody>')
                            for base, members in sorted(multi_clusters.items(), key=lambda x: -len(x[1])):
                                primary = members[0]
                                prod, _ = _classify_host(primary.get("groups", []))
                                primary_traffic = traffic_map.get(primary["hostid"], 0)
                                prov = detect_provider(host_ip(primary)) if host_ip(primary) else "?"
                                html.append(f'<tr><td><b>{base}</b></td><td class="num">{len(members)}</td><td>{prod}</td><td class="num">{primary_traffic:.1f} Mbps</td><td>{prov}</td></tr>')
                            html.append('</tbody></table>')

                        # Recommendation
                        rec = ""
                        if "dead" in reasons:
                            rec = f"All {cd['servers']} servers offline. Investigate or decommission."
                        elif "infra only" in reasons:
                            rec = f"Only tunneling infrastructure ({cd['servers']} servers). Add VPN servers to serve {name} users directly."
                        elif "VPN issues" in reasons:
                            down = cd["vpn_total"] - cd["vpn_up"]
                            rec = f"{down} VPN servers DOWN. Investigate blocked IPs or service failures."
                        elif "explosive growth" in reasons:
                            rec = f"Traffic +{cd.get('change', 0)}%. Monitor capacity — may need more servers soon."
                        elif "traffic drop" in reasons:
                            rec = f"Traffic declined {cd.get('change', 0)}%. Check for geo-blocking or routing issues."
                        elif "no VPN checks" in reasons:
                            rec = f"{cd['servers']} servers without VPN health monitoring. Add standard check items."
                        elif "requested" in reasons:
                            rec = f"Manual review requested. {cd['servers']} servers, {cd['traffic_gbps']} Gbps."
                        if rec:
                            html.append(f'<div class="alert alert-yellow" style="margin-top:8px"><b>Recommendation:</b> {rec}</div>')

                    html.append('</div>')

                                prov_counts: dict[str, int] = {}
                for h in hosts:
                    ip = host_ip(h)
                    if not ip:
                        continue  # skip hosts without IP (monitoring endpoints)
                    p = detect_provider(ip)
                    prov_counts[p] = prov_counts.get(p, 0) + 1
                prov_sorted = sorted(prov_counts.items(), key=lambda x: -x[1])
                prov_total = sum(c for _, c in prov_sorted)
                top_prov = prov_sorted[0] if prov_sorted else ("?", 0)

                html.append('<div class="section"><h2>Provider Distribution</h2>')
                html.append(f'<div class="desc">{prov_total} servers across {len(prov_counts)} providers</div>')
                # Stacked bar
                html.append('<div style="display:flex;height:24px;border-radius:6px;overflow:hidden;margin:12px 0">')
                colors = ["#6366f1", "#3b82f6", "#8b5cf6", "#f59e0b", "#22c55e", "#ef4444", "#06b6d4", "#ec4899", "#9ca3af"]
                for i, (prov, cnt) in enumerate(prov_sorted[:8]):
                    w = cnt / prov_total * 100
                    html.append(f'<div style="width:{w:.1f}%;background:{colors[i % len(colors)]}" title="{prov}: {cnt}"></div>')
                if len(prov_sorted) > 8:
                    rest = sum(c for _, c in prov_sorted[8:])
                    html.append(f'<div style="width:{rest/prov_total*100:.1f}%;background:#d1d5db" title="Others: {rest}"></div>')
                html.append('</div><div style="font-size:11px;color:#6b7280;margin-bottom:12px">')
                for i, (prov, cnt) in enumerate(prov_sorted[:8]):
                    html.append(f'<span style="color:{colors[i % len(colors)]}">&#9632;</span> {prov} ({cnt}) &nbsp; ')
                html.append('</div>')
                if top_prov[1] / prov_total > 0.25:
                    html.append(f'<div class="alert alert-yellow"><b>Concentration risk:</b> {top_prov[0]} hosts {top_prov[1]} servers ({top_prov[1]*100//prov_total}%). Consider diversifying.</div>')
                html.append('</div>')

                                from zbbx_mcp.data import REGION_MAP
                html.append('<div class="section"><h2>Expansion Opportunities</h2><div class="desc">Regional analysis: where to invest for growth</div>')
                for region_name, region_label in [("LATAM", "LATAM &mdash; Growth Potential"), ("APAC", "APAC &mdash; Capacity Constrained"), ("EMEA", "EMEA &mdash; Core Markets")]:
                    region_codes = set(REGION_MAP.get(region_name, []))
                    region_rows = [(cc, cd) for cc, cd in sorted_countries if cc in region_codes and cd["traffic_gbps"] > 0.01]
                    if not region_rows:
                        continue
                    html.append(f'<h3>{region_label}</h3>')
                    html.append('<table><thead><tr><th>Country</th><th class="num">Servers</th><th class="num">Traffic</th><th class="num">Mbps/srv</th><th>Status</th></tr></thead><tbody>')
                    for cc, cd in sorted(region_rows, key=lambda x: -x[1]["traffic_gbps"])[:6]:
                        name = _COUNTRY_NAMES.get(cc, cc)
                        density = round(cd["traffic_gbps"] * 1000 / cd["servers"], 1) if cd["servers"] > 0 else 0
                        status = "OVERLOADED" if density > 3000 else "HIGH" if density > 1500 else "OK" if density > 50 else "LOW"
                        cls = "critical" if status == "OVERLOADED" else "high" if status == "HIGH" else "ok" if status == "OK" else "stable"
                        html.append(f'<tr><td><b>{name}</b></td><td class="num">{cd["servers"]}</td><td class="num">{cd["traffic_gbps"]} Gbps</td><td class="num">{density}</td><td>{_badge(cls, status)}</td></tr>')
                    # Missing countries in region
                    missing = sorted(region_codes - {cc for cc, _ in sorted_countries})
                    if missing[:5]:
                        names = ", ".join(_COUNTRY_NAMES.get(c, c) for c in missing[:5])
                        html.append(f'<tr><td colspan="5" style="color:#9ca3af;font-size:11px">No servers: {names}</td></tr>')
                    html.append('</tbody></table>')
                html.append('</div>')

                                recs_immediate = []
                recs_short = []
                recs_medium = []

                # Auto-generate from data
                for cc, cd in sorted_countries:
                    if cd["trend"] == "dead" and cd["servers"] > 2:
                        name = _COUNTRY_NAMES.get(cc, cc)
                        recs_immediate.append((f"<b>{name}:</b> Investigate {cd['servers']} dead servers or decommission", "Recover/save", "Low"))
                    if cd["vpn_total"] > 0 and cd["vpn_up"] < cd["vpn_total"] * 0.6 and cd["traffic_gbps"] > 0.5:
                        name = _COUNTRY_NAMES.get(cc, cc)
                        down = cd["vpn_total"] - cd["vpn_up"]
                        recs_immediate.append((f"<b>{name}:</b> Fix {down} VPN-DOWN servers ({cd['traffic_gbps']} Gbps at risk)", f"Recover ~{cd['traffic_gbps']} Gbps", "Low"))
                if dead_servers:
                    recs_immediate.append((f"<b>Shutdown {len(dead_servers)} dead servers</b> (0 traffic, 0 CPU)", "Cost reduction", "Trivial"))

                for cc, cd in sorted_countries:
                    density = cd["traffic_gbps"] * 1000 / cd["servers"] if cd["servers"] > 0 else 0
                    if density > 3000 and cd["traffic_gbps"] > 5:
                        name = _COUNTRY_NAMES.get(cc, cc)
                        recs_short.append((f"<b>{name}:</b> Add servers &mdash; {cd['servers']} handling {cd['traffic_gbps']} Gbps ({density:.0f} Mbps/srv)", "Prevent outage", "Medium"))

                missing_latam = sorted(set(REGION_MAP.get("LATAM", [])) - {cc for cc, _ in sorted_countries})
                if missing_latam:
                    names = ", ".join(_COUNTRY_NAMES.get(c, c) for c in missing_latam[:3])
                    recs_medium.append((f"<b>LATAM expansion:</b> Add servers in {names}", "New market", "High"))
                missing_apac = sorted(set(REGION_MAP.get("APAC", [])) - {cc for cc, _ in sorted_countries})
                if missing_apac:
                    names = ", ".join(_COUNTRY_NAMES.get(c, c) for c in missing_apac[:3])
                    recs_medium.append((f"<b>APAC expansion:</b> Add servers in {names}", "Growth", "High"))

                if recs_immediate or recs_short or recs_medium:
                    html.append('<div class="section"><h2>Strategic Recommendations</h2><div class="desc">Prioritized actions</div>')
                    for label, recs in [("Immediate (This Week)", recs_immediate), ("Short-term (2&ndash;4 Weeks)", recs_short), ("Medium-term (1&ndash;3 Months)", recs_medium)]:
                        if not recs:
                            continue
                        html.append(f'<h3>{label}</h3>')
                        html.append('<table><thead><tr><th>#</th><th>Action</th><th>Impact</th><th>Effort</th></tr></thead><tbody>')
                        for i, (action, impact, effort) in enumerate(recs[:5], 1):
                            html.append(f'<tr><td>{i}</td><td>{action}</td><td>{impact}</td><td>{effort}</td></tr>')
                        html.append('</tbody></table>')
                    html.append('</div>')

                                # Find dead/dropping countries and check if neighbors absorbed traffic
                redistribution = []
                for cc, cd in sorted_countries:
                    if cd["trend"] not in ("dead", "dropping"):
                        continue
                    if cd.get("avg_gbps", 0) < 0.1:
                        continue
                    name = _COUNTRY_NAMES.get(cc, cc)
                    lost_gbps = round(cd.get("avg_gbps", 0) - cd["traffic_gbps"], 1)
                    if lost_gbps <= 0:
                        continue

                    # Check if any country in same region gained traffic
                    from zbbx_mcp.data import REGION_MAP
                    cc_region = ""
                    for rname, rcodes in REGION_MAP.items():
                        if cc in rcodes:
                            cc_region = rname
                            break
                    neighbors_gained = []
                    if cc_region:
                        for ncc, ncd in sorted_countries:
                            if ncc == cc or ncc not in REGION_MAP.get(cc_region, []):
                                continue
                            if ncd.get("change", 0) > 15 and ncd["traffic_gbps"] > 0.1:
                                neighbors_gained.append((_COUNTRY_NAMES.get(ncc, ncc), ncd.get("change", 0)))

                    if neighbors_gained:
                        absorbed = ", ".join(f"{n} +{c}%" for n, c in neighbors_gained[:3])
                        redistribution.append((name, lost_gbps, cd["trend"], f"Partial redirect to: {absorbed}"))
                    else:
                        redistribution.append((name, lost_gbps, cd["trend"], "Traffic lost &mdash; users likely churned, no regional redirect detected"))

                if redistribution:
                    html.append('<div class="section"><h2>Traffic Redistribution Analysis</h2>')
                    html.append('<div class="desc">When servers go down, where does the traffic go?</div>')
                    html.append('<table><thead><tr><th>Country</th><th>Status</th><th class="num">Lost Gbps</th><th>Where Traffic Went</th></tr></thead><tbody>')
                    for name, lost, trend, where in redistribution:
                        cls = "critical" if trend == "dead" else "dropping"
                        html.append(f'<tr><td><b>{name}</b></td><td>{_badge(cls, trend.title())}</td><td class="num">{lost}</td><td>{where}</td></tr>')
                    html.append('</tbody></table>')
                    html.append('<div class="alert alert-yellow" style="margin-top:12px">')
                    html.append('<b>Key insight:</b> Traffic does not automatically redistribute geographically. ')
                    html.append('When servers go down in one country, those users are <b>lost</b> &mdash; ')
                    html.append('they do not connect to servers in other countries. ')
                    html.append('Every day of downtime = permanent user loss. Proactive blocking detection is critical.')
                    html.append('</div></div>')

                                html.append('<div class="section"><h2>Status Legend</h2><div class="desc">How to read the severity labels in this report</div>')
                html.append('<table><thead><tr><th>Status</th><th>Meaning</th><th>Business Impact</th><th>Recommended Action</th></tr></thead><tbody>')
                html.append(f'<tr><td>{_badge("critical", "CRITICAL")}</td><td>VPN service DOWN, active users affected</td><td>Users cannot connect, revenue impact</td><td>Fix within 24h &mdash; rotate IPs or switch protocol</td></tr>')
                html.append(f'<tr><td>{_badge("dead", "DEAD")}</td><td>All servers offline 30+ days, zero traffic</td><td>Country fully lost, paying for idle servers</td><td>Decommission or rotate to save costs</td></tr>')
                html.append(f'<tr><td>{_badge("dropping", "DROPPING")}</td><td>Traffic declining &gt;15% vs average</td><td>Users leaving or blocked by ISP</td><td>Investigate geo-blocking or routing issues</td></tr>')
                html.append(f'<tr><td>{_badge("overloaded", "OVERLOADED")}</td><td>Server density &gt;3 Gbps/server</td><td>Risk of outage if one server fails</td><td>Add servers within 2 weeks</td></tr>')
                html.append(f'<tr><td>{_badge("high", "HIGH")}</td><td>Server density 1.5&ndash;3 Gbps/server</td><td>Close to capacity, no room for growth</td><td>Plan expansion next quarter</td></tr>')
                html.append(f'<tr><td>{_badge("rising", "RISING")}</td><td>Traffic growing &gt;15% vs period start</td><td>Healthy growth, may need more capacity</td><td>Monitor, prepare to scale</td></tr>')
                html.append(f'<tr><td>{_badge("stable", "STABLE")}</td><td>Traffic within &plusmn;15% of average</td><td>Normal operations</td><td>No action needed</td></tr>')
                html.append(f'<tr><td>{_badge("ok", "OK")}</td><td>VPN healthy, capacity adequate</td><td>All good</td><td>Continue monitoring</td></tr>')
                html.append('</tbody></table></div>')

                                html.append(f'<div class="footer">Made with &hearts; by Alex Velesnitski &bull; {total_servers} servers &bull; {now_str}<br>Confidential &mdash; for internal use only</div>')
                html.append('</div></body></html>')

                                if not output_dir:
                    output_dir = os.path.expanduser("~/Downloads")
                filename = f"infra-report-{date_str}.html"
                filepath = os.path.join(output_dir, filename)
                with open(filepath, "w") as f:
                    f.write("\n".join(html))

                vpn_down = sum(1 for cc, cd in country_data.items() if cd["vpn_total"] > 0 and cd["vpn_up"] < cd["vpn_total"])
                return (
                    f"**CEO Report Generated**\n"
                    f"**File:** `{filepath}`\n"
                    f"**Fleet:** {total_servers} servers, {total_countries} countries, {total_traffic} Gbps\n"
                    f"**Alerts:** {len(alerts)} | **VPN issues:** {vpn_down} countries"
                )
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"
