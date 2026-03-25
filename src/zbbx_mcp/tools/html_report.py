"""Dark-themed HTML infrastructure report."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from statistics import median

import httpx

from zbbx_mcp.resolver import InstanceResolver
from zbbx_mcp.data import fetch_all_data, fetch_trends_batch, extract_country
from zbbx_mcp.excel import BW_RED, BW_ORANGE, BW_MAX
from zbbx_mcp.classify import classify_host as _classify_host, detect_provider

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
@media print{body{background:#fff;color:#000}th{background:#f0f0f0;color:#333}td{border-color:#ddd}.card{border-color:#ddd;background:#f8f8f8}.badge-red{color:#c00}.badge-green{color:#060}}
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
            period: str = "7d",
            output_dir: str = "",
            instance: str = "",
        ) -> str:
            """Generate a dark-themed HTML infrastructure report.

            Includes KPI cards, server table with color-coded metrics,
            traffic bars, trend analysis, and health overview.
            Printable to PDF via browser.

            Args:
                country: Filter by country code (optional)
                product: Filter by product name (optional)
                period: Trend period for avg/peak columns (default: 7d)
                output_dir: Directory for the HTML file (default: ~/Downloads)
                instance: Zabbix instance name (optional)
            """
            try:
                client = resolver.resolve(instance)

                # Fetch current data
                result = await fetch_all_data(client)
                rows = result.rows

                # Filter
                if country or product:
                    rows = [
                        r for r in rows
                        if (not country or country.lower() in r.get("Country", "").lower()
                            or country.lower() in r.get("Host", "").lower())
                        and (not product or product.lower() in r.get("Product", "").lower())
                    ]

                if not rows:
                    return "No servers match the filters."

                # Fetch trends for filtered hosts

                hostids = []
                host_id_map = {}
                all_hosts = await client.call("host.get", {
                    "output": ["hostid", "host"], "filter": {"status": "0"},
                })
                name_to_id = {h["host"]: h["hostid"] for h in all_hosts}
                for r in rows:
                    hid = name_to_id.get(r["Host"])
                    if hid:
                        hostids.append(hid)
                        host_id_map[r["Host"]] = hid

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

                # Server table
                html.append('<h2>Server Overview</h2>')
                html.append('<table><thead><tr>')
                html.append('<th>Server</th><th>Country</th><th>Product</th><th>Provider</th>')
                html.append(f'<th>CPU Now</th><th>CPU Avg {period}</th>')
                html.append(f'<th>Traffic Now</th><th>Traffic Avg {period}</th>')
                html.append('<th>BW Util</th><th>service Primary</th><th>Trend</th>')
                html.append('</tr></thead><tbody>')

                rows.sort(key=lambda r: -(r.get("Traffic In Mbps") or 0))

                for r in rows:
                    hostname = r["Host"]
                    cpu_now = r.get("CPU %")
                    traffic_now = r.get("Traffic In Mbps")

                    # Trend data
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

                    service1 = r.get("service Primary", "")
                    service_html = _badge("DOWN", "red") if service1 == "DOWN" else (_badge("OK", "green") if service1 == "OK" else "")

                    html.append(f'<tr>')
                    html.append(f'<td><strong>{hostname}</strong></td>')
                    html.append(f'<td>{r.get("Country", "")}</td>')
                    html.append(f'<td>{r.get("Product", "")}/{r.get("Tier", "")}</td>')
                    html.append(f'<td>{r.get("Provider", "")}</td>')
                    html.append(f'<td class="{cpu_cls}">{cpu_str}</td>')
                    html.append(f'<td>{cpu_avg}</td>')
                    html.append(f'<td>{traffic_str} Mbps</td>')
                    html.append(f'<td>{traffic_avg} Mbps</td>')
                    html.append(f'<td>{_bw_bar(traffic_now)}</td>')
                    html.append(f'<td>{service_html}</td>')
                    html.append(f'<td class="{trend_class}">{trend_dir}</td>')
                    html.append('</tr>')

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
                html.append(f'<div class="subtitle" style="margin-top:40px;text-align:center">')
                html.append(f'Generated by zbbx-mcp v1.0 | {total} servers | {now}</div>')
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
