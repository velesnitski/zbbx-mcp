"""Batch trend tools: historical metrics, dashboards, comparison, health, capacity."""


import httpx

from zbbx_mcp.classify import classify_host as _classify_host
from zbbx_mcp.classify import detect_provider
from zbbx_mcp.data import extract_country, fetch_trends_batch
from zbbx_mcp.excel import BW_MAX
from zbbx_mcp.resolver import InstanceResolver


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()):

    if "get_trends_batch" not in skip:

        @mcp.tool()
        async def get_trends_batch(
            country: str = "",
            product: str = "",
            tier: str = "",
            group: str = "",
            metrics: str = "cpu,traffic,load",
            period: str = "7d",
            aggregation: str = "summary",
            max_results: int = 50,
            instance: str = "",
        ) -> str:
            """Get trend data (avg/peak/min) for multiple servers and metrics.

            Shows historical averages instead of point-in-time snapshots.
            Uses batch API calls (3 total) regardless of server count.

            Args:
                country: Filter by country code in hostname (optional)
                product: Filter by product name (optional)
                tier: Filter by tier name (optional)
                group: Filter by Zabbix host group (optional)
                metrics: Comma-separated: cpu, traffic, load, memory (default: cpu,traffic,load)
                period: Time period: 1d, 7d, 30d (default: 7d)
                aggregation: 'summary' (default) or 'daily' for per-day breakdown
                max_results: Maximum servers (default: 50)
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)

                # Get and filter hosts
                params = {
                    "output": ["hostid", "host"],
                    "selectGroups": ["name"],
                    "filter": {"status": "0"},
                    "sortfield": "host",
                }
                if group:
                    groups = await client.call("hostgroup.get", {
                        "output": ["groupid"], "filter": {"name": [group]},
                    })
                    if not groups:
                        return f"Host group '{group}' not found."
                    params["groupids"] = [g["groupid"] for g in groups]

                hosts = await client.call("host.get", params)

                filtered_ids = []
                for h in hosts:
                    prod, t = _classify_host(h.get("groups", []))
                    if product and product.lower() not in (prod or "").lower():
                        continue
                    if tier and tier.lower() not in (t or "").lower():
                        continue
                    if country and extract_country(h.get("host", "")).lower() != country.lower():
                        continue
                    filtered_ids.append(h["hostid"])
                    if len(filtered_ids) >= max_results:
                        break

                if not filtered_ids:
                    return "No servers match the filters."

                metric_list = [m.strip() for m in metrics.split(",") if m.strip()]
                trend_rows, host_map = await fetch_trends_batch(
                    client, filtered_ids, metric_list, period,
                )

                if not trend_rows:
                    return f"No trend data for the last {period}."

                units = {"cpu": "%", "traffic": "Mbps", "load": "", "memory": "GB"}
                server_count = len(set(r.hostid for r in trend_rows))

                if aggregation == "daily":
                    # Collect all unique days across all rows
                    all_days = sorted(set(
                        d for r in trend_rows for d in r.daily
                    ))
                    if not all_days:
                        return "No daily data available."

                    day_cols = " | ".join(all_days)
                    parts = [
                        f"**Daily Trends ({period}) for {server_count} servers**\n",
                        f"| Server | Metric | {day_cols} |",
                        f"|--------|--------|{'---|' * len(all_days)}",
                    ]
                    for r in trend_rows:
                        u = units.get(r.metric, "")
                        vals = " | ".join(
                            f"{r.daily.get(d, '')} {u}".strip() if d in r.daily else ""
                            for d in all_days
                        )
                        parts.append(f"| {r.hostname} | {r.metric} | {vals} |")

                    return "\n".join(parts)
                else:
                    parts = [
                        f"**Trends ({period}) for {server_count} servers**\n",
                        "| Server | Metric | Avg | Peak | Min | Current | Trend |",
                        "|--------|--------|-----|------|-----|---------|-------|",
                    ]
                    for r in trend_rows:
                        u = units.get(r.metric, "")
                        parts.append(
                            f"| {r.hostname} | {r.metric} | "
                            f"{r.avg} {u} | {r.peak} {u} | {r.min_val} {u} | "
                            f"{r.current} {u} | {r.trend_dir} |"
                        )

                    return "\n".join(parts)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

    if "get_server_dashboard" not in skip:

        @mcp.tool()
        async def get_server_dashboard(
            host: str,
            period: str = "7d",
            aggregation: str = "daily",
            instance: str = "",
        ) -> str:
            """Get a per-server dashboard showing metric trends.

            Args:
                host: Hostname or host ID
                period: Time period: 1d, 7d, 30d (default: 7d)
                aggregation: 'daily' (default) for per-day table, 'summary' for period totals
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)

                # Resolve hostname
                if not host.isdigit():
                    lookup = await client.call("host.get", {
                        "output": ["hostid", "host"],
                        "filter": {"host": [host]},
                    })
                    if not lookup:
                        lookup = await client.call("host.get", {
                            "output": ["hostid", "host"],
                            "search": {"host": host, "name": host},
                            "searchByAny": True, "searchWildcardsEnabled": True,
                            "limit": 1,
                        })
                    if not lookup:
                        return f"Host '{host}' not found."
                    hostid = lookup[0]["hostid"]
                    hostname = lookup[0]["host"]
                else:
                    hostid = host
                    h = await client.call("host.get", {
                        "hostids": [hostid], "output": ["host"],
                    })
                    hostname = h[0]["host"] if h else hostid

                trend_rows, _ = await fetch_trends_batch(
                    client, [hostid], ["cpu", "traffic", "load", "memory"], period,
                )

                if not trend_rows:
                    return f"No trend data for '{hostname}' in the last {period}."

                units = {"cpu": "%", "traffic": "Mbps", "traffic_out": "Mbps", "load": "", "memory": "GB"}

                parts = [f"# Dashboard: {hostname} (last {period})\n"]

                # Summary line per metric
                for r in trend_rows:
                    u = units.get(r.metric, "")
                    parts.append(
                        f"**{r.metric.title()}:** "
                        f"avg {r.avg} {u} | peak {r.peak} {u} | "
                        f"min {r.min_val} {u} | current {r.current} {u} | "
                        f"trend: {r.trend_dir}"
                    )

                # Daily breakdown table
                if aggregation == "daily":
                    all_days = sorted(set(d for r in trend_rows for d in r.daily))
                    if all_days:
                        parts.append("\n## Daily Breakdown\n")
                        day_cols = " | ".join(all_days)
                        parts.append(f"| Metric | {day_cols} |")
                        parts.append(f"|--------|{'---|' * len(all_days)}")
                        for r in trend_rows:
                            u = units.get(r.metric, "")
                            vals = " | ".join(
                                f"{r.daily.get(d, '')}" for d in all_days
                            )
                            parts.append(f"| {r.metric} ({u}) | {vals} |")

                return "\n".join(parts)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

    if "compare_servers" not in skip:

        @mcp.tool()
        async def compare_servers(
            hosts: str,
            metrics: str = "cpu,traffic,load",
            period: str = "7d",
            instance: str = "",
        ) -> str:
            """Compare multiple servers side-by-side with trend data.

            Args:
                hosts: Comma-separated hostnames (e.g., 'srv-nl01,srv-de01')
                metrics: Comma-separated: cpu, traffic, traffic_out, load, memory
                period: Time period: 1d, 7d, 30d (default: 7d)
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                from typing import Any
                client = resolver.resolve(instance)
                host_names = [h.strip() for h in hosts.split(",") if h.strip()]

                if len(host_names) < 2:
                    return "Need at least 2 hostnames to compare."

                lookup = await client.call("host.get", {
                    "output": ["hostid", "host"],
                    "selectInterfaces": ["ip"],
                    "filter": {"host": host_names},
                })
                if len(lookup) < 2:
                    return f"Found only {len(lookup)} hosts. Need at least 2."

                hostids = [h["hostid"] for h in lookup]
                metric_list = [m.strip() for m in metrics.split(",") if m.strip()]
                trend_rows, _ = await fetch_trends_batch(client, hostids, metric_list, period)

                if not trend_rows:
                    return f"No trend data for the last {period}."

                by_metric: dict[str, dict[str, Any]] = {}
                for r in trend_rows:
                    by_metric.setdefault(r.metric, {})[r.hostname] = r

                units = {"cpu": "%", "traffic": "Mbps", "traffic_out": "Mbps", "load": "", "memory": "GB"}
                server_names = [h["host"] for h in lookup]
                cols = " | ".join(server_names)

                parts = [
                    f"**Server Comparison ({period})**\n",
                    f"| Metric | {cols} |",
                    f"|--------|{'---|' * len(server_names)}",
                ]
                for mn in metric_list:
                    if mn not in by_metric:
                        continue
                    u = units.get(mn, "")
                    data = by_metric[mn]
                    vals_avg = [f"{data[n].avg} {u}" if n in data else "N/A" for n in server_names]
                    vals_peak = [f"{data[n].peak} {u}" if n in data else "N/A" for n in server_names]
                    vals_now = [f"{data[n].current} {u}" if n in data else "N/A" for n in server_names]
                    parts.append(f"| {mn} avg | {' | '.join(vals_avg)} |")
                    parts.append(f"| {mn} peak | {' | '.join(vals_peak)} |")
                    parts.append(f"| {mn} now | {' | '.join(vals_now)} |")

                # Efficiency metrics
                cpu_data = by_metric.get("cpu", {})
                traffic_data = by_metric.get("traffic", {})
                if cpu_data and traffic_data:
                    parts.append(f"\n| Efficiency | {cols} |")
                    parts.append(f"|------------|{'---|' * len(server_names)}")
                    # CPU per 100 Mbps
                    eff_vals = []
                    for n in server_names:
                        cpu_avg = cpu_data[n].avg if n in cpu_data else 0
                        traffic_avg = traffic_data[n].avg if n in traffic_data else 0
                        if traffic_avg > 0:
                            eff_vals.append(f"{cpu_avg / (traffic_avg / 100):.1f}%")
                        else:
                            eff_vals.append("N/A")
                    parts.append(f"| CPU per 100 Mbps | {' | '.join(eff_vals)} |")
                    # Traffic headroom
                    headroom_vals = []
                    for n in server_names:
                        if n in traffic_data:
                            headroom = 800 - traffic_data[n].peak  # vs BW_MAX
                            headroom_vals.append(f"{headroom:.0f} Mbps")
                        else:
                            headroom_vals.append("N/A")
                    parts.append(f"| BW headroom (vs 800) | {' | '.join(headroom_vals)} |")

                # Provider/country info
                parts.append(f"\n| Info | {cols} |")
                parts.append(f"|------|{'---|' * len(server_names)}")
                provs = []
                for h in lookup:
                    ip = next((i["ip"] for i in h.get("interfaces", []) if i.get("ip") != "127.0.0.1"), "")
                    provs.append(detect_provider(ip) if ip else "?")
                parts.append(f"| Provider | {' | '.join(provs)} |")
                parts.append(f"| Country | {' | '.join(extract_country(h['host']) for h in lookup)} |")

                return "\n".join(parts)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error comparing servers: {e}"

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
            """Automated health assessment with per-server scoring and issue detection.

            Analyzes trends to detect:
            - Chronic CPU overload (avg > 80%, min > 50%)
            - Traffic drops (current < 70% of period avg)
            - Hardware inefficiency (CPU per 100 Mbps > 3x peer median)
            - Near bandwidth saturation (peak > 700 Mbps)

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
                    grps = await client.call("hostgroup.get", {
                        "output": ["groupid"], "filter": {"name": [group]},
                    })
                    if not grps:
                        return f"Group '{group}' not found."
                    params["groupids"] = [g["groupid"] for g in grps]

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

                import asyncio
                trend_rows_task = fetch_trends_batch(
                    client, hostids, ["cpu", "traffic", "load"], period,
                )
                # Task 36: Fetch service health alongside trends
                service1_task = client.call("item.get", {
                    "hostids": hostids,
                    "output": ["hostid", "lastvalue"],
                    "filter": {"key_": "service_primary_check[{HOST.IP}]", "status": "0"},
                })

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
                from statistics import median as _median
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
                        ip = next((i["ip"] for i in h.get("interfaces", []) if i.get("ip") != "127.0.0.1"), "")
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

            Categories:
            - DEAD: traffic < 1 Mbps AND CPU < 5% — shutdown immediately
            - BROKEN: service DOWN AND traffic near zero — fix or shutdown
            - ZOMBIE: CPU > 50% but traffic < 1 Mbps — stuck process
            - IDLE: traffic < threshold AND CPU < threshold — review

            Args:
                product: Filter by product name (optional)
                tier: Filter by tier name (optional)
                country: Filter by country code (optional)
                period: Analysis period (default: 7d)
                traffic_threshold: Mbps below which = idle (default: 5.0)
                cpu_threshold: CPU % below which = idle (default: 5.0)
                instance: Zabbix instance name (optional)
            """
            try:
                import asyncio as _aio
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
                (trend_rows, _), service1_items = await _aio.gather(
                    fetch_trends_batch(client, hostids, ["cpu", "traffic"], period),
                    client.call("item.get", {
                        "hostids": hostids,
                        "output": ["hostid", "lastvalue"],
                        "filter": {"key_": "service_primary_check[{HOST.IP}]", "status": "0"},
                    }),
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
                    ip = next((i["ip"] for i in h.get("interfaces", []) if i.get("ip") != "127.0.0.1"), "")

                    cpu_avg = cpu.avg if cpu else None
                    traffic_avg = traffic.avg if traffic else None
                    service = "DOWN" if service1_val == 0 else ("OK" if service1_val == 1 else "")

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
                        candidates.append({
                            "host": h["host"], "ip": ip, "category": category, "reason": reason,
                            "product": h.get("_prod", ""), "tier": h.get("_tier", ""),
                            "provider": detect_provider(ip) if ip else "",
                            "cpu_avg": cpu_avg, "traffic_avg": traffic_avg, "service": service,
                        })

                if not candidates:
                    return f"No shutdown candidates among {len(filtered)} servers."

                order = {"DEAD": 0, "ZOMBIE": 1, "BROKEN": 2, "IDLE": 3}
                candidates.sort(key=lambda c: (order.get(c["category"], 9), c.get("traffic_avg") or 0))

                counts = {}
                for c in candidates:
                    counts[c["category"]] = counts.get(c["category"], 0) + 1

                parts = [
                    f"**Shutdown Candidates ({period}): {len(candidates)} of {len(filtered)} servers**\n",
                    " | ".join(f"{k}: {v}" for k, v in sorted(counts.items(), key=lambda x: order.get(x[0], 9))),
                    "",
                    "| Category | Server | IP | Product | Provider | CPU% | Traffic | service | Reason |",
                    "|----------|--------|----|---------|----------|------|---------|-----|--------|",
                ]
                for c in candidates:
                    cpu_s = f"{c['cpu_avg']:.1f}" if c["cpu_avg"] is not None else "N/A"
                    t_s = f"{c['traffic_avg']:.1f}" if c["traffic_avg"] is not None else "N/A"
                    parts.append(
                        f"| {c['category']} | {c['host']} | {c.get('ip', '')} | {c['product']}/{c['tier']} | "
                        f"{c['provider']} | {cpu_s}% | {t_s} Mbps | {c['service']} | {c['reason']} |"
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
            """Find overloaded servers that need capacity increase or hardware upgrade.

            Detects:
            - OVERLOADED: sustained high CPU (avg > threshold, never drops below 50%)
            - SATURATED: traffic approaching NIC limit
            - INEFFICIENT: high CPU per traffic ratio vs peers (needs upgrade)
            - GROWING: traffic trend rising >20% (will hit limit soon)

            Args:
                product: Filter by product name (optional)
                tier: Filter by tier name (optional)
                country: Filter by country code (optional)
                period: Analysis period (default: 7d)
                cpu_threshold: CPU avg % above which = overloaded (default: 70%)
                traffic_threshold: Traffic avg Mbps above which = saturated (default: 600)
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
                trend_rows, _ = await fetch_trends_batch(
                    client, hostids, ["cpu", "traffic", "load"], period,
                )
                host_metrics: dict[str, dict] = {}
                for r in trend_rows:
                    host_metrics.setdefault(r.hostid, {})[r.metric] = r

                from statistics import median as _median
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
                    ip = next((i["ip"] for i in h.get("interfaces", []) if i.get("ip") != "127.0.0.1"), "")
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
