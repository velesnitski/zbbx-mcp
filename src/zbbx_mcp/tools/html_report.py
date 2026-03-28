"""Dark-themed HTML infrastructure report."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from statistics import median

import httpx

from zbbx_mcp.data import (
    extract_country,
    fetch_all_data,
    fetch_trends_batch,
)
from zbbx_mcp.excel import BW_MAX, BW_ORANGE, BW_RED
from zbbx_mcp.resolver import InstanceResolver

CSS = """
:root{--bg:#0f1117;--card:#1a1d27;--border:#2a2d3a;--text:#e4e4e7;--muted:#9ca3af;--accent:#6366f1;--red:#ef4444;--orange:#f59e0b;--green:#22c55e;--blue:#3b82f6}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--text);line-height:1.5;padding:24px;max-width:1400px;margin:0 auto}
h1{font-size:1.8rem;margin-bottom:4px}h2{font-size:1.3rem;margin:32px 0 16px;padding-bottom:8px;border-bottom:1px solid var(--border)}
.subtitle{color:var(--muted);font-size:.9rem;margin-bottom:24px}
.grid{display:grid;gap:16px;margin:16px 0}.grid-4{grid-template-columns:repeat(4,1fr)}.grid-3{grid-template-columns:repeat(3,1fr)}
.card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:20px}
.card-header{color:var(--muted);font-size:.8rem;text-transform:uppercase;letter-spacing:.5px;margin-bottom:8px}
.card-value{font-size:2rem;font-weight:700}.card-sub{font-size:.85rem;color:var(--muted);margin-top:4px}
.badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:.75rem;font-weight:600}
.badge-red{background:rgba(239,68,68,.15);color:var(--red)}.badge-orange{background:rgba(245,158,11,.15);color:var(--orange)}
.badge-green{background:rgba(34,197,94,.15);color:var(--green)}.badge-blue{background:rgba(59,130,246,.15);color:var(--blue)}
table{width:100%;border-collapse:collapse;margin:12px 0;font-size:.875rem}
th{text-align:left;padding:10px 12px;background:var(--card);border-bottom:2px solid var(--border);color:var(--muted);font-weight:600;font-size:.8rem;text-transform:uppercase;letter-spacing:.3px;position:sticky;top:0}
td{padding:10px 12px;border-bottom:1px solid var(--border)}tr:hover td{background:rgba(99,102,241,.05)}
.cpu-critical{color:var(--red);font-weight:700}.cpu-warn{color:var(--orange);font-weight:600}.cpu-ok{color:var(--green)}
.trend-drop{color:var(--red)}.trend-rise{color:var(--green)}.trend-stable{color:var(--muted)}
.section{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:24px;margin:16px 0}
.bar{height:8px;border-radius:4px;background:var(--border);overflow:hidden}.bar-fill{height:100%;border-radius:4px}
@media(max-width:768px){.grid-4,.grid-3{grid-template-columns:1fr 1fr}.card-value{font-size:1.5rem}}
@media print{body{background:#fff;color:#000;padding:12px}
.card{background:#f8f8f8;border:1px solid #ddd}.card-header{color:#666}.card-value{color:#000}
th{background:#f0f0f0;color:#333;border-color:#ccc}td{border-color:#ddd;color:#000}
.badge-red{background:#fee;color:#c00;border:1px solid #c00}.badge-green{background:#efe;color:#060;border:1px solid #060}
.badge-orange{background:#ffd;color:#960;border:1px solid #960}.badge-blue{background:#eef;color:#009;border:1px solid #009}
.cpu-critical{color:#c00}.cpu-warn{color:#960}.cpu-ok{color:#060}
.trend-drop{color:#c00}.trend-rise{color:#060}.trend-stable{color:#666}
.bar{border:1px solid #ccc}.bar-fill{print-color-adjust:exact;-webkit-print-color-adjust:exact}
a{color:#00c;text-decoration:underline}.subtitle{color:#666}
code{background:#f0f0f0;padding:1px 4px;border-radius:2px}
table{font-size:.8rem;page-break-inside:auto}tr{page-break-inside:avoid}}
"""


def _cpu_class(val: float | None) -> str:
    if val is None:
        return ""
    if val >= 80:
        return "cpu-critical"
    if val >= 50:
        return "cpu-warn"
    return "cpu-ok"


def _badge(text: str, color: str) -> str:
    return f'<span class="badge badge-{color}">{text}</span>'


def _kpi_card(header: str, value: str, sub: str = "", color: str = "") -> str:
    style = f' style="color: var(--{color})"' if color else ""
    return (
        f'<div class="card"><div class="card-header">{header}</div>'
        f'<div class="card-value"{style}>{value}</div>'
        f'<div class="card-sub">{sub}</div></div>'
    )


def _bw_bar(mbps: float | None) -> str:
    if mbps is None:
        return ""
    pct = min(mbps / BW_MAX * 100, 100)
    color = "var(--red)" if mbps >= BW_RED else "var(--orange)" if mbps >= BW_ORANGE else "var(--green)"
    return f'<div class="bar"><div class="bar-fill" style="width:{pct:.0f}%;background:{color}"></div></div>'


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()) -> None:

    if "generate_html_report" not in skip:

        @mcp.tool()
        async def generate_html_report(
            country: str = "",
            product: str = "",
            products: str = "",
            exclude_product: str = "",
            period: str = "7d",
            output_dir: str = "",
            instance: str = "",
        ) -> str:
            """Generate a dark-themed HTML infrastructure report, printable to PDF.

            Args:
                country: Country code filter (optional)
                product: Single product filter (optional)
                products: Comma-separated products to include (optional)
                exclude_product: Comma-separated products to exclude (optional)
                period: Trend period (default: 7d)
                output_dir: Output directory (default: ~/Downloads)
                instance: Zabbix instance (optional)
            """
            try:
                client = resolver.resolve(instance)
                zabbix_base_url = client.frontend_url

                # Fetch current data
                result = await fetch_all_data(client)
                rows = result.rows

                # Filter
                include_set = set()
                if products:
                    include_set = {p.strip().lower() for p in products.split(",")}
                exclude_set = set()
                if exclude_product:
                    exclude_set = {p.strip().lower() for p in exclude_product.split(",")}

                if country or product or include_set or exclude_set:
                    filtered = []
                    for r in rows:
                        rp = r.get("Product", "").lower()
                        if country and extract_country(r.get("Host", "")).lower() != country.lower():
                            continue
                        if product and product.lower() not in rp:
                            continue
                        if include_set and not any(p in rp for p in include_set):
                            continue
                        if exclude_set and any(p in rp for p in exclude_set):
                            continue
                        filtered.append(r)
                    rows = filtered

                if not rows:
                    return "No servers match the filters."

                # Fetch trends for filtered hosts (use Host ID from fetch_all_data)
                hostids = [r["Host ID"] for r in rows if r.get("Host ID")]

                trend_rows = []
                if hostids:
                    trend_rows, _ = await fetch_trends_batch(
                        client, hostids, ["cpu", "traffic"], period,
                    )

                # Build trend lookup
                host_trends: dict[str, dict] = {}
                for tr in trend_rows:
                    host_trends.setdefault(tr.hostname, {})[tr.metric] = tr

                # KPIs
                total = len(rows)
                with_cpu = [r for r in rows if r["CPU %"] is not None]
                with_traffic = [r for r in rows if r["Traffic In Mbps"] is not None]
                high_cpu = sum(1 for r in with_cpu if r["CPU %"] >= 80)
                high_bw = sum(1 for r in with_traffic if r["Traffic In Mbps"] >= BW_RED)
                service_down = sum(1 for r in rows if r.get("service Primary") == "DOWN")
                countries = sorted(set(r["Country"] for r in rows if r["Country"]))
                providers = sorted(set(r["Provider"] for r in rows if r["Provider"]))
                med_cpu = median([r["CPU %"] for r in with_cpu]) if with_cpu else 0
                med_traffic = median([r["Traffic In Mbps"] for r in with_traffic]) if with_traffic else 0

                now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                title = "Infrastructure Report"
                if country:
                    title += f" — {country.upper()}"
                if product:
                    title += f" — {product}"

                # Build HTML
                html = [f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title><style>{CSS}</style></head><body>
<h1>{title}</h1>
<div class="subtitle">Generated {now} | Period: {period} | zbbx-mcp</div>

<div class="grid grid-4">
{_kpi_card("Servers", str(total), f"{len(countries)} countries, {len(providers)} providers")}
{_kpi_card("Median CPU", f"{med_cpu:.1f}%", f"{high_cpu} servers &gt; 80%", "red" if high_cpu else "")}
{_kpi_card("Median Traffic", f"{med_traffic:.0f} Mbps", f"{high_bw} near saturation", "orange" if high_bw else "")}
{_kpi_card("service Health", f"{service_down} DOWN" if service_down else "All OK", f"{total - service_down} healthy" if service_down else f"{sum(1 for r in rows if r.get('service Primary')=='OK')} monitored", "red" if service_down else "green")}
</div>
"""]

                                # Country traffic breakdown (top 10 as CSS bar chart)
                country_traffic: dict[str, float] = {}
                country_servers: dict[str, int] = {}
                country_risk: dict[str, list[str]] = {}
                for r in rows:
                    cc = r.get("Country", "")
                    if not cc:
                        continue
                    country_servers[cc] = country_servers.get(cc, 0) + 1
                    country_traffic[cc] = country_traffic.get(cc, 0) + (r.get("Traffic In Mbps") or 0)

                # Risk flags
                for cc in country_servers:
                    risks = []
                    if country_servers[cc] == 1:
                        risks.append("no redundancy")
                    cc_cpus = [r["CPU %"] for r in rows if r.get("Country") == cc and r.get("CPU %") is not None]
                    if cc_cpus and sum(cc_cpus) / len(cc_cpus) > 70:
                        risks.append("high CPU")
                    cc_service_down = sum(1 for r in rows if r.get("Country") == cc and r.get("service Primary") == "DOWN")
                    if cc_service_down > 0:
                        risks.append(f"{cc_service_down} service DOWN")
                    if risks:
                        country_risk[cc] = risks

                top_countries = sorted(country_traffic.items(), key=lambda x: -x[1])[:12]
                max_ct = max((t for _, t in top_countries), default=1)

                html.append('<div class="section"><h2 style="margin-top:0">Executive Summary</h2>')

                # Actions needed
                actions = []
                if service_down:
                    actions.append(f'<span class="badge badge-red">{service_down} service DOWN</span>')
                if high_cpu:
                    actions.append(f'<span class="badge badge-orange">{high_cpu} servers &gt;80% CPU</span>')
                if high_bw:
                    actions.append(f'<span class="badge badge-orange">{high_bw} near BW limit</span>')
                no_redund = [cc for cc, cnt in country_servers.items() if cnt == 1]
                if no_redund:
                    actions.append(f'<span class="badge badge-blue">{len(no_redund)} single-server countries</span>')
                if actions:
                    html.append(f'<div style="margin:12px 0">{"  ".join(actions)}</div>')
                else:
                    html.append('<div style="margin:12px 0"><span class="badge badge-green">All clear</span></div>')

                # Country traffic bars
                html.append('<h3 style="color:var(--muted);margin:16px 0 8px">Traffic by Country</h3>')
                html.append('<table style="font-size:.85rem"><tbody>')
                for cc, traffic in top_countries:
                    pct = traffic / max_ct * 100
                    risk_badges = ""
                    if cc in country_risk:
                        risk_badges = " ".join(f'<span class="badge badge-red" style="font-size:.7rem">{r}</span>' for r in country_risk[cc])
                    bar_color = "var(--red)" if cc in country_risk else "var(--accent)"
                    html.append(
                        f'<tr><td style="width:40px;font-weight:600">{cc}</td>'
                        f'<td style="width:50px;text-align:right;color:var(--muted)">{country_servers.get(cc, 0)} srv</td>'
                        f'<td><div class="bar" style="height:6px"><div class="bar-fill" style="width:{pct:.0f}%;background:{bar_color}"></div></div></td>'
                        f'<td style="width:90px;text-align:right">{traffic / 1000:.2f} Gbps</td>'
                        f'<td style="width:140px">{risk_badges}</td></tr>'
                    )
                if len(country_traffic) > 12:
                    html.append(f'<tr><td colspan="5" style="color:var(--muted);font-size:.8rem">+{len(country_traffic) - 12} more countries</td></tr>')
                html.append('</tbody></table></div>')

                # Group rows by Product → Dashboard → Tab
                def _render_server_row(r: dict) -> str:
                    hostname = r["Host"]
                    cpu_now = r.get("CPU %")
                    traffic_now = r.get("Traffic In Mbps")
                    ht = host_trends.get(hostname, {})
                    cpu_trend = ht.get("cpu")
                    traffic_trend = ht.get("traffic")
                    cpu_avg = f'{cpu_trend.avg:.1f}%' if cpu_trend else "N/A"
                    traffic_avg = f'{traffic_trend.avg:.0f}' if traffic_trend else "N/A"
                    trend_dir = traffic_trend.trend_dir if traffic_trend else ""
                    trend_class = {"rising": "trend-rise", "dropping": "trend-drop", "stable": "trend-stable"}.get(trend_dir, "")
                    cpu_cls = _cpu_class(cpu_now)
                    cpu_str = f'{cpu_now:.1f}%' if cpu_now is not None else "N/A"
                    traffic_str = f'{traffic_now:.0f}' if traffic_now is not None else "N/A"
                    service_status = r.get("service Primary", "")
                    service_html = _badge("DOWN", "red") if service_status == "DOWN" else (_badge("OK", "green") if service_status == "OK" else "")
                    hostid = r.get("Host ID", "")
                    ip = r.get("IP", "")
                    dashid = r.get("Dashboard ID", "")
                    pidx = r.get("Page Index", 0)
                    if dashid:
                        zlink = f'{zabbix_base_url}/zabbix.php?action=dashboard.view&dashboardid={dashid}&page={pidx}'
                    elif hostid:
                        zlink = f'{zabbix_base_url}/zabbix.php?action=latest.view&hostids%5B%5D={hostid}'
                    else:
                        zlink = ""
                    host_cell = f'<a href="{zlink}" target="_blank" style="color:var(--accent);text-decoration:none"><strong>{hostname}</strong></a>' if zlink else f'<strong>{hostname}</strong>'
                    ip_cell = f'<code style="font-size:.8rem;color:var(--muted)">{ip}</code>' if ip else ""
                    return (
                        f'<tr><td>{host_cell}</td><td>{ip_cell}</td>'
                        f'<td>{r.get("Country", "")}</td><td>{r.get("Provider", "")}</td>'
                        f'<td class="{cpu_cls}">{cpu_str}</td><td>{cpu_avg}</td>'
                        f'<td>{traffic_str} Mbps</td><td>{traffic_avg} Mbps</td>'
                        f'<td>{_bw_bar(traffic_now)}</td><td>{service_html}</td>'
                        f'<td class="{trend_class}">{trend_dir}</td></tr>'
                    )

                table_header = (
                    '<table><thead><tr>'
                    '<th>Server</th><th>IP</th><th>Country</th><th>Provider</th>'
                    f'<th>CPU Now</th><th>CPU Avg {period}</th>'
                    f'<th>Traffic Now</th><th>Traffic Avg {period}</th>'
                    '<th>BW Util</th><th>service</th><th>Trend</th>'
                    '</tr></thead><tbody>'
                )

                # Build hierarchy: Product → Dashboard → Tab → [rows]
                from collections import defaultdict
                grouped: dict[str, dict[str, dict[str, list]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
                off_dashboard = []
                for r in rows:
                    if r.get("Dashboard"):
                        prod_key = f"{r.get('Product', 'Unknown')}/{r.get('Tier', '')}"
                        grouped[prod_key][r["Dashboard"]][r.get("Tab", "")].append(r)
                    else:
                        off_dashboard.append(r)

                for prod_key in sorted(grouped):
                    html.append(f'<h2>{prod_key}</h2>')
                    for dash_name in sorted(grouped[prod_key]):
                        # Dashboard link
                        sample = next(iter(next(iter(grouped[prod_key][dash_name].values()))), None)
                        dashid = sample.get("Dashboard ID", "") if sample else ""
                        dash_link = f'{zabbix_base_url}/zabbix.php?action=dashboard.view&dashboardid={dashid}' if dashid else ""
                        dash_html = f'<a href="{dash_link}" target="_blank" style="color:var(--accent);text-decoration:none">{dash_name}</a>' if dash_link else dash_name
                        html.append(f'<h3>{dash_html}</h3>')

                        for tab_name in sorted(grouped[prod_key][dash_name]):
                            tab_rows = grouped[prod_key][dash_name][tab_name]
                            tab_rows.sort(key=lambda r: -(r.get("Traffic In Mbps") or 0))
                            tab_traffic = sum(r.get("Traffic In Mbps") or 0 for r in tab_rows)
                            html.append(f'<h4 style="color:var(--muted);margin:12px 0 6px">{tab_name} ({len(tab_rows)} servers, {tab_traffic:.0f} Mbps total)</h4>')
                            html.append(table_header)
                            for r in tab_rows:
                                html.append(_render_server_row(r))
                            html.append('</tbody></table>')

                if off_dashboard:
                    off_dashboard.sort(key=lambda r: -(r.get("Traffic In Mbps") or 0))
                    html.append(f'<h2>Off-Dashboard ({len(off_dashboard)} servers)</h2>')
                    html.append(table_header)
                    for r in off_dashboard:
                        html.append(_render_server_row(r))
                    html.append('</tbody></table>')

                # Provider summary
                prov_counts: dict[str, int] = {}
                for r in rows:
                    p = r.get("Provider") or "Other"
                    prov_counts[p] = prov_counts.get(p, 0) + 1

                html.append('<h2>Provider Distribution</h2>')
                html.append('<div class="grid grid-3">')
                for prov in sorted(prov_counts, key=lambda x: -prov_counts[x]):
                    html.append(_kpi_card(prov, str(prov_counts[prov]), "servers"))
                html.append('</div>')

                # Footer
                html.append('<div class="subtitle" style="margin-top:40px;text-align:center">')
                html.append(f'Generated by zbbx-mcp v1.3 | {total} servers | {now}</div>')
                html.append('</body></html>')

                # Save
                if not output_dir:
                    output_dir = os.path.expanduser("~/Downloads")
                ts = datetime.now().strftime("%Y%m%d_%H%M")
                safe_name = f"{'_'.join(filter(None, [country, product]))}_" if (country or product) else ""
                filename = f"zabbix_report_{safe_name}{ts}.html"
                filepath = os.path.join(output_dir, filename)

                with open(filepath, "w") as f:
                    f.write("\n".join(html))

                return (
                    f"**HTML Report Generated**\n\n"
                    f"**File:** `{filepath}`\n"
                    f"**Servers:** {total}\n"
                    f"**Open in browser and Cmd+P to save as PDF**"
                )
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"
