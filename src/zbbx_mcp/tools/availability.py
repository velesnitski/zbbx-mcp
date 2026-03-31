"""Host availability and recent changes tools."""

from __future__ import annotations

import httpx

from zbbx_mcp.data import extract_country, fetch_enabled_hosts, host_ip
from zbbx_mcp.classify import classify_host, detect_provider
from zbbx_mcp.resolver import InstanceResolver


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()) -> None:

    if "get_host_availability" not in skip:

        @mcp.tool()
        async def get_host_availability(
            status: str = "unavailable",
            limit: int = 50,
            instance: str = "",
        ) -> str:
            """Show host agent/SNMP availability status.

            Args:
                status: Filter: 'unavailable', 'available', or 'all' (default: unavailable)
                limit: Max results (default: 50)
                instance: Zabbix instance name (optional)
            """
            try:
                client = resolver.resolve(instance)
                hosts = await client.call("host.get", {
                    "output": ["hostid", "host", "name", "status"],
                    "selectGroups": ["name"],
                    "selectInterfaces": ["ip"],
                    "filter": {"status": "0"},
                    "sortfield": "host",
                })

                # Fetch availability via host.get with selectAvailability (Zabbix 6.2+)
                try:
                    avail_hosts = await client.call("host.get", {
                        "output": ["hostid"],
                        "filter": {"status": "0"},
                        "selectInterfaces": ["available", "type"],
                    })
                    avail_map: dict[str, dict] = {}
                    for h in avail_hosts:
                        agent_avail = 0
                        snmp_avail = 0
                        for iface in h.get("interfaces", []):
                            a = int(iface.get("available", 0))
                            if iface.get("type") == "1":  # agent
                                agent_avail = max(agent_avail, a)
                            elif iface.get("type") == "2":  # SNMP
                                snmp_avail = max(snmp_avail, a)
                        avail_map[h["hostid"]] = {"agent": agent_avail, "snmp": snmp_avail}
                except (ValueError, KeyError):
                    avail_map = {}

                host_map = {h["hostid"]: h for h in hosts}

                rows = []
                for hid, h in host_map.items():
                    avail = avail_map.get(hid, {})
                    agent = avail.get("agent", 0)
                    snmp = avail.get("snmp", 0)

                    # 0=unknown, 1=available, 2=unavailable
                    agent_str = {0: "?", 1: "OK", 2: "DOWN"}.get(agent, "?")
                    snmp_str = {0: "-", 1: "OK", 2: "DOWN"}.get(snmp, "-")

                    if status == "unavailable" and agent != 2 and snmp != 2:
                        continue
                    elif status == "available" and agent != 1:
                        continue

                    hostname = h.get("host", "")
                    cc = extract_country(hostname)
                    prod, _ = classify_host(h.get("groups", []))
                    ip = host_ip(h)
                    rows.append((hostname, cc, prod, ip, agent_str, snmp_str))

                if not rows:
                    return f"No {status} hosts found."

                rows = rows[:limit]
                lines = [f"**Host Availability** ({len(rows)} hosts, filter: {status})\n"]
                lines.append("| Host | Country | Product | IP | Agent | SNMP |")
                lines.append("|------|---------|---------|-----|-------|------|")
                for hostname, cc, prod, ip, agent_str, snmp_str in rows:
                    lines.append(f"| {hostname} | {cc} | {prod} | {ip} | {agent_str} | {snmp_str} |")

                return "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

    if "get_recent_changes" not in skip:

        @mcp.tool()
        async def get_recent_changes(
            hours: int = 24,
            limit: int = 50,
            instance: str = "",
        ) -> str:
            """Show what happened recently: new problems, resolved problems, host status changes.

            Args:
                hours: Look back period in hours (default: 24)
                limit: Max results per category (default: 50)
                instance: Zabbix instance name (optional)
            """
            try:
                import time
                client = resolver.resolve(instance)
                time_from = str(int(time.time()) - hours * 3600)

                # Fetch in parallel: current problems, recent events, recently resolved
                import asyncio
                current_problems, recent_events = await asyncio.gather(
                    client.call("problem.get", {
                        "output": ["eventid", "name", "severity", "clock", "acknowledged"],
                        "selectHosts": ["host"],
                        "time_from": time_from,
                        "sortfield": "eventid",
                        "sortorder": "DESC",
                        "limit": limit,
                        "recent": True,
                    }),
                    client.call("event.get", {
                        "output": ["eventid", "name", "clock", "value", "severity"],
                        "selectHosts": ["host"],
                        "time_from": time_from,
                        "sortfield": "eventid",
                        "sortorder": "DESC",
                        "limit": limit * 2,
                        "value": "0",  # resolved events
                    }),
                )

                from datetime import datetime, timezone
                def _fmt_time(ts: str) -> str:
                    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%m-%d %H:%M")

                _SEV = {
                    "0": "Info", "1": "Info", "2": "Warning",
                    "3": "Average", "4": "High", "5": "Disaster",
                }

                lines = [f"**Recent Changes** (last {hours}h)\n"]

                # New problems
                if current_problems:
                    lines.append(f"### New Problems ({len(current_problems)})\n")
                    lines.append("| Time | Severity | Host | Problem | Ack |")
                    lines.append("|------|----------|------|---------|-----|")
                    for p in current_problems[:limit]:
                        host = p.get("hosts", [{}])[0].get("host", "?") if p.get("hosts") else "?"
                        sev = _SEV.get(p.get("severity", "0"), "?")
                        ack = "Yes" if p.get("acknowledged") == "1" else "No"
                        lines.append(f"| {_fmt_time(p['clock'])} | {sev} | {host} | {p.get('name', '?')[:60]} | {ack} |")
                else:
                    lines.append("### New Problems: None\n")

                # Recently resolved
                resolved = [e for e in recent_events if e.get("value") == "0"]
                if resolved:
                    lines.append(f"\n### Resolved ({len(resolved)})\n")
                    lines.append("| Time | Host | Problem |")
                    lines.append("|------|------|---------|")
                    for e in resolved[:limit]:
                        host = e.get("hosts", [{}])[0].get("host", "?") if e.get("hosts") else "?"
                        lines.append(f"| {_fmt_time(e['clock'])} | {host} | {e.get('name', '?')[:60]} |")
                else:
                    lines.append("\n### Resolved: None\n")

                # Summary
                sev_counts: dict[str, int] = {}
                for p in current_problems:
                    s = _SEV.get(p.get("severity", "0"), "?")
                    sev_counts[s] = sev_counts.get(s, 0) + 1
                summary = ", ".join(f"{s}: {c}" for s, c in sorted(sev_counts.items(), key=lambda x: -x[1]))
                lines.append(f"\n**Summary:** {len(current_problems)} new, {len(resolved)} resolved. {summary}")

                return "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"
