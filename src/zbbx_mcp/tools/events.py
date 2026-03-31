import time
from collections import defaultdict

import httpx

from zbbx_mcp.formatters import _ts, format_severity
from zbbx_mcp.resolver import InstanceResolver

EVENT_SOURCES = {"0": "Trigger", "1": "Discovery", "2": "Autoregistration", "3": "Internal"}
EVENT_VALUES = {
    "0": {"0": "OK", "1": "PROBLEM"},
    "1": {"0": "Up", "1": "Down", "2": "Discovered", "3": "Lost"},
    "2": {"0": "Registered"},
    "3": {"0": "Normal", "1": "Unknown"},
}


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()) -> None:

    if "get_events" not in skip:

        @mcp.tool()
        async def get_events(
            host_id: str = "",
            group: str = "",
            time_from: str = "",
            time_till: str = "",
            source: int = 0,
            max_results: int = 50,
            instance: str = "",
        ) -> str:
            """Get Zabbix events (all events, not just active problems).

            Args:
                host_id: Filter by host ID (optional)
                group: Filter by host group name (optional)
                time_from: Start time as Unix timestamp (optional)
                time_till: End time as Unix timestamp (optional)
                source: Event source: 0=trigger, 1=discovery, 2=autoregistration, 3=internal (default: 0)
                max_results: Maximum number of results (default: 50)
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)
                params = {
                    "output": ["eventid", "source", "object", "objectid", "clock",
                               "value", "acknowledged", "severity", "name"],
                    "sortfield": ["clock"],
                    "sortorder": ["DESC"],
                    "limit": max_results,
                    "source": source,
                }
                if host_id:
                    params["hostids"] = [host_id]
                if time_from:
                    params["time_from"] = int(time_from)
                if time_till:
                    params["time_till"] = int(time_till)
                if group:
                    groups = await client.call("hostgroup.get", {
                        "output": ["groupid"], "filter": {"name": [group]},
                    })
                    if not groups:
                        return f"Host group '{group}' not found."
                    params["groupids"] = [g["groupid"] for g in groups]

                data = await client.call("event.get", params)

                if not data:
                    return "No events found."

                src_str = str(source)
                lines = []
                for e in data:
                    val_map = EVENT_VALUES.get(src_str, {})
                    value = val_map.get(e.get("value", "0"), e.get("value", "?"))
                    sev = format_severity(e.get("severity", "0"))
                    clock = _ts(e.get("clock", "0"))
                    ack = " [ACK]" if e.get("acknowledged") == "1" else ""
                    name = e.get("name", "")
                    lines.append(
                        f"- **[{sev}]** {name} [{value}]{ack} — {clock} "
                        f"(eventid: {e.get('eventid', '?')})"
                    )

                header = f"**Found: {len(data)} events**"
                if len(data) >= max_results:
                    header += f" (showing first {max_results}, more may exist)"
                return f"{header}\n\n" + "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error querying Zabbix: {e}"

    if "get_trends" not in skip:

        @mcp.tool()
        async def get_trends(
            item_id: str,
            time_from: str = "",
            time_till: str = "",
            limit: int = 50,
            instance: str = "",
        ) -> str:
            """Get trend (aggregated hourly) data for an item. Use for long-term analysis.

            Args:
                item_id: Zabbix item ID
                time_from: Start time as Unix timestamp (optional)
                time_till: End time as Unix timestamp (optional)
                limit: Maximum records (default: 50)
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)

                # Get item info first for name and value_type
                items = await client.call("item.get", {
                    "itemids": [item_id],
                    "output": ["itemid", "name", "key_", "units", "value_type"],
                })
                if not items:
                    return f"Item '{item_id}' not found."

                item = items[0]
                units = item.get("units", "")

                params = {
                    "itemids": [item_id],
                    "sortfield": "clock",
                    "sortorder": "DESC",
                    "limit": limit,
                    "output": "extend",
                }
                if time_from:
                    params["time_from"] = int(time_from)
                if time_till:
                    params["time_till"] = int(time_till)

                data = await client.call("trend.get", params)

                if not data:
                    return f"No trend data for item '{item.get('name', item_id)}'."

                from zbbx_mcp.tools.items import _format_value

                parts = [
                    f"# Trends: {item.get('name', '?')}",
                    f"**Key:** `{item.get('key_', '?')}`",
                    f"**Item ID:** {item_id}",
                    f"**Records:** {len(data)} (hourly aggregates)",
                    "",
                    "| Time | Min | Avg | Max | Count |",
                    "|------|-----|-----|-----|-------|",
                ]

                for r in data:
                    ts = _ts(r.get("clock", "0"))
                    vmin = _format_value(r.get("value_min", ""), units)
                    vavg = _format_value(r.get("value_avg", ""), units)
                    vmax = _format_value(r.get("value_max", ""), units)
                    num = r.get("num", "?")
                    parts.append(f"| {ts} | {vmin} | {vavg} | {vmax} | {num} |")

                return "\n".join(parts)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error querying Zabbix: {e}"

    
    if "get_event_frequency" not in skip:

        @mcp.tool()
        async def get_event_frequency(
            hours: int = 24,
            min_events: int = 3,
            group: str = "",
            max_results: int = 30,
            instance: str = "",
        ) -> str:
            """Detect flapping hosts/triggers with frequent events.

            Args:
                hours: Lookback period in hours (default: 24)
                min_events: Min events to flag as flapping (default: 3)
                group: Host group name filter (optional)
                max_results: Max results (default: 30)
                instance: Zabbix instance (optional)
            """
            try:
                client = resolver.resolve(instance)
                time_from = int(time.time()) - hours * 3600

                params = {
                    "output": ["eventid", "clock", "objectid", "name", "severity"],
                    "selectHosts": ["hostid", "host"],
                    "source": 0,
                    "value": 1,  # PROBLEM only
                    "time_from": time_from,
                    "sortfield": "clock",
                    "sortorder": "DESC",
                    "limit": 5000,
                }
                if group:
                    groups = await client.call("hostgroup.get", {
                        "output": ["groupid"], "filter": {"name": [group]},
                    })
                    if not groups:
                        return f"Host group '{group}' not found."
                    params["groupids"] = [g["groupid"] for g in groups]

                events = await client.call("event.get", params)

                if not events:
                    return f"No problem events in the last {hours}h."

                # Count events per host+trigger
                freq: dict[str, dict] = defaultdict(lambda: {"count": 0, "host": "", "trigger": "", "severity": "0", "times": []})
                for e in events:
                    hosts = e.get("hosts", [])
                    hostname = hosts[0]["host"] if hosts else "?"
                    key = f"{hostname}|{e.get('objectid', '')}"
                    entry = freq[key]
                    entry["count"] += 1
                    entry["host"] = hostname
                    entry["trigger"] = e.get("name", "?")
                    entry["severity"] = e.get("severity", "0")
                    entry["times"].append(int(e.get("clock", "0")))

                # Filter by min_events and sort by count desc
                flapping = sorted(
                    [v for v in freq.values() if v["count"] >= min_events],
                    key=lambda x: -x["count"],
                )[:max_results]

                if not flapping:
                    return f"No flapping detected (min {min_events} events in {hours}h). {len(events)} total events checked."

                lines = [f"**{len(flapping)} flapping triggers** (last {hours}h, min {min_events} events)\n"]
                lines.append("| Host | Trigger | Severity | Events | Avg interval |")
                lines.append("|------|---------|----------|--------|-------------|")

                for f in flapping:
                    sev = format_severity(f["severity"])
                    times = sorted(f["times"])
                    if len(times) > 1:
                        intervals = [times[i+1] - times[i] for i in range(len(times)-1)]
                        avg_min = sum(intervals) / len(intervals) / 60
                        interval_str = f"{avg_min:.0f}m" if avg_min < 60 else f"{avg_min/60:.1f}h"
                    else:
                        interval_str = "–"
                    lines.append(f"| {f['host']} | {f['trigger'][:60]} | {sev} | {f['count']} | {interval_str} |")

                total_unique = len(freq)
                if total_unique > len(flapping):
                    lines.append(f"\n*{total_unique - len(flapping)} more triggers with < {min_events} events omitted*")
                return "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

    
    if "get_correlated_events" not in skip:

        @mcp.tool()
        async def get_correlated_events(
            hours: int = 24,
            min_hosts: int = 2,
            window_minutes: int = 10,
            group: str = "",
            max_results: int = 20,
            instance: str = "",
        ) -> str:
            """Find correlated problems across multiple hosts in a time window.

            Args:
                hours: Lookback period in hours (default: 24)
                min_hosts: Min hosts with same problem (default: 2)
                window_minutes: Correlation window in minutes (default: 10)
                group: Host group name filter (optional)
                max_results: Max correlated groups (default: 20)
                instance: Zabbix instance (optional)
            """
            try:
                client = resolver.resolve(instance)
                time_from = int(time.time()) - hours * 3600

                params = {
                    "output": ["eventid", "clock", "objectid", "name", "severity"],
                    "selectHosts": ["hostid", "host"],
                    "source": 0,
                    "value": 1,
                    "time_from": time_from,
                    "sortfield": "clock",
                    "sortorder": "DESC",
                    "limit": 5000,
                }
                if group:
                    groups = await client.call("hostgroup.get", {
                        "output": ["groupid"], "filter": {"name": [group]},
                    })
                    if not groups:
                        return f"Host group '{group}' not found."
                    params["groupids"] = [g["groupid"] for g in groups]

                events = await client.call("event.get", params)

                if not events:
                    return f"No problem events in the last {hours}h."

                # Group events by trigger name (normalized)
                by_trigger: dict[str, list[dict]] = defaultdict(list)
                for e in events:
                    name = e.get("name", "").strip()
                    if not name:
                        continue
                    hosts = e.get("hosts", [])
                    hostname = hosts[0]["host"] if hosts else "?"
                    by_trigger[name].append({
                        "host": hostname,
                        "clock": int(e.get("clock", "0")),
                        "severity": e.get("severity", "0"),
                    })

                window_sec = window_minutes * 60
                correlated = []

                for trigger_name, trigger_events in by_trigger.items():
                    unique_hosts = {e["host"] for e in trigger_events}
                    if len(unique_hosts) < min_hosts:
                        continue

                    # Find time clusters within window
                    sorted_evts = sorted(trigger_events, key=lambda x: x["clock"])
                    clusters = []
                    current_cluster = [sorted_evts[0]]

                    for evt in sorted_evts[1:]:
                        if evt["clock"] - current_cluster[0]["clock"] <= window_sec:
                            current_cluster.append(evt)
                        else:
                            if len({e["host"] for e in current_cluster}) >= min_hosts:
                                clusters.append(current_cluster)
                            current_cluster = [evt]
                    if len({e["host"] for e in current_cluster}) >= min_hosts:
                        clusters.append(current_cluster)

                    for cluster in clusters:
                        hosts_in_cluster = {e["host"] for e in cluster}
                        correlated.append({
                            "trigger": trigger_name,
                            "hosts": sorted(hosts_in_cluster),
                            "host_count": len(hosts_in_cluster),
                            "event_count": len(cluster),
                            "severity": max(e["severity"] for e in cluster),
                            "time": min(e["clock"] for e in cluster),
                        })

                correlated.sort(key=lambda x: (-x["host_count"], -x["time"]))
                correlated = correlated[:max_results]

                if not correlated:
                    return f"No correlated events found ({len(events)} events across {len(by_trigger)} triggers, window {window_minutes}m, min {min_hosts} hosts)."

                lines = [f"**{len(correlated)} correlated incidents** (last {hours}h, {window_minutes}m window)\n"]
                for c in correlated:
                    sev = format_severity(c["severity"])
                    ts = _ts(str(c["time"]))
                    host_list = ", ".join(c["hosts"][:8])
                    if c["host_count"] > 8:
                        host_list += f" +{c['host_count'] - 8} more"
                    lines.append(f"**[{sev}]** {c['trigger'][:80]}")
                    lines.append(f"  {c['host_count']} hosts at {ts}: {host_list}")
                    lines.append("")

                return "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

    if "get_error_rate" not in skip:

        @mcp.tool()
        async def get_error_rate(
            hours: int = 24,
            severity_min: int = 3,
            max_results: int = 15,
            instance: str = "",
        ) -> str:
            """Top servers by error count with trend (getting worse or better).

            Args:
                hours: Lookback period (default: 24)
                severity_min: Minimum severity 0-5 (default: 3=Average)
                max_results: Max servers (default: 15)
                instance: Zabbix instance name (optional)
            """
            try:
                client = resolver.resolve(instance)
                now = int(time.time())
                half = hours * 3600 // 2

                events = await client.call("event.get", {
                    "output": ["eventid", "clock", "objectid", "name", "severity"],
                    "selectHosts": ["host"],
                    "source": 0, "value": 1,
                    "time_from": now - hours * 3600,
                    "sortfield": "clock", "sortorder": "DESC",
                    "severities": list(range(severity_min, 6)),
                    "limit": 5000,
                })

                if not events:
                    return f"No errors (severity >= {severity_min}) in {hours}h."

                # Resolve unknown hosts via trigger ID → host lookup
                unresolved_trigger_ids = set()
                for e in events:
                    if not e.get("hosts"):
                        unresolved_trigger_ids.add(e.get("objectid", ""))
                unresolved_trigger_ids.discard("")

                trigger_host_map: dict[str, str] = {}
                if unresolved_trigger_ids:
                    triggers = await client.call("trigger.get", {
                        "triggerids": list(unresolved_trigger_ids),
                        "output": ["triggerid"],
                        "selectHosts": ["host"],
                    })
                    for t in triggers:
                        thosts = t.get("hosts", [])
                        if thosts:
                            trigger_host_map[t["triggerid"]] = thosts[0]["host"]

                cutoff = now - half
                host_errors: dict[str, dict] = {}
                for e in events:
                    hosts = e.get("hosts", [])
                    if hosts:
                        hostname = hosts[0]["host"]
                    elif e.get("objectid") in trigger_host_map:
                        hostname = trigger_host_map[e["objectid"]]
                    else:
                        name = e.get("name", "")
                        if " on " in name:
                            hostname = name.split(" on ")[-1].strip()[:40]
                        elif ": " in name:
                            hostname = name.split(": ")[0].strip()[:40]
                        else:
                            hostname = f"trigger:{e.get('objectid', '?')}"
                    entry = host_errors.setdefault(hostname, {"total": 0, "recent": 0, "older": 0, "triggers": set()})
                    entry["total"] += 1
                    if int(e.get("clock", 0)) >= cutoff:
                        entry["recent"] += 1
                    else:
                        entry["older"] += 1
                    entry["triggers"].add(e.get("name", "?")[:50])

                ranked = sorted(host_errors.items(), key=lambda x: -x[1]["total"])[:max_results]

                base_url = client.frontend_url
                lines = [f"**Error rate ({hours}h, sev>={severity_min}): {len(host_errors)} servers, {len(events)} events**\n"]
                for hostname, d in ranked:
                    trend = ""
                    if d["older"] > 0:
                        ratio = d["recent"] / d["older"]
                        trend = " WORSE" if ratio > 1.5 else (" better" if ratio < 0.5 else "")
                    elif d["recent"] > 0:
                        trend = " NEW"
                    triggers = ", ".join(list(d["triggers"])[:2])
                    link = f" [→]({base_url}/zabbix.php?action=search&search={hostname})" if not hostname.startswith("trigger:") else ""
                    lines.append(f"- **{hostname}** {d['total']} err ({d['recent']}/{d['older']}{trend}){link} — {triggers}")

                return "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"
