import time as _time

import httpx

from zbbx_mcp.data import fetch_traffic_map
from zbbx_mcp.resolver import InstanceResolver


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()) -> None:

    if "check_connection" not in skip:

        @mcp.tool()
        async def check_connection(instance: str = "") -> str:
            """Check connectivity to a Zabbix server and return its version.

            Args:
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)
                version = await client.call("apiinfo.version", {})
                return f"Connected. Zabbix version: {version}"
            except (httpx.HTTPError, ValueError) as e:
                return f"Connection failed: {e}"

    if "get_agent_unreachable" not in skip:

        @mcp.tool()
        async def get_agent_unreachable(
            max_results: int = 30,
            instance: str = "",
        ) -> str:
            """Find hosts where Zabbix agent is unreachable (agent.ping failed).

            Args:
                max_results: Maximum results (default: 30)
                instance: Zabbix instance name (optional)
            """
            try:
                client = resolver.resolve(instance)
                # Get all enabled hosts with agent availability
                hosts = await client.call("host.get", {
                    "output": ["hostid", "host", "name"],
                    "selectInterfaces": ["ip"],
                    "filter": {"status": "0"},
                    "sortfield": "host",
                })

                # Check agent.ping items
                items = await client.call("item.get", {
                    "hostids": [h["hostid"] for h in hosts],
                    "output": ["hostid", "lastvalue", "lastclock"],
                    "filter": {"key_": "agent.ping", "status": "0"},
                })

                ping_map = {it["hostid"]: it for it in items}
                # Fetch traffic to filter false positives — server with traffic is alive
                traffic_map = await fetch_traffic_map(client, [h["hostid"] for h in hosts])
                now = int(_time.time())
                unreachable = []

                for h in hosts:
                    hid = h["hostid"]
                    ping = ping_map.get(hid)
                    if not ping:
                        continue  # no agent.ping item
                    try:
                        val = int(float(ping.get("lastvalue", "0")))
                        last = int(ping.get("lastclock", "0"))
                    except (ValueError, TypeError):
                        continue
                    # Skip hosts with real traffic — agent.ping may be deprecated there
                    if traffic_map.get(hid, 0) >= 5:
                        continue
                    stale_hours = round((now - last) / 3600, 1) if last > 0 else 0
                    if val != 1 or stale_hours > 1:
                        ip = next((i["ip"] for i in h.get("interfaces", []) if i.get("ip") != "127.0.0.1"), "")
                        unreachable.append((h["host"], ip, val, stale_hours))

                if not unreachable:
                    return f"All agents reachable ({len(ping_map)} checked)."

                shown = unreachable[:max_results]
                lines = [f"**{len(unreachable)} unreachable agents** ({len(ping_map)} total)\n"]
                lines.append("| Host | IP | Ping | Last Seen |")
                lines.append("|------|----|------|----------|")
                for host, ip, val, hours in shown:
                    status = "DOWN" if val != 1 else "STALE"
                    lines.append(f"| {host} | {ip} | {status} | {hours}h ago |")
                if len(unreachable) > max_results:
                    lines.append(f"\n*{len(unreachable) - max_results} more omitted*")
                return "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

    if "get_active_problems" not in skip:

        @mcp.tool()
        async def get_active_problems(
            min_severity: int = 2,
            max_results: int = 30,
            instance: str = "",
        ) -> str:
            """Active problems summary — grouped by severity with counts.

            Args:
                min_severity: Minimum severity: 0=info, 2=warning, 3=average, 4=high, 5=disaster
                max_results: Maximum individual problems to show (default: 30)
                instance: Zabbix instance name (optional)
            """
            try:
                client = resolver.resolve(instance)
                problems = await client.call("problem.get", {
                    "output": ["eventid", "name", "severity", "clock", "objectid"],
                    "severities": list(range(min_severity, 6)),
                    "sortfield": "eventid",
                    "sortorder": "DESC",
                    "limit": 500,
                    "recent": True,
                })

                # problem.get doesn't reliably return hosts in Zabbix 6.4 — use event.get
                if problems:
                    event_ids = [p["eventid"] for p in problems]
                    events = await client.call("event.get", {
                        "output": ["eventid"],
                        "selectHosts": ["host"],
                        "eventids": event_ids,
                    })
                    event_hosts = {e["eventid"]: e.get("hosts", []) for e in events}
                    for p in problems:
                        p["hosts"] = event_hosts.get(p["eventid"], [])

                # Sort by severity desc (Zabbix 6.4 doesn't support severity sort)
                problems.sort(key=lambda p: (-int(p.get("severity", "0")), -int(p.get("clock", "0"))))

                if not problems:
                    return f"No active problems (severity >= {min_severity})."

                _SEV = {0: "Info", 1: "Info", 2: "Warning", 3: "Average", 4: "High", 5: "Disaster"}
                sev_counts: dict[str, int] = {}
                for p in problems:
                    s = _SEV.get(int(p.get("severity", 0)), "?")
                    sev_counts[s] = sev_counts.get(s, 0) + 1

                lines = [f"**{len(problems)} active problems**\n"]
                # Summary by severity
                for sev in ["Disaster", "High", "Average", "Warning", "Info"]:
                    if sev in sev_counts:
                        lines.append(f"- **{sev}:** {sev_counts[sev]}")
                lines.append("")

                # Detect alert storms: same problem name on many hosts within same minute
                from collections import Counter
                name_counts = Counter(p.get("name", "?") for p in problems)
                recent_clocks = [int(p.get("clock", "0")) for p in problems]
                if recent_clocks:
                    clock_spread = max(recent_clocks) - min(recent_clocks)
                    for name, cnt in name_counts.most_common(1):
                        if cnt >= 10 and clock_spread < 300:  # 10+ alerts in <5min
                            lines.append(f"**Alert storm detected:** {cnt} simultaneous '{name}' alerts (all within {clock_spread}s).")
                            lines.append("Likely monitoring-side issue (check script, DNS, proxy) rather than real service failure.")
                            lines.append("Verify by checking traffic on affected hosts — if traffic is normal, alert is noise.\n")

                # Group correlated problems: same (name, severity) across 5+ hosts = 1 entry
                grouped: dict[tuple, list] = {}
                for p in problems:
                    sev_int = int(p.get("severity", 0))
                    name = p.get("name", "?")
                    host = p["hosts"][0]["host"] if p.get("hosts") else "?"
                    key = (sev_int, name)
                    grouped.setdefault(key, []).append(host)

                # Flatten: singles + groups
                display = []
                for (sev_int, name), hosts_list in grouped.items():
                    sev = _SEV.get(sev_int, "?")
                    if len(hosts_list) >= 5:
                        # Cluster: show count + sample hosts
                        sample = ", ".join(hosts_list[:3])
                        display.append((sev_int, sev, f"{len(hosts_list)} hosts", f"{name[:80]} — {sample}{'...' if len(hosts_list) > 3 else ''}", len(hosts_list)))
                    else:
                        for h in hosts_list:
                            display.append((sev_int, sev, h, name[:80], 1))

                display.sort(key=lambda x: (-x[0], -x[4]))
                raw_count = len(problems)
                grouped_count = sum(1 for d in display if d[4] >= 5)
                collapsed = raw_count - len(display)

                lines.append("| Severity | Host(s) | Problem |")
                lines.append("|----------|---------|---------|")
                for _sev_int, sev, host, name, _cnt in display[:max_results]:
                    lines.append(f"| {sev} | {host} | {name} |")

                if grouped_count:
                    lines.append(f"\n*{grouped_count} cluster incidents collapsed from {collapsed} individual problems*")
                if len(display) > max_results:
                    lines.append(f"*{len(display) - max_results} more omitted*")
                return "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

    if "get_stale_servers" not in skip:

        @mcp.tool()
        async def get_stale_servers(
            hours: int = 24,
            max_results: int = 30,
            instance: str = "",
        ) -> str:
            """Find servers where agent data is stale (last update > N hours ago).

            Args:
                hours: Flag servers with data older than this (default: 24)
                max_results: Maximum results (default: 30)
                instance: Zabbix instance name (optional)
            """
            try:
                client = resolver.resolve(instance)
                hosts = await client.call("host.get", {
                    "output": ["hostid", "host"],
                    "selectInterfaces": ["ip"],
                    "filter": {"status": "0"},
                })

                # Get agent.ping lastclock for all hosts
                items = await client.call("item.get", {
                    "hostids": [h["hostid"] for h in hosts],
                    "output": ["hostid", "lastclock"],
                    "filter": {"key_": "agent.ping", "status": "0"},
                })

                ping_map = {it["hostid"]: int(it.get("lastclock", "0")) for it in items}
                now = int(_time.time())
                cutoff = now - hours * 3600
                stale = []

                for h in hosts:
                    hid = h["hostid"]
                    last = ping_map.get(hid, 0)
                    if last == 0:
                        continue  # no agent.ping item — skip
                    if last < cutoff:
                        stale_h = round((now - last) / 3600, 1)
                        ip = next((i["ip"] for i in h.get("interfaces", []) if i.get("ip") != "127.0.0.1"), "")
                        stale.append((h["host"], ip, stale_h))

                if not stale:
                    return f"All agents reported within {hours}h ({len(ping_map)} checked)."

                stale.sort(key=lambda x: -x[2])
                shown = stale[:max_results]

                lines = [f"**{len(stale)} stale servers** (data > {hours}h old)\n"]
                lines.append("| Host | IP | Last Data |")
                lines.append("|------|----|----------|")
                for host, ip, h in shown:
                    days = h / 24
                    age = f"{days:.0f}d" if days >= 2 else f"{h:.0f}h"
                    lines.append(f"| {host} | {ip} | {age} ago |")
                if len(stale) > max_results:
                    lines.append(f"\n*{len(stale) - max_results} more omitted*")
                return "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"
