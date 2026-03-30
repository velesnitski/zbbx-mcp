"""Traffic anomaly detection: peer comparison, trend drops, connection correlation."""

import asyncio
import time as _time
from statistics import median, stdev

import httpx

from zbbx_mcp.classify import classify_host as _classify_host
from zbbx_mcp.classify import detect_provider
from zbbx_mcp.data import KEY_CONNECTIONS, TRAFFIC_IN_KEYS, countries_for_region, extract_country
from zbbx_mcp.resolver import InstanceResolver


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()) -> None:

    if "detect_traffic_anomalies" not in skip:

        @mcp.tool()
        async def detect_traffic_anomalies(
            group: str = "",
            product: str = "",
            country: str = "",
            region: str = "",
            threshold_pct: float = 20.0,
            min_peers: int = 3,
            instance: str = "",
        ) -> str:
            """Detect servers with abnormally low traffic compared to their peer group.

            Args:
                group: Zabbix host group (optional)
                product: Filter by product (optional)
                country: Country code filter (optional)
                region: LATAM, APAC, EMEA, NA, CIS, ALL (optional)
                threshold_pct: % of group median to flag (default: 20)
                min_peers: Min peers for comparison (default: 3)
                instance: Zabbix instance (optional)
            """
            try:
                client = resolver.resolve(instance)

                # Phase 1: hosts (1 call)
                hosts = await client.call("host.get", {
                    "output": ["hostid", "host"],
                    "selectGroups": ["name"],
                    "selectInterfaces": ["ip"],
                    "filter": {"status": "0"},
                })
                host_map = {h["hostid"]: h for h in hosts}
                all_ids = list(host_map.keys())

                # Phase 2: traffic + connections + CPU in parallel (3 calls)
                traffic_task = client.call("item.get", {
                    "hostids": all_ids,
                    "output": ["hostid", "lastvalue"],
                    "filter": {"key_": TRAFFIC_IN_KEYS, "status": "0"},
                })
                async def _empty():
                    return []

                conn_task = client.call("item.get", {
                    "hostids": all_ids,
                    "output": ["hostid", "lastvalue"],
                    "filter": {"key_": KEY_CONNECTIONS, "status": "0"},
                }) if KEY_CONNECTIONS else _empty()
                cpu_task = client.call("item.get", {
                    "hostids": all_ids,
                    "output": ["hostid", "lastvalue"],
                    "filter": {"key_": "system.cpu.util[,idle]", "status": "0"},
                })

                traffic_items, conn_items, cpu_items = await asyncio.gather(
                    traffic_task, conn_task, cpu_task
                )

                # Build per-host metrics (max traffic across interfaces)
                host_traffic: dict[str, float] = {}
                for i in traffic_items:
                    try:
                        val = float(i.get("lastvalue", "0"))
                    except (ValueError, TypeError):
                        continue
                    hid = i["hostid"]
                    if val > host_traffic.get(hid, 0):
                        host_traffic[hid] = val

                host_conns: dict[str, float] = {}
                for i in conn_items:
                    try:
                        host_conns[i["hostid"]] = float(i.get("lastvalue", "0"))
                    except (ValueError, TypeError):
                        pass

                host_cpu: dict[str, float] = {}
                for i in cpu_items:
                    try:
                        host_cpu[i["hostid"]] = round(100 - float(i["lastvalue"]), 1)
                    except (ValueError, TypeError):
                        pass

                # Group hosts by Zabbix group
                group_members: dict[str, list[str]] = {}
                for h in hosts:
                    prod, tier = _classify_host(h.get("groups", []))
                    if product and product.lower() not in (prod or "").lower():
                        continue
                    if country and extract_country(h.get("host", "")).lower() != country.lower():
                        continue
                    if region:
                        rc = countries_for_region(region)
                        if rc and extract_country(h.get("host", "")).upper() not in rc:
                            continue
                    for g in h.get("groups", []):
                        gname = g["name"]
                        if group and group.lower() != gname.lower():
                            continue
                        group_members.setdefault(gname, []).append(h["hostid"])

                # Analyze each group
                all_anomalies = []
                group_stats = []

                for gname, member_ids in sorted(group_members.items()):
                    # Get traffic values for members with data
                    peer_traffic = {
                        hid: host_traffic[hid]
                        for hid in member_ids
                        if hid in host_traffic
                    }

                    # Need enough peers with non-zero traffic
                    active_peers = {hid: v for hid, v in peer_traffic.items() if v > 0}
                    if len(active_peers) < min_peers:
                        continue

                    values = list(active_peers.values())
                    med = median(values)
                    avg = sum(values) / len(values)
                    sd = stdev(values) if len(values) >= 2 else 0
                    threshold = med * (threshold_pct / 100)

                    group_stats.append({
                        "group": gname,
                        "peers": len(active_peers),
                        "total": len(member_ids),
                        "median_mbps": med / 1e6,
                        "anomalies": 0,
                    })

                    for hid in member_ids:
                        traffic = host_traffic.get(hid, 0)
                        conns = host_conns.get(hid, 0)
                        cpu = host_cpu.get(hid)
                        h = host_map[hid]
                        hostname = h["host"]
                        ip = next((i["ip"] for i in h.get("interfaces", []) if i.get("ip") != "127.0.0.1"), "")
                        provider = detect_provider(ip) if ip else ""

                        # Determine anomaly type
                        reasons = []

                        # Signal 1: Below threshold of peer median
                        if traffic < threshold and hid in host_traffic:
                            pct = (traffic / med * 100) if med > 0 else 0
                            reasons.append(f"Traffic {pct:.0f}% of group median ({med/1e6:.1f} Mbps)")

                        # Signal 2: Has connections but very low traffic (tunnel broken)
                        if conns > 0 and traffic < threshold:
                            reasons.append(f"{conns:.0f} active connections but only {traffic/1e6:.2f} Mbps")

                        # Signal 3: Statistical outlier (> 2 SD below mean)
                        if sd > 0 and traffic < (avg - 2 * sd) and traffic > 0:
                            reasons.append(f"Statistical outlier (>{2}σ below mean)")

                        # Signal 4: Zero traffic but CPU is active (server is up but not forwarding)
                        if traffic == 0 and cpu is not None and cpu > 5 and hid in host_traffic:
                            reasons.append(f"Zero traffic but CPU at {cpu}%")

                        if reasons:
                            severity = "HIGH" if len(reasons) >= 2 else "MEDIUM"
                            if conns > 0 and traffic < threshold:
                                severity = "CRITICAL"  # Active conns + low traffic = broken tunnel

                            all_anomalies.append({
                                "host": hostname,
                                "group": gname,
                                "ip": ip,
                                "provider": provider,
                                "traffic_mbps": traffic / 1e6,
                                "connections": conns,
                                "cpu_pct": cpu,
                                "severity": severity,
                                "reasons": reasons,
                                "peer_median_mbps": med / 1e6,
                            })
                            group_stats[-1]["anomalies"] += 1

                if not all_anomalies and not group_stats:
                    return "No groups with enough peers for analysis."

                # Sort: CRITICAL first, then HIGH, then MEDIUM
                sev_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2}
                all_anomalies.sort(key=lambda a: (sev_order.get(a["severity"], 3), -a["peer_median_mbps"]))

                # Format output
                parts = []
                if all_anomalies:
                    parts.append(f"**{len(all_anomalies)} traffic anomalies detected**\n")

                    # Group anomalies by severity
                    for sev in ("CRITICAL", "HIGH", "MEDIUM"):
                        sev_items = [a for a in all_anomalies if a["severity"] == sev]
                        if not sev_items:
                            continue
                        icon = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡"}[sev]
                        parts.append(f"### {icon} {sev} ({len(sev_items)})\n")
                        for a in sev_items:
                            conns_str = f", {a['connections']:.0f} conns" if a["connections"] > 0 else ""
                            cpu_str = f", CPU {a['cpu_pct']}%" if a["cpu_pct"] is not None else ""
                            parts.append(
                                f"- **{a['host']}** ({a['provider']}) — "
                                f"{a['traffic_mbps']:.2f} Mbps{conns_str}{cpu_str}\n"
                                f"  Group: {a['group']} (median: {a['peer_median_mbps']:.1f} Mbps)\n"
                                f"  Reasons: {'; '.join(a['reasons'])}"
                            )
                        parts.append("")
                else:
                    parts.append("**No traffic anomalies detected.**\n")

                # Group summary table
                active_groups = [g for g in group_stats if g["peers"] >= min_peers]
                if active_groups:
                    parts.append("### Group Summary\n")
                    parts.append("| Group | Peers | Median Mbps | Anomalies |")
                    parts.append("|-------|-------|-------------|-----------|")
                    for g in sorted(active_groups, key=lambda x: -x["median_mbps"]):
                        parts.append(
                            f"| {g['group']} | {g['peers']}/{g['total']} | "
                            f"{g['median_mbps']:.1f} | {g['anomalies']} |"
                        )

                return "\n".join(parts)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

    if "get_traffic_report" not in skip:

        @mcp.tool()
        async def get_traffic_report(
            group: str = "",
            product: str = "",
            tier: str = "",
            country: str = "",
            sort_by: str = "traffic",
            max_results: int = 50,
            instance: str = "",
        ) -> str:
            """Traffic report with connections and bandwidth per client.

            Args:
                group: Filter by Zabbix host group (optional)
                product: Filter by product name (optional)
                tier: Filter by tier name (optional)
                country: Filter by country code in hostname (optional)
                sort_by: Sort by 'traffic' (desc), 'bw_per_client', or 'connections' (default: traffic)
                max_results: Maximum results (default: 50)
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)

                hosts = await client.call("host.get", {
                    "output": ["hostid", "host"],
                    "selectGroups": ["name"],
                    "selectInterfaces": ["ip"],
                    "filter": {"status": "0"},
                })
                host_map = {h["hostid"]: h for h in hosts}
                all_ids = list(host_map.keys())

                async def _empty_list():
                    return []

                traffic_items, conn_items = await asyncio.gather(
                    client.call("item.get", {
                        "hostids": all_ids,
                        "output": ["hostid", "lastvalue"],
                        "filter": {"key_": TRAFFIC_IN_KEYS, "status": "0"},
                    }),
                    client.call("item.get", {
                        "hostids": all_ids,
                        "output": ["hostid", "lastvalue"],
                        "filter": {"key_": KEY_CONNECTIONS, "status": "0"},
                    }) if KEY_CONNECTIONS else _empty_list(),
                )

                host_traffic: dict[str, float] = {}
                for i in traffic_items:
                    try:
                        val = float(i.get("lastvalue", "0"))
                    except (ValueError, TypeError):
                        continue
                    hid = i["hostid"]
                    if val > host_traffic.get(hid, 0):
                        host_traffic[hid] = val

                host_conns: dict[str, float] = {}
                for i in conn_items:
                    try:
                        host_conns[i["hostid"]] = float(i.get("lastvalue", "0"))
                    except (ValueError, TypeError):
                        pass

                rows = []
                for hid, traffic in host_traffic.items():
                    h = host_map.get(hid)
                    if not h:
                        continue
                    prod, host_tier = _classify_host(h.get("groups", []))
                    if product and product.lower() not in (prod or "").lower():
                        continue
                    if tier and tier.lower() not in (host_tier or "").lower():
                        continue
                    if group:
                        if not any(g["name"].lower() == group.lower() for g in h.get("groups", [])):
                            continue
                    if country and extract_country(h.get("host", "")).lower() != country.lower():
                        continue

                    conns = host_conns.get(hid, 0)
                    bw_per_client = (traffic / conns) if conns > 0 else 0
                    ip = next((i["ip"] for i in h.get("interfaces", []) if i.get("ip") != "127.0.0.1"), "")

                    rows.append({
                        "host": h["host"],
                        "ip": ip,
                        "provider": detect_provider(ip) if ip else "",
                        "product": prod or "",
                        "tier": host_tier or "",
                        "traffic": traffic,
                        "connections": conns,
                        "bw_per_client": bw_per_client,
                    })

                # Sort
                if sort_by == "connections":
                    rows.sort(key=lambda r: -r["connections"])
                elif sort_by == "bw_per_client":
                    rows.sort(key=lambda r: -r["bw_per_client"])
                else:
                    rows.sort(key=lambda r: -r["traffic"])

                rows = rows[:max_results]

                if not rows:
                    return "No traffic data found."

                parts = [
                    "| Server | Product | Provider | Traffic | Connections | BW/Client |",
                    "|--------|---------|----------|---------|-------------|-----------|",
                ]
                for r in rows:
                    t = f"{r['traffic']/1e6:.1f} Mbps"
                    c = f"{r['connections']:.0f}" if r["connections"] > 0 else "0"
                    bw = f"{r['bw_per_client']/1e3:.0f} Kbps" if r["bw_per_client"] > 0 else "–"
                    parts.append(
                        f"| {r['host']} | {r['product']}/{r['tier']} | "
                        f"{r['provider']} | {t} | {c} | {bw} |"
                    )

                header = f"**Traffic Report ({len(rows)} servers, sorted by {sort_by})**\n\n"
                return header + "\n".join(parts)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

    if "detect_traffic_drops" not in skip:

        @mcp.tool()
        async def detect_traffic_drops(
            group: str = "",
            product: str = "",
            country: str = "",
            region: str = "",
            drop_pct: float = 50.0,
            baseline_days: int = 7,
            max_results: int = 50,
            instance: str = "",
        ) -> str:
            """Detect servers where traffic dropped significantly vs their baseline.

            Args:
                group: Zabbix host group (optional)
                product: Filter by product (optional)
                country: Country code filter (optional)
                region: LATAM, APAC, EMEA, NA, CIS, ALL (optional)
                drop_pct: Min drop % to flag (default: 50)
                baseline_days: Days for baseline (default: 7)
                max_results: Max results (default: 50)
                instance: Zabbix instance (optional)
            """
            try:
                client = resolver.resolve(instance)

                # Get hosts
                hosts = await client.call("host.get", {
                    "output": ["hostid", "host"],
                    "selectGroups": ["name"],
                    "selectInterfaces": ["ip"],
                    "filter": {"status": "0"},
                })
                host_map = {h["hostid"]: h for h in hosts}

                # Filter
                filtered_ids = []
                for h in hosts:
                    prod, _ = _classify_host(h.get("groups", []))
                    if product and product.lower() not in (prod or "").lower():
                        continue
                    if group and not any(g["name"].lower() == group.lower() for g in h.get("groups", [])):
                        continue
                    if country and extract_country(h.get("host", "")).lower() != country.lower():
                        continue
                    if region:
                        rc = countries_for_region(region)
                        if rc and extract_country(h.get("host", "")).upper() not in rc:
                            continue
                    filtered_ids.append(h["hostid"])

                if not filtered_ids:
                    return "No servers match the filter."

                # Get traffic items (need itemid + hostid + current value)
                traffic_items = await client.call("item.get", {
                    "hostids": filtered_ids,
                    "output": ["itemid", "hostid", "key_", "lastvalue", "value_type"],
                    "search": {"name": "Incoming network traffic"},
                    "filter": {"status": "0"},
                })

                # Pick the main interface per host (highest current traffic)
                host_main_item: dict[str, dict] = {}
                for i in traffic_items:
                    hid = i["hostid"]
                    try:
                        val = float(i.get("lastvalue", "0"))
                    except (ValueError, TypeError):
                        continue
                    current = host_main_item.get(hid)
                    if current is None or val > float(current.get("lastvalue", "0")):
                        host_main_item[hid] = i

                if not host_main_item:
                    return "No traffic items found."

                # Fetch trends for all main items in parallel
                now = int(_time.time())
                baseline_start = now - baseline_days * 86400
                day_ago = now - 86400

                # Batch trend fetch (all items at once)
                item_ids = [i["itemid"] for i in host_main_item.values()]
                trends = await client.call("trend.get", {
                    "itemids": item_ids,
                    "time_from": baseline_start,
                    "output": ["itemid", "clock", "value_avg", "value_max"],
                    "limit": len(item_ids) * baseline_days * 24,
                })

                # Build per-item trend data
                item_trends: dict[str, list] = {}
                for t in trends:
                    item_trends.setdefault(t["itemid"], []).append(t)

                # Analyze each host
                drops = []
                for hid, item in host_main_item.items():
                    iid = item["itemid"]
                    current = float(item.get("lastvalue", "0"))
                    t_data = item_trends.get(iid, [])

                    if not t_data:
                        continue

                    # Split trends: baseline (older) vs recent (last 24h)
                    baseline_records = [t for t in t_data if int(t["clock"]) < day_ago]
                    if not baseline_records:
                        continue

                    baseline_avg = sum(float(t["value_avg"]) for t in baseline_records) / len(baseline_records)
                    baseline_peak = max(float(t["value_max"]) for t in baseline_records)

                    if baseline_avg < 1e6:  # Skip servers with < 1 Mbps baseline
                        continue

                    # Calculate drop
                    drop = ((baseline_avg - current) / baseline_avg * 100) if baseline_avg > 0 else 0

                    if drop >= drop_pct:
                        h = host_map[hid]
                        hostname = h["host"]
                        ip = next((i["ip"] for i in h.get("interfaces", []) if i.get("ip") != "127.0.0.1"), "")
                        provider = detect_provider(ip) if ip else ""
                        prod, tier = _classify_host(h.get("groups", []))
                        groups = [g["name"] for g in h.get("groups", [])]

                        severity = "CRITICAL" if drop >= 80 else "HIGH" if drop >= 60 else "MEDIUM"

                        drops.append({
                            "host": hostname,
                            "ip": ip,
                            "provider": provider,
                            "product": prod or "",
                            "tier": tier or "",
                            "groups": ", ".join(groups),
                            "current_mbps": current / 1e6,
                            "baseline_avg_mbps": baseline_avg / 1e6,
                            "baseline_peak_mbps": baseline_peak / 1e6,
                            "drop_pct": drop,
                            "severity": severity,
                        })

                drops.sort(key=lambda d: -d["drop_pct"])
                drops = drops[:max_results]

                if not drops:
                    return (
                        f"No traffic drops >{drop_pct:.0f}% detected "
                        f"(compared to {baseline_days}-day baseline). "
                        f"Analyzed {len(host_main_item)} servers."
                    )

                parts = [
                    f"**{len(drops)} servers with traffic drops >{drop_pct:.0f}%** "
                    f"(vs {baseline_days}-day baseline)\n",
                    "| Server | Provider | Current | Baseline Avg | Peak | Drop | Severity |",
                    "|--------|----------|---------|-------------|------|------|----------|",
                ]

                for d in drops:
                    parts.append(
                        f"| {d['host']} | {d['provider']} | "
                        f"{d['current_mbps']:.1f} Mbps | "
                        f"{d['baseline_avg_mbps']:.1f} Mbps | "
                        f"{d['baseline_peak_mbps']:.1f} Mbps | "
                        f"**{d['drop_pct']:.0f}%** | {d['severity']} |"
                    )

                # Summary by provider (to spot ISP-level blocking)
                prov_drops: dict[str, list] = {}
                for d in drops:
                    prov_drops.setdefault(d["provider"], []).append(d)

                if len(prov_drops) > 1:
                    parts.append("\n### Drops by Provider (spot ISP-level blocking)\n")
                    for prov in sorted(prov_drops, key=lambda x: -len(prov_drops[x])):
                        plist = prov_drops[prov]
                        avg_drop = sum(d["drop_pct"] for d in plist) / len(plist)
                        parts.append(
                            f"- **{prov}**: {len(plist)} servers, avg drop {avg_drop:.0f}%"
                        )

                return "\n".join(parts)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"
