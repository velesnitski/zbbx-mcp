"""Health assessment, shutdown candidates, and capacity planning tools."""

import asyncio
from statistics import median as _median

import httpx

from zbbx_mcp.classify import classify_host as _classify_host
from zbbx_mcp.classify import detect_provider
from zbbx_mcp.data import (
    KEY_service_PRIMARY,
    build_parent_map,
    extract_country,
    fetch_host_dashboards,
    fetch_service_status,
    fetch_trends_batch,
    host_ip,
)
from zbbx_mcp.excel import BW_MAX
from zbbx_mcp.resolver import InstanceResolver
from zbbx_mcp.utils import resolve_group_ids


def _compute_shutdown_safety(
    candidate_avg_mbps: float | None,
    peer_metrics: list[dict],
    safety_margin: float = 1.5,
) -> tuple[str, float]:
    """Decide whether peers can absorb a candidate's traffic.

    A peer's spare capacity is `peak - avg` — the headroom between its
    typical-busy and its observed maximum. Cohort headroom is the sum
    across peers (other shutdown candidates excluded by the caller).

    Returns (label, headroom_mbps):
        SOLO  — no peers in the cohort; cannot shut down regardless
        SAFE  — headroom >= candidate_avg * safety_margin
        RISKY — headroom is positive but below the safety margin
        N/A   — candidate has no traffic figure to compare against

    peer_metrics: [{"peak": float|None, "avg": float|None}, ...]
    """
    if not peer_metrics:
        return "SOLO", 0.0
    headroom = 0.0
    for p in peer_metrics:
        peak = p.get("peak")
        avg = p.get("avg")
        if peak is None or avg is None:
            continue
        spare = peak - avg
        if spare > 0:
            headroom += spare
    if candidate_avg_mbps is None:
        return "N/A", headroom
    if headroom >= candidate_avg_mbps * safety_margin:
        return "SAFE", headroom
    return "RISKY", headroom


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()):

    if "get_health_assessment" not in skip:

        @mcp.tool()
        async def get_health_assessment(
            country: str = "",
            product: str = "",
            group: str = "",
            period: str = "7d",
            min_severity: str = "WARNING",
            max_results: int = 30,
            group_similar: bool = True,
            instance: str = "",
        ) -> str:
            """Health assessment — per-server scoring for CPU, traffic, efficiency issues.

            Args:
                country: Filter by country code (optional)
                product: Filter by product name (optional)
                group: Filter by Zabbix host group (optional)
                period: Analysis period: 1d, 7d, 30d (default: 7d)
                instance: Zabbix instance name (optional)
            """
            try:
                client = resolver.resolve(instance)

                params: dict = {
                    "output": ["hostid", "host", "available"],
                    "selectGroups": ["name"],
                    "selectInterfaces": ["ip"],
                    "filter": {"status": "0"},
                }
                if group:
                    gids = await resolve_group_ids(client, group)
                    if gids is None:
                        return f"Group '{group}' not found."
                    params["groupids"] = gids

                hosts = await client.call("host.get", params)
                filtered = []
                for h in hosts:
                    prod, tier = _classify_host(h.get("groups", []))
                    if product and product.lower() not in (prod or "").lower():
                        continue
                    if country and extract_country(h.get("host", "")).lower() != country.lower():
                        continue
                    h["_prod"] = prod
                    filtered.append(h)

                if not filtered:
                    return "No servers match the filters."

                hostids = [h["hostid"] for h in filtered]

                trend_rows_task = fetch_trends_batch(
                    client, hostids, ["cpu", "traffic", "load"], period,
                )
                # Task 36: Fetch service health alongside trends
                async def _empty():
                    return []

                service1_task = client.call("item.get", {
                    "hostids": hostids,
                    "output": ["hostid", "lastvalue"],
                    "filter": {"key_": KEY_service_PRIMARY, "status": "0"},
                }) if KEY_service_PRIMARY else _empty()

                (trend_rows, _), service1_items = await asyncio.gather(
                    trend_rows_task, service1_task,
                    return_exceptions=True,
                )
                if isinstance(trend_rows, BaseException):
                    trend_rows = []
                service1_items = service1_items if isinstance(service1_items, list) else []

                service1_map: dict[str, int] = {}
                for item in service1_items:
                    try:
                        service1_map[item["hostid"]] = int(float(item["lastvalue"]))
                    except (ValueError, TypeError, KeyError):
                        pass

                # Build per-host metrics
                host_metrics: dict[str, dict] = {}
                for r in trend_rows:
                    host_metrics.setdefault(r.hostid, {})[r.metric] = r

                # Peer medians for efficiency
                # cpu_avgs removed (unused)
                efficiencies = []
                for hm in host_metrics.values():
                    cpu = hm.get("cpu")
                    traffic = hm.get("traffic")
                    if cpu and traffic and traffic.avg > 10:
                        efficiencies.append(cpu.avg / (traffic.avg / 100))
                eff_median = _median(efficiencies) if efficiencies else 0

                # Assess each server
                issues: list[dict] = []
                for h in filtered:
                    hid = h["hostid"]
                    hm = host_metrics.get(hid, {})
                    cpu = hm.get("cpu")
                    traffic = hm.get("traffic")
                    hostname = h["host"]
                    server_issues = []
                    score = 100

                    if cpu and cpu.avg > 80 and cpu.min_val > 50:
                        server_issues.append(f"Chronic CPU overload: avg {cpu.avg}%, never below {cpu.min_val}%")
                        score -= 40
                    elif cpu and cpu.avg > 60:
                        server_issues.append(f"High CPU: avg {cpu.avg}%")
                        score -= 20

                    if traffic and traffic.avg > 10 and traffic.current < traffic.avg * 0.7:
                        drop = round((1 - traffic.current / traffic.avg) * 100)
                        server_issues.append(f"Traffic dropped {drop}%: {traffic.current} vs avg {traffic.avg} Mbps")
                        score -= 25

                    if cpu and traffic and traffic.avg > 10 and eff_median > 0:
                        eff = cpu.avg / (traffic.avg / 100)
                        if eff > eff_median * 3:
                            server_issues.append(f"Inefficient: {eff:.1f}% CPU/100Mbps (peer: {eff_median:.1f}%)")
                            score -= 15

                    if traffic and traffic.peak > 700:
                        server_issues.append(f"Near BW limit: peak {traffic.peak} Mbps")
                        score -= 10

                    # Task 34+40: Idle/dead with recency detection
                    if traffic and traffic.avg < 1.0 and cpu and cpu.avg < 5.0:
                        if traffic.peak > 10:
                            server_issues.append(f"Recently died: peak was {traffic.peak} Mbps, now avg {traffic.avg}")
                            score -= 35
                        else:
                            server_issues.append(f"Always idle: traffic {traffic.avg} Mbps, CPU {cpu.avg}%")
                            score -= 25

                    # Task 34: Zombie detection (high CPU, no traffic)
                    if cpu and cpu.avg > 50 and traffic and traffic.avg < 1.0:
                        server_issues.append(f"Zombie: CPU {cpu.avg}% but traffic {traffic.avg} Mbps")
                        score -= 40

                    # Task 36: service health
                    service1_val = service1_map.get(hid)
                    service_status = ""
                    if service1_val is not None:
                        if service1_val == 0:
                            server_issues.append("service protocol DOWN")
                            score -= 20
                            service_status = "DOWN"
                        else:
                            service_status = "OK"

                    # Task 41: Agent availability
                    agent_avail = h.get("available", "0")
                    if agent_avail == "2":
                        server_issues.append("Zabbix agent unavailable")
                        score -= 30

                    if server_issues:
                        ip = host_ip(h)
                        issues.append({
                            "host": hostname, "hostid": hid, "ip": ip,
                            "score": max(0, score),
                            "product": h.get("_prod", ""),
                            "provider": detect_provider(ip) if ip else "",
                            "service": service_status,
                            "agent": "down" if agent_avail == "2" else "up",
                            "issues": server_issues,
                            "cpu_avg": cpu.avg if cpu else None,
                            "traffic_avg": traffic.avg if traffic else None,
                        })

                issues.sort(key=lambda x: x["score"])

                if not issues:
                    return f"All {len(filtered)} servers healthy over {period}."

                healthy = len(filtered) - len(issues)
                critical = sum(1 for i in issues if i["score"] < 30)
                warning = sum(1 for i in issues if 30 <= i["score"] < 70)

                # Task 39: Cluster/location pattern detection
                clusters: dict[str, list[dict]] = {}
                for i in issues:
                    ctry = extract_country(i["host"])
                    if ctry:
                        clusters.setdefault(ctry, []).append(i)

                cluster_alerts = []
                for ctry, members in clusters.items():
                    if len(members) >= 3:
                        dead = sum(1 for m in members if m["score"] < 30)
                        if dead == len(members):
                            cluster_alerts.append(f"**CLUSTER DEAD: {ctry}** — all {len(members)} servers critical")
                        elif dead / len(members) > 0.5:
                            cluster_alerts.append(f"**CLUSTER DEGRADED: {ctry}** — {dead}/{len(members)} servers critical")

                agents_down = sum(1 for i in issues if i.get("agent") == "down")

                # Filter by min_severity
                sev_thresholds = {"CRITICAL": 30, "WARNING": 70, "INFO": 100}
                max_score = sev_thresholds.get(min_severity.upper(), 100) if min_severity else 100
                all_issues = len(issues)
                issues_filtered = [i for i in issues if i["score"] < max_score]
                info_omitted = all_issues - len(issues_filtered)

                parts = [
                    f"**Health Assessment ({period}): {len(filtered)} servers**\n",
                    f"Healthy: {healthy} | Warning: {warning} | Critical: {critical}",
                ]
                if agents_down:
                    parts.append(f"Agent unavailable: {agents_down}")
                if info_omitted:
                    parts.append(f"*{info_omitted} INFO items omitted (use min_severity='INFO' to see all)*")
                parts.append("")

                issues = issues_filtered

                if cluster_alerts:
                    parts.append("## Cluster Alerts\n")
                    for alert in cluster_alerts:
                        parts.append(alert)
                    parts.append("")

                if group_similar and len(issues) > 10:
                    # Group servers with identical score+issues by country+provider
                    grouped: dict[str, list] = {}
                    for i in issues:
                        key = f"{extract_country(i['host'])}|{i['provider']}|{i['score']}|{'|'.join(sorted(is_[:30] for is_ in i['issues']))}"
                        grouped.setdefault(key, []).append(i)

                    for _key, group in sorted(grouped.items(), key=lambda x: x[1][0]["score"]):
                        if len(group) >= 3:
                            # Render as grouped entry
                            sample = group[0]
                            sev = "CRITICAL" if sample["score"] < 30 else "WARNING"
                            ctry = extract_country(sample["host"])
                            hostnames = ", ".join(i["host"].split("-")[-1] if "-" in i["host"] else i["host"] for i in group[:8])
                            if len(group) > 8:
                                hostnames += f", +{len(group)-8} more"
                            parts.append(f"### {ctry} {sample['provider']} ({len(group)} servers) — {sample['score']}/100 [{sev}]")
                            for issue in sample["issues"]:
                                parts.append(f"- {issue}")
                            parts.append(f"  Servers: {hostnames}")
                            parts.append("")
                        else:
                            for i in group:
                                sev = "CRITICAL" if i["score"] < 30 else "WARNING" if i["score"] < 70 else "INFO"
                                parts.append(f"### {i['host']} — {i['score']}/100 [{sev}]")
                                parts.append(f"{i['product']} | {i['provider']} | CPU: {i['cpu_avg'] or 'N/A'}% | Traffic: {i['traffic_avg'] or 'N/A'} Mbps")
                                for issue in i["issues"]:
                                    parts.append(f"- {issue}")
                                parts.append("")

                    shown_count = sum(min(len(g), 1) if len(g) >= 3 else len(g) for g in grouped.values())
                    if shown_count > max_results:
                        parts = parts[:max_results * 5]  # rough line estimate
                        parts.append(f"\n*Showing top entries. {len(issues)} total issues.*")
                else:
                    shown = issues[:max_results]
                    for i in shown:
                        sev = "CRITICAL" if i["score"] < 30 else "WARNING" if i["score"] < 70 else "INFO"
                        service = f" | service: {i.get('service', 'N/A')}" if i.get("service") else ""
                        agent = " | Agent: DOWN" if i.get("agent") == "down" else ""
                        parts.append(f"### {i['host']} — {i['score']}/100 [{sev}]")
                        parts.append(f"{i['product']} | {i['provider']} | CPU: {i['cpu_avg'] or 'N/A'}% | Traffic: {i['traffic_avg'] or 'N/A'} Mbps{service}{agent}")
                        for issue in i["issues"]:
                            parts.append(f"- {issue}")
                        parts.append("")
                    if len(issues) > max_results:
                        parts.append(f"\n*{len(issues) - max_results} more entries omitted*")

                return "\n".join(parts)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error in health assessment: {e}"

    if "get_shutdown_candidates" not in skip:

        @mcp.tool()
        async def get_shutdown_candidates(
            product: str = "",
            tier: str = "",
            country: str = "",
            period: str = "7d",
            traffic_threshold: float = 5.0,
            cpu_threshold: float = 5.0,
            instance: str = "",
        ) -> str:
            """Find servers that can be safely shut down or need investigation.

            Args:
                product: Filter by product (optional)
                tier: Filter by tier (optional)
                country: Country code filter (optional)
                period: Analysis period (default: 7d)
                traffic_threshold: Mbps below which = idle (default: 5.0)
                cpu_threshold: CPU % below which = idle (default: 5.0)
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

                filtered = []
                for h in hosts:
                    prod, t = _classify_host(h.get("groups", []))
                    if product and product.lower() not in (prod or "").lower():
                        continue
                    if tier and tier.lower() not in (t or "").lower():
                        continue
                    if country and extract_country(h.get("host", "")).lower() != country.lower():
                        continue
                    h["_prod"] = prod
                    h["_tier"] = t
                    filtered.append(h)

                if not filtered:
                    return "No servers match the filters."

                hostids = [h["hostid"] for h in filtered]
                async def _empty_list():
                    return []

                trend_result = await fetch_trends_batch(client, hostids, ["cpu", "traffic"], period)
                trend_rows = trend_result[0] if not isinstance(trend_result[0], BaseException) else []
                service1_map = await fetch_service_status(client, hostids)

                dash_map = await fetch_host_dashboards(client)

                host_metrics: dict[str, dict] = {}
                for r in trend_rows:
                    host_metrics.setdefault(r.hostid, {})[r.metric] = r

                candidates = []
                for h in filtered:
                    hid = h["hostid"]
                    hm = host_metrics.get(hid, {})
                    cpu = hm.get("cpu")
                    traffic = hm.get("traffic")
                    service1_val = service1_map.get(hid)
                    ip = host_ip(h)

                    cpu_avg = cpu.avg if cpu else None
                    traffic_avg = traffic.avg if traffic else None
                    service = "DOWN" if service1_val == 0 else ("PARTIAL" if service1_val == -1 else ("OK" if service1_val == 1 else ""))

                    category = None
                    reason = ""

                    if traffic_avg is not None and traffic_avg < 1.0 and cpu_avg is not None and cpu_avg < 5.0:
                        category = "DEAD"
                        reason = f"Traffic {traffic_avg} Mbps, CPU {cpu_avg}%"
                    elif cpu_avg is not None and cpu_avg > 50 and traffic_avg is not None and traffic_avg < 1.0:
                        category = "ZOMBIE"
                        reason = f"CPU {cpu_avg}% but traffic {traffic_avg} Mbps"
                    elif service == "DOWN" and traffic_avg is not None and traffic_avg < 5.0:
                        category = "BROKEN"
                        reason = f"service DOWN, traffic {traffic_avg} Mbps"
                    elif (traffic_avg is not None and traffic_avg < traffic_threshold
                          and cpu_avg is not None and cpu_avg < cpu_threshold):
                        category = "IDLE"
                        reason = f"Traffic {traffic_avg} Mbps, CPU {cpu_avg}%"

                    if category:
                        dash = dash_map.get(hid, "-")
                        candidates.append({
                            "hostid": hid, "host": h["host"], "ip": ip,
                            "category": category, "reason": reason,
                            "country": extract_country(h.get("host", "")),
                            "product": h.get("_prod", ""), "tier": h.get("_tier", ""),
                            "provider": detect_provider(ip) if ip else "",
                            "cpu_avg": cpu_avg, "traffic_avg": traffic_avg, "service": service,
                            "dash": dash,
                        })

                if not candidates:
                    return f"No shutdown candidates among {len(filtered)} servers."

                # Peer-headroom check: can the cohort absorb each candidate's load?
                # Cohort = same product+tier+country, excluding self AND other candidates.
                candidate_ids = {c["hostid"] for c in candidates}
                cohorts: dict[tuple[str, str, str], list[dict]] = {}
                for h in filtered:
                    if h["hostid"] in candidate_ids:
                        continue  # peers must be live, not also-shutdown
                    cc = extract_country(h.get("host", ""))
                    key = (h.get("_prod", ""), h.get("_tier", ""), cc)
                    traffic = host_metrics.get(h["hostid"], {}).get("traffic")
                    cohorts.setdefault(key, []).append({
                        "peak": traffic.peak if traffic else None,
                        "avg": traffic.avg if traffic else None,
                    })
                for c in candidates:
                    cohort = cohorts.get((c["product"], c["tier"], c["country"]), [])
                    label, headroom = _compute_shutdown_safety(c["traffic_avg"], cohort)
                    c["safety"] = label
                    c["peer_headroom_mbps"] = headroom
                    c["peer_count"] = len(cohort)

                order = {"DEAD": 0, "ZOMBIE": 1, "BROKEN": 2, "IDLE": 3}
                candidates.sort(key=lambda c: (order.get(c["category"], 9), c.get("traffic_avg") or 0))

                counts = {}
                for c in candidates:
                    counts[c["category"]] = counts.get(c["category"], 0) + 1

                parts = [
                    f"**Shutdown Candidates ({period}): {len(candidates)} of {len(filtered)} servers**\n",
                    " | ".join(f"{k}: {v}" for k, v in sorted(counts.items(), key=lambda x: order.get(x[0], 9))),
                ]

                # Check if all on same dashboard (or all orphaned)
                dashes = {c["dash"] for c in candidates}
                if dashes == {"-"}:
                    parts.append("All orphaned (not on any dashboard)")

                # Compact output: group by category, list names with key metric
                for cat in ["DEAD", "ZOMBIE", "BROKEN", "IDLE"]:
                    group = [c for c in candidates if c["category"] == cat]
                    if not group:
                        continue
                    if cat == "DEAD":
                        # DEAD has zero traffic — safety is always SAFE unless SOLO
                        entries = []
                        for c in group:
                            sf = c["safety"]
                            badge = f" [{sf}]" if sf in {"SOLO", "RISKY"} else ""
                            entries.append(f"{c['host']}{badge}")
                        parts.append(f"\n**DEAD ({len(group)}):** {', '.join(entries)}")
                    else:
                        entries = []
                        for c in group:
                            t = f"{c['traffic_avg']:.1f}" if c["traffic_avg"] is not None else "?"
                            service = " service DOWN" if c["service"] == "DOWN" else ""
                            d = f" [{c['dash']}]" if c["dash"] != "-" else ""
                            sf = c["safety"]
                            if sf == "SAFE":
                                safety = f" SAFE ({c['peer_headroom_mbps']:.0f}Mbps headroom)"
                            elif sf == "RISKY":
                                safety = f" RISKY ({c['peer_headroom_mbps']:.0f}Mbps headroom)"
                            elif sf == "SOLO":
                                safety = " SOLO (no peers)"
                            else:
                                safety = ""
                            entries.append(f"{c['host']} ({t} Mbps{service}{d}){safety}")
                        parts.append(f"\n**{cat} ({len(group)}):** {', '.join(entries)}")

                # Shutdown-safety summary line
                solo = sum(1 for c in candidates if c["safety"] == "SOLO")
                risky = sum(1 for c in candidates if c["safety"] == "RISKY")
                if solo or risky:
                    parts.append(
                        f"\n*Peer-headroom: {solo} SOLO (no peers), "
                        f"{risky} RISKY (insufficient cohort capacity).*"
                    )

                return "\n".join(parts)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

    if "get_capacity_planning" not in skip:

        @mcp.tool()
        async def get_capacity_planning(
            product: str = "",
            tier: str = "",
            country: str = "",
            period: str = "7d",
            cpu_threshold: float = 70.0,
            traffic_threshold: float = 600.0,
            min_priority: str = "",
            max_results: int = 30,
            instance: str = "",
        ) -> str:
            """Find overloaded servers that need capacity increase or upgrade.

            Args:
                product: Filter by product (optional)
                tier: Filter by tier (optional)
                country: Country code filter (optional)
                period: Analysis period (default: 7d)
                cpu_threshold: CPU avg % for overloaded (default: 70)
                traffic_threshold: Mbps for saturated (default: 600)
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
                filtered = []
                for h in hosts:
                    prod, t = _classify_host(h.get("groups", []))
                    if product and product.lower() not in (prod or "").lower():
                        continue
                    if tier and tier.lower() not in (t or "").lower():
                        continue
                    if country and extract_country(h.get("host", "")).lower() != country.lower():
                        continue
                    h["_prod"] = prod
                    h["_tier"] = t
                    filtered.append(h)

                if not filtered:
                    return "No servers match the filters."

                p_map = build_parent_map(hosts)
                hostids = [h["hostid"] for h in filtered]
                parent_ids = list({p_map[h] for h in hostids if h in p_map} - set(hostids))

                trend_rows, _ = await fetch_trends_batch(
                    client, hostids + parent_ids, ["cpu", "traffic", "load"], period,
                )
                host_metrics: dict[str, dict] = {}
                for r in trend_rows:
                    host_metrics.setdefault(r.hostid, {})[r.metric] = r
                # Inherit parent trends for child hosts
                for hid in hostids:
                    pid = p_map.get(hid)
                    if pid and hid not in host_metrics and pid in host_metrics:
                        host_metrics[hid] = host_metrics[pid]

                efficiencies = []
                for hm in host_metrics.values():
                    cpu = hm.get("cpu")
                    traffic = hm.get("traffic")
                    if cpu and traffic and traffic.avg > 10:
                        efficiencies.append(cpu.avg / (traffic.avg / 100))
                eff_median = _median(efficiencies) if efficiencies else 0

                candidates = []
                for h in filtered:
                    hid = h["hostid"]
                    hm = host_metrics.get(hid, {})
                    cpu = hm.get("cpu")
                    traffic = hm.get("traffic")
                    load = hm.get("load")
                    ip = host_ip(h)
                    signals = []
                    severity = 0

                    if cpu and cpu.avg > cpu_threshold:
                        kind = "chronic" if cpu.min_val > 50 else "frequent"
                        signals.append(f"CPU {kind}: avg {cpu.avg}%, min {cpu.min_val}%")
                        severity += 3 if kind == "chronic" else 2

                    if traffic and traffic.avg > traffic_threshold:
                        headroom = BW_MAX - traffic.peak
                        signals.append(f"BW saturated: avg {traffic.avg}, peak {traffic.peak} Mbps (headroom: {headroom:.0f})")
                        severity += 3 if headroom < 100 else 2

                    if cpu and traffic and traffic.avg > 10 and eff_median > 0:
                        eff = cpu.avg / (traffic.avg / 100)
                        if eff > eff_median * 2:
                            signals.append(f"Inefficient: {eff:.1f}% CPU/100Mbps (peer: {eff_median:.1f}%)")
                            severity += 2

                    if traffic and traffic.trend_dir == "rising":
                        signals.append(f"Traffic rising: {traffic.current} vs avg {traffic.avg} Mbps")
                        severity += 1

                    if load and load.avg > 3:
                        signals.append(f"High load: avg {load.avg}")
                        severity += 1

                    if signals:
                        cat = "CRITICAL" if severity >= 5 else "HIGH" if severity >= 3 else "MEDIUM"
                        if severity >= 5:
                            action = "Add replicas or upgrade hardware"
                        elif "Inefficient" in " ".join(signals):
                            action = "Upgrade to faster hardware"
                        elif "rising" in " ".join(signals):
                            action = "Plan capacity increase"
                        elif "saturated" in " ".join(signals).lower():
                            action = "Upgrade NIC or add load balancer"
                        else:
                            action = "Monitor closely"

                        candidates.append({
                            "host": h["host"], "category": cat,
                            "product": h.get("_prod", ""), "tier": h.get("_tier", ""),
                            "provider": detect_provider(ip) if ip else "",
                            "country": extract_country(h["host"]),
                            "cpu_avg": cpu.avg if cpu else None,
                            "traffic_avg": traffic.avg if traffic else None,
                            "traffic_peak": traffic.peak if traffic else None,
                            "trend": traffic.trend_dir if traffic else "",
                            "signals": signals, "action": action, "severity": severity,
                        })

                if not candidates:
                    return f"No overloaded servers among {len(filtered)}."

                candidates.sort(key=lambda c: -c["severity"])

                # Filter by min_priority
                total_all = len(candidates)
                critical = sum(1 for c in candidates if c["category"] == "CRITICAL")
                high = sum(1 for c in candidates if c["category"] == "HIGH")
                medium = sum(1 for c in candidates if c["category"] == "MEDIUM")

                if min_priority:
                    priority_order = {"CRITICAL": 3, "HIGH": 2, "MEDIUM": 1}
                    min_val = priority_order.get(min_priority.upper(), 0)
                    candidates = [c for c in candidates if priority_order.get(c["category"], 0) >= min_val]

                shown = candidates[:max_results]
                omitted = len(candidates) - len(shown)

                parts = [
                    f"**Capacity Planning ({period}): {total_all} servers need attention**\n",
                    f"CRITICAL: {critical} | HIGH: {high} | MEDIUM: {medium}\n",
                    "| Priority | Server | Country | Product | CPU Avg | Traffic Avg | Peak | Trend | Action |",
                    "|----------|--------|---------|---------|---------|-------------|------|-------|--------|",
                ]
                for c in shown:
                    cpu_a = f"{c['cpu_avg']:.1f}%" if c["cpu_avg"] else "N/A"
                    t_a = f"{c['traffic_avg']:.0f}" if c["traffic_avg"] else "N/A"
                    t_p = f"{c['traffic_peak']:.0f}" if c["traffic_peak"] else "N/A"
                    parts.append(
                        f"| {c['category']} | {c['host']} | {c['country']} | "
                        f"{c['product']}/{c['tier']} | {cpu_a} | "
                        f"{t_a} Mbps | {t_p} Mbps | {c['trend']} | {c['action']} |"
                    )

                if omitted:
                    parts.append(f"\n*{omitted} more servers omitted (use max_results to see all)*")

                parts.append("\n### Top Issues\n")
                for c in shown[:5]:
                    parts.append(f"**{c['host']}** ({c['provider']}):")
                    for s in c["signals"]:
                        parts.append(f"  - {s}")

                return "\n".join(parts)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error in capacity planning: {e}"
