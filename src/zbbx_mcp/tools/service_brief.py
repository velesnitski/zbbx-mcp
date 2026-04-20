"""Daily service brief — marketing/product audience.

Separate from the infra CEO report. Uses softer language (probabilities,
"likely working"), traffic-validates health (active traffic = working
regardless of check state), and filters micro-markets from risk alerts.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx

from zbbx_mcp.classify import classify_host as _classify_host
from zbbx_mcp.data import (
    STATUS_ENABLED,
    KEY_service_PRIMARY,
    KEY_service_SECONDARY,
    KEY_service_TERTIARY,
    build_value_map,
    extract_country,
    fetch_enabled_hosts,
    fetch_traffic_map,
    fetch_trends_batch,
    is_hidden_product,
)
from zbbx_mcp.resolver import InstanceResolver
from zbbx_mcp.utils import safe_output_path

MICRO_MARKET_MBPS = 10.0
TRAFFIC_VALIDATED_MBPS = 5.0
IDLE_CPU_THRESHOLD = 2.0


_CSS = """
body{font-family:-apple-system,'Segoe UI',Roboto,sans-serif;color:#1a1a2e;background:#f8f9fa;line-height:1.55;font-size:13px;margin:0;padding:24px;max-width:1100px;margin:0 auto}
h1{font-size:24px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 10px;color:#1a1a2e;border-bottom:1px solid #e5e7eb;padding-bottom:6px}
.subtitle{color:#6b7280;font-size:13px;margin-bottom:24px}
.section{background:white;border-radius:10px;padding:20px 24px;margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,0.05)}
table{width:100%;border-collapse:collapse;font-size:12px;margin-top:8px}
th{background:#f3f4f6;text-align:left;padding:8px 10px;font-size:10px;text-transform:uppercase;letter-spacing:0.5px;color:#6b7280;border-bottom:2px solid #e5e7eb}
td{padding:8px 10px;border-bottom:1px solid #f3f4f6}
.num{text-align:right;font-variant-numeric:tabular-nums}
.score-bar{display:inline-block;width:140px;height:8px;background:#f3f4f6;border-radius:4px;overflow:hidden;vertical-align:middle;margin-right:8px}
.score-fill{height:100%;border-radius:4px}
.tag{display:inline-block;padding:2px 8px;border-radius:9999px;font-size:10px;font-weight:600;text-transform:uppercase}
.tag-ok{background:#f0fdf4;color:#16a34a}.tag-warn{background:#fff7ed;color:#ea580c}.tag-risk{background:#fef2f2;color:#dc2626}
.tag-info{background:#eff6ff;color:#2563eb}.tag-mute{background:#f5f5f5;color:#6b7280}
.muted{color:#6b7280;font-size:11px}
"""


def _score_color(score: float) -> str:
    if score >= 85:
        return "#16a34a"
    if score >= 65:
        return "#eab308"
    if score >= 40:
        return "#ea580c"
    return "#dc2626"


def _probability_label(pct: float) -> str:
    if pct >= 95:
        return "almost certainly working"
    if pct >= 85:
        return "very likely working"
    if pct >= 70:
        return "likely working"
    if pct >= 50:
        return "partially working"
    if pct >= 25:
        return "mostly impaired"
    return "likely blocked"


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()) -> None:

    if "generate_service_brief" in skip:
        return

    @mcp.tool()
    async def generate_service_brief(
        period: str = "7d",
        output_dir: str = "",
        instance: str = "",
    ) -> str:
        """Generate daily service brief (HTML) for product/marketing audience.

        Differs from generate_ceo_report: softer language with probability scores,
        traffic-validated health (active traffic = working), micro-market filter
        on risk alerts (skip countries with <10 Mbps), 7-day per-product trends.

        Args:
            period: Trend period (default: 7d)
            output_dir: Output directory (default: ~/Downloads)
            instance: Zabbix instance name (optional)
        """
        try:
            client = resolver.resolve(instance)
            now = datetime.now(timezone.utc)
            date_str = now.strftime("%Y-%m-%d")
            now_str = now.strftime("%Y-%m-%d %H:%M UTC")

            hosts = await fetch_enabled_hosts(client)
            product_hosts: dict[str, list[dict]] = {}
            for h in hosts:
                prod, _ = _classify_host(h.get("groups", []))
                if not prod or is_hidden_product(prod):
                    continue
                product_hosts.setdefault(prod, []).append(h)

            if not product_hosts:
                return "No products with enabled hosts."

            all_ids = [h["hostid"] for h in hosts]

            # Gather metrics in parallel
            service_keys = [k for k in (KEY_service_PRIMARY, KEY_service_SECONDARY,
                                         KEY_service_TERTIARY) if k]

            service_items_task = (
                client.call("item.get", {
                    "hostids": all_ids,
                    "output": ["hostid", "key_", "lastvalue"],
                    "filter": {"key_": service_keys, "status": STATUS_ENABLED},
                }) if service_keys else asyncio.sleep(0, result=[])
            )

            cpu_task = client.call("item.get", {
                "hostids": all_ids,
                "output": ["hostid", "lastvalue"],
                "filter": {"key_": "system.cpu.util[,idle]", "status": STATUS_ENABLED},
            })

            traffic_map, service_items, cpu_items = await asyncio.gather(
                fetch_traffic_map(client, all_ids),
                service_items_task,
                cpu_task,
            )

            cpu_map = build_value_map(cpu_items, lambda v: round(100 - float(v), 1))

            # Per-host: which service checks are configured and which are OK
            host_checks: dict[str, dict[str, int]] = {}
            for it in service_items:
                hid = it["hostid"]
                key = it["key_"]
                try:
                    val = int(float(it.get("lastvalue", 0)))
                except (ValueError, TypeError):
                    val = 0
                host_checks.setdefault(hid, {})[key] = val

            # 7-day traffic trends (chunked)
            trend_rows = []
            for i in range(0, len(all_ids), 200):
                chunk = all_ids[i:i + 200]
                rows, _ = await fetch_trends_batch(client, chunk, ["traffic"], period)
                trend_rows.extend(rows)
            host_trend: dict[str, object] = {tr.hostid: tr for tr in trend_rows}

            # --- Build per-product health scores ---
            product_scores: list[dict] = []
            for prod, p_hosts in product_hosts.items():
                total_traffic = 0.0
                weighted_ok = 0.0
                validated_active = 0
                country_set: set[str] = set()
                for h in p_hosts:
                    hid = h["hostid"]
                    mbps = traffic_map.get(hid, 0.0)
                    total_traffic += mbps
                    cc = extract_country(h["host"])
                    if cc:
                        country_set.add(cc)
                    # Health status per host
                    checks = host_checks.get(hid, {})
                    if mbps >= TRAFFIC_VALIDATED_MBPS:
                        host_ok = 1.0  # traffic-validated
                        validated_active += 1
                    elif not checks:
                        host_ok = 0.5  # unknown
                    else:
                        ok_count = sum(1 for v in checks.values() if v == 1)
                        host_ok = ok_count / len(checks) if checks else 0.5
                    # Weight by traffic (min weight = 1 so dormant servers count a bit)
                    weight = max(mbps, 1.0)
                    weighted_ok += host_ok * weight

                total_weight = sum(max(traffic_map.get(h["hostid"], 0.0), 1.0) for h in p_hosts)
                score = (weighted_ok / total_weight * 100) if total_weight else 0
                product_scores.append({
                    "product": prod,
                    "score": round(score, 1),
                    "servers": len(p_hosts),
                    "countries": len(country_set),
                    "traffic_mbps": round(total_traffic, 1),
                    "validated_active": validated_active,
                })

            product_scores.sort(key=lambda x: -x["traffic_mbps"])

            # --- Per-country service quality (probability language) ---
            country_data: dict[str, dict] = {}
            for h in hosts:
                cc = extract_country(h["host"])
                if not cc:
                    continue
                hid = h["hostid"]
                cd = country_data.setdefault(cc, {
                    "total": 0, "ok": 0, "partial": 0, "down": 0,
                    "traffic": 0.0, "checks_configured": 0, "validated": 0,
                })
                cd["total"] += 1
                mbps = traffic_map.get(hid, 0.0)
                cd["traffic"] += mbps
                checks = host_checks.get(hid, {})
                if mbps >= TRAFFIC_VALIDATED_MBPS:
                    cd["validated"] += 1
                    cd["ok"] += 1
                    continue
                if not checks:
                    continue
                cd["checks_configured"] += 1
                ok_count = sum(1 for v in checks.values() if v == 1)
                if ok_count == len(checks):
                    cd["ok"] += 1
                elif ok_count > 0:
                    cd["partial"] += 1
                else:
                    cd["down"] += 1

            # --- Per-protocol blocked servers table ---
            blocked_by_check: dict[str, list[tuple[str, str, float]]] = {}
            for h in hosts:
                hid = h["hostid"]
                checks = host_checks.get(hid, {})
                mbps = traffic_map.get(hid, 0.0)
                cc = extract_country(h["host"])
                for key, val in checks.items():
                    if val == 0:  # DOWN
                        blocked_by_check.setdefault(key, []).append((h["host"], cc, mbps))

            # --- Blocking risk predictions: dropping traffic + failing checks
            # Apply idle filter (cpu_now >= IDLE_CPU_THRESHOLD) to skip standby servers
            # Apply micro-market filter (country traffic >= MICRO_MARKET_MBPS)
            blocking_risks: list[dict] = []
            for cc, cd in country_data.items():
                if cd["traffic"] < MICRO_MARKET_MBPS:
                    continue  # micro-market skip
                cc_hosts = [h for h in hosts if extract_country(h["host"]) == cc]
                drop_pct_sum = 0.0
                drop_count = 0
                problem_count = 0
                for h in cc_hosts:
                    hid = h["hostid"]
                    cpu_now = cpu_map.get(hid, 0.0)
                    if cpu_now < IDLE_CPU_THRESHOLD:
                        continue  # idle standby — skip
                    tr = host_trend.get(hid)
                    if not tr:
                        continue
                    if tr.avg > 1 and tr.current < tr.avg * 0.5:
                        drop_pct = (tr.avg - tr.current) / tr.avg * 100
                        drop_pct_sum += drop_pct
                        drop_count += 1
                    checks = host_checks.get(hid, {})
                    if checks and not any(v == 1 for v in checks.values()):
                        problem_count += 1
                if drop_count == 0 and problem_count == 0:
                    continue
                active = max(cd["total"] - (cd["total"] - cd["validated"] - cd["checks_configured"]), 1)
                problem_ratio = problem_count / active if active else 0
                # Confidence: higher when both traffic drop AND check failures agree
                confidence = min(95, 30 + drop_count * 10 + problem_ratio * 50)
                if confidence < 40:
                    continue
                blocking_risks.append({
                    "country": cc,
                    "confidence": round(confidence),
                    "avg_drop_pct": round(drop_pct_sum / drop_count, 0) if drop_count else 0,
                    "failing_checks": problem_count,
                    "traffic": round(cd["traffic"], 1),
                })
            blocking_risks.sort(key=lambda r: -r["confidence"])

            # --- 7-day traffic trend per product ---
            product_trend: dict[str, dict[str, float]] = {}
            for prod, p_hosts in product_hosts.items():
                pid_set = {h["hostid"] for h in p_hosts}
                day_totals: dict[str, float] = {}
                for tr in trend_rows:
                    if tr.hostid not in pid_set:
                        continue
                    for day, val in (tr.daily or {}).items():
                        day_totals[day] = day_totals.get(day, 0) + val
                product_trend[prod] = day_totals

            # --- Build HTML ---
            html = [f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<title>Service Brief — {now.strftime("%B %d, %Y")}</title>
<style>{_CSS}</style></head><body>
<h1>Service Brief</h1>
<div class="subtitle">Product &amp; Marketing Audience &bull; {now_str}</div>
"""]

            # Product health scores
            html.append('<div class="section"><h2>Product Health Score</h2>')
            html.append('<table><thead><tr><th>Product</th><th>Score</th>'
                        '<th class="num">Servers</th><th class="num">Countries</th>'
                        '<th class="num">Traffic</th><th class="num">Validated Active</th></tr></thead><tbody>')
            for ps in product_scores:
                color = _score_color(ps["score"])
                pct = ps["score"]
                html.append(
                    f'<tr><td><b>{ps["product"]}</b></td>'
                    f'<td><span class="score-bar"><span class="score-fill" '
                    f'style="width:{pct:.0f}%;background:{color}"></span></span>'
                    f'<b style="color:{color}">{ps["score"]:.0f}</b></td>'
                    f'<td class="num">{ps["servers"]}</td>'
                    f'<td class="num">{ps["countries"]}</td>'
                    f'<td class="num">{ps["traffic_mbps"]:.0f} Mbps</td>'
                    f'<td class="num">{ps["validated_active"]}/{ps["servers"]}</td></tr>'
                )
            html.append('</tbody></table></div>')

            # Service quality per country (top 15 by traffic)
            html.append('<div class="section"><h2>Service Quality by Country</h2>'
                        '<div class="muted">Probability language based on traffic-validated checks</div>')
            html.append('<table><thead><tr><th>Country</th><th class="num">Servers</th>'
                        '<th class="num">Traffic</th><th>Quality</th></tr></thead><tbody>')
            country_sorted = sorted(country_data.items(), key=lambda x: -x[1]["traffic"])
            for cc, cd in country_sorted[:15]:
                if cd["traffic"] < MICRO_MARKET_MBPS:
                    label = "insufficient data"
                    tag = "tag-mute"
                    pct = 0
                else:
                    pct = (cd["ok"] / cd["total"] * 100) if cd["total"] else 0
                    label = _probability_label(pct)
                    if pct >= 85:
                        tag = "tag-ok"
                    elif pct >= 60:
                        tag = "tag-info"
                    elif pct >= 30:
                        tag = "tag-warn"
                    else:
                        tag = "tag-risk"
                html.append(
                    f'<tr><td><b>{cc}</b></td><td class="num">{cd["total"]}</td>'
                    f'<td class="num">{cd["traffic"]:.0f} Mbps</td>'
                    f'<td><span class="tag {tag}">{label}</span> '
                    f'<span class="muted">({pct:.0f}% OK)</span></td></tr>'
                )
            html.append('</tbody></table></div>')

            # Blocked servers per check (shows per-protocol visibility)
            if blocked_by_check:
                html.append('<div class="section"><h2>Blocked Servers by Check</h2>'
                            '<div class="muted">Servers failing each configured service check</div>')
                html.append('<table><thead><tr><th>Check</th><th class="num">Servers Failing</th>'
                            '<th class="num">Traffic Still Flowing</th><th>Top Affected Countries</th></tr></thead><tbody>')
                for key in service_keys:
                    rows = blocked_by_check.get(key, [])
                    if not rows:
                        html.append(
                            f'<tr><td><code>{key}</code></td><td class="num">0</td>'
                            f'<td class="num">&mdash;</td>'
                            f'<td><span class="tag tag-ok">all healthy</span></td></tr>'
                        )
                        continue
                    traffic_flowing = sum(1 for _, _, m in rows if m >= TRAFFIC_VALIDATED_MBPS)
                    country_counts: dict[str, int] = {}
                    for _, cc, _ in rows:
                        if cc:
                            country_counts[cc] = country_counts.get(cc, 0) + 1
                    top_ccs = sorted(country_counts.items(), key=lambda x: -x[1])[:5]
                    top_str = ", ".join(f"{cc} ({n})" for cc, n in top_ccs)
                    html.append(
                        f'<tr><td><code>{key}</code></td><td class="num">{len(rows)}</td>'
                        f'<td class="num">{traffic_flowing}</td>'
                        f'<td>{top_str or "&mdash;"}</td></tr>'
                    )
                html.append('</tbody></table></div>')

            # Blocking risk predictions
            html.append('<div class="section"><h2>Blocking Risk Predictions</h2>'
                        '<div class="muted">Idle standby servers and micro-markets '
                        f'(&lt;{MICRO_MARKET_MBPS:.0f} Mbps) excluded</div>')
            if blocking_risks:
                html.append('<table><thead><tr><th>Country</th><th class="num">Confidence</th>'
                            '<th class="num">Traffic Drop</th><th class="num">Failing Checks</th>'
                            '<th class="num">Country Traffic</th></tr></thead><tbody>')
                for r in blocking_risks[:15]:
                    conf_color = _score_color(100 - r["confidence"])  # inverse: high conf = red
                    html.append(
                        f'<tr><td><b>{r["country"]}</b></td>'
                        f'<td class="num" style="color:{conf_color};font-weight:600">{r["confidence"]}%</td>'
                        f'<td class="num">{r["avg_drop_pct"]:+.0f}%</td>'
                        f'<td class="num">{r["failing_checks"]}</td>'
                        f'<td class="num">{r["traffic"]:.0f} Mbps</td></tr>'
                    )
                html.append('</tbody></table></div>')
            else:
                html.append('<p><span class="tag tag-ok">no significant blocking risk detected</span></p></div>')

            # 7-day traffic trend per product
            html.append(f'<div class="section"><h2>Traffic Trend by Product ({period})</h2>')
            html.append('<table><thead><tr><th>Product</th>')
            all_days: list[str] = []
            for trends in product_trend.values():
                for day in trends:
                    if day not in all_days:
                        all_days.append(day)
            # Sort by actual calendar order (YYYY-MM-DD-style or month-day)
            all_days.sort()
            for day in all_days:
                html.append(f'<th class="num">{day}</th>')
            html.append('</tr></thead><tbody>')
            for ps in product_scores:
                prod = ps["product"]
                trends = product_trend.get(prod, {})
                cells = "".join(f'<td class="num">{trends.get(d, 0):.0f}</td>' for d in all_days)
                html.append(f'<tr><td><b>{prod}</b></td>{cells}</tr>')
            html.append('</tbody></table>')
            html.append('<div class="muted">Values in Mbps, daily average</div></div>')

            # Footer
            html.append(f'<div class="muted" style="text-align:center;margin-top:32px">'
                        f'{len(hosts)} servers &bull; {len(product_hosts)} products &bull; '
                        f'generated {now_str}</div>')
            html.append('</body></html>')

            if not output_dir:
                output_dir = "~/Downloads"
            filename = f"service-brief-{date_str}.html"
            filepath = safe_output_path(output_dir, filename)
            with open(filepath, "w") as f:
                f.write("\n".join(html))

            return (
                f"**Service Brief Generated**\n"
                f"**File:** `{filepath}`\n"
                f"**Products:** {len(product_scores)} | "
                f"**Top score:** {product_scores[0]['product']} ({product_scores[0]['score']:.0f}) | "
                f"**Blocking risks:** {len(blocking_risks)} countries"
            )
        except (httpx.HTTPError, ValueError) as e:
            return f"Error: {e}"
