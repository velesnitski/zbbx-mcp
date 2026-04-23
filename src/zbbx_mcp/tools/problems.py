import asyncio

import httpx

from zbbx_mcp.formatters import _ts, format_problem_list, format_severity
from zbbx_mcp.resolver import InstanceResolver
from zbbx_mcp.utils import parse_time, resolve_group_ids


_EVENT_VALUE_LABEL = {"0": "OK", "1": "PROBLEM"}


def _format_event_list(events: list) -> str:
    """Format event.get results with OK/PROBLEM state labels."""
    if not events:
        return "No events found."
    lines = []
    for e in events:
        severity = format_severity(e.get("severity", "0"))
        state = _EVENT_VALUE_LABEL.get(e.get("value", "1"), "?")
        ack = " [ACK]" if e.get("acknowledged") == "1" else ""
        clock = _ts(e.get("clock", "0"))
        lines.append(
            f"- **[{severity}]** {e.get('name', 'Unknown')} [{state}]{ack} — {clock} "
            f"(eventid: {e.get('eventid', '?')})"
        )
    return "\n".join(lines)


async def _problem_timeline(client, eventid: str) -> str:
    """Return the PROBLEM→OK transition for a single event."""
    events = await client.call("event.get", {
        "eventids": [eventid],
        "output": ["eventid", "name", "severity", "clock", "value",
                   "acknowledged", "r_eventid"],
    })
    if not events:
        return f"Event '{eventid}' not found."
    e = events[0]
    recovery_id = e.get("r_eventid", "0")

    recovery = None
    if recovery_id and recovery_id != "0":
        rec = await client.call("event.get", {
            "eventids": [recovery_id],
            "output": ["eventid", "clock", "value", "acknowledged"],
        })
        if rec:
            recovery = rec[0]

    parts = [
        f"# Timeline: {e.get('name', 'Unknown')}",
        "",
        f"**Event ID:** {e.get('eventid', '?')}",
        f"**Severity:** {format_severity(e.get('severity', '0'))}",
        "",
        "| Time | State | Event ID |",
        "|------|-------|----------|",
        f"| {_ts(e.get('clock', '0'))} | PROBLEM | {e.get('eventid', '?')} |",
    ]
    if recovery:
        parts.append(
            f"| {_ts(recovery.get('clock', '0'))} | OK | {recovery.get('eventid', '?')} |"
        )
    else:
        parts.append("| *(ongoing)* | — | — |")
    return "\n".join(parts)


async def _resolved_events(
    client, resolver, host, group, severity_min,
    acknowledged, max_results, tf, tt,
) -> str:
    """Return problem+recovery events from event.get (includes OK transitions)."""
    params = {
        "output": ["eventid", "name", "severity", "clock", "value", "acknowledged"],
        "source": 0, "object": 0,
        "sortfield": ["clock"],
        "sortorder": ["DESC"],
        "limit": max_results,
    }
    if severity_min > 0:
        params["severities"] = list(range(severity_min, 6))
    if tf is not None:
        params["time_from"] = tf
    if tt is not None:
        params["time_till"] = tt
    if acknowledged == "yes":
        params["acknowledged"] = True
    elif acknowledged == "no":
        params["acknowledged"] = False

    if host and group:
        host_result, group_result = await asyncio.gather(
            client.call("host.get", {"output": ["hostid"], "filter": {"host": [host]}}),
            client.call("hostgroup.get", {"output": ["groupid"], "filter": {"name": [group]}}),
        )
        if not host_result:
            return f"Host '{host}' not found."
        if not group_result:
            return f"Host group '{group}' not found."
        params["hostids"] = [h["hostid"] for h in host_result]
        params["groupids"] = [g["groupid"] for g in group_result]
    elif host:
        hosts = await client.call("host.get", {
            "output": ["hostid"], "filter": {"host": [host]},
        })
        if not hosts:
            return f"Host '{host}' not found."
        params["hostids"] = [h["hostid"] for h in hosts]
    elif group:
        gids = await resolve_group_ids(client, group)
        if gids is None:
            return f"Host group '{group}' not found."
        params["groupids"] = gids

    data = await client.call("event.get", params)
    result = _format_event_list(data)
    count = len(data)
    if count == 0:
        return result
    header = f"**Found: {count} events**"
    if count >= max_results:
        header += f" (showing first {max_results}, more may exist)"
    return f"{header}\n\n{result}"


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()) -> None:

    if "get_problems" not in skip:

        @mcp.tool()
        async def get_problems(
            severity_min: int = 0,
            host: str = "",
            group: str = "",
            recent: bool = True,
            acknowledged: str = "",
            max_results: int = 50,
            time_from: str = "",
            time_till: str = "",
            include_resolved: bool = False,
            event_eventid: str = "",
            instance: str = "",
        ) -> str:
            """Get current Zabbix problems (active triggers).

            Args:
                severity_min: Minimum severity (0=Not classified, 1=Info, 2=Warning, 3=Average, 4=High, 5=Disaster)
                host: Filter by host name (exact match)
                group: Filter by host group name
                recent: Only recent problems (default: True; ignored when include_resolved)
                acknowledged: Filter by acknowledgement: 'yes', 'no', or '' for all (default: all)
                max_results: Maximum number of results (default: 50)
                time_from: Start of window — epoch, ISO, or relative ("24h", "7d") (optional)
                time_till: End of window — same formats as time_from (optional)
                include_resolved: Include OK transitions in the window (default: False)
                event_eventid: Return full PROBLEM↔OK timeline for a specific event ID
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)

                tf = None
                tt = None
                if time_from:
                    try:
                        tf = parse_time(time_from)
                    except ValueError as e:
                        return f"Invalid time_from: {e}"
                if time_till:
                    try:
                        tt = parse_time(time_till)
                    except ValueError as e:
                        return f"Invalid time_till: {e}"

                if event_eventid:
                    return await _problem_timeline(client, event_eventid)

                if include_resolved:
                    return await _resolved_events(
                        client, resolver, host, group, severity_min,
                        acknowledged, max_results, tf, tt,
                    )

                params = {
                    "output": ["eventid", "name", "severity", "clock", "acknowledged", "suppressed"],
                    "sortfield": ["eventid"],
                    "sortorder": ["DESC"],
                    "limit": max_results,
                    "recent": recent,
                    "severities": list(range(severity_min, 6)) if severity_min > 0 else None,
                }
                # Remove None values
                params = {k: v for k, v in params.items() if v is not None}
                if tf is not None:
                    params["time_from"] = tf
                if tt is not None:
                    params["time_till"] = tt

                if acknowledged == "yes":
                    params["acknowledged"] = True
                elif acknowledged == "no":
                    params["acknowledged"] = False

                # Resolve host and group filters in parallel when both are specified
                if host and group:
                    host_result, group_result = await asyncio.gather(
                        client.call("host.get", {"output": ["hostid"], "filter": {"host": [host]}}),
                        client.call("hostgroup.get", {"output": ["groupid"], "filter": {"name": [group]}}),
                    )
                    if not host_result:
                        return f"Host '{host}' not found."
                    if not group_result:
                        return f"Host group '{group}' not found."
                    params["hostids"] = [h["hostid"] for h in host_result]
                    params["groupids"] = [g["groupid"] for g in group_result]
                elif host:
                    hosts = await client.call("host.get", {
                        "output": ["hostid"],
                        "filter": {"host": [host]},
                    })
                    if not hosts:
                        hosts = await client.call("host.get", {
                            "output": ["hostid"],
                            "search": {"host": host, "name": host},
                            "searchByAny": True, "searchWildcardsEnabled": True,
                        })
                    if not hosts:
                        return f"Host '{host}' not found."
                    params["hostids"] = [h["hostid"] for h in hosts]
                elif group:
                    gids = await resolve_group_ids(client, group)
                    if gids is None:
                        return f"Host group '{group}' not found."
                    params["groupids"] = gids

                data = await client.call("problem.get", params)

                result = format_problem_list(data)
                count = len(data)
                if count == 0:
                    return result
                header = f"**Found: {count} problems**"
                if count >= max_results:
                    header += f" (showing first {max_results}, more may exist)"
                return f"{header}\n\n{result}"
            except (httpx.HTTPError, ValueError) as e:
                return f"Error querying Zabbix: {e}"

    if "get_problem_detail" not in skip:

        @mcp.tool()
        async def get_problem_detail(problem_id: str = "", event_id: str = "", instance: str = "") -> str:
            """Get full details of a specific problem/event.

            Args:
                problem_id: Problem/event ID (preferred)
                event_id: Alias for problem_id (backward compatible)
                instance: Zabbix instance name (optional)
            """
            try:
                eid = problem_id or event_id
                if not eid:
                    return "Either problem_id or event_id is required."
                client = resolver.resolve(instance)
                data = await client.call("problem.get", {
                    "eventids": [eid],
                    "output": "extend",
                    "selectAcknowledges": ["userid", "alias", "message", "clock", "action"],
                    "selectTags": ["tag", "value"],
                    "selectSuppressionData": ["maintenanceid"],
                })

                if not data:
                    return f"Problem with eventid '{event_id}' not found."

                p = data[0]
                severity = format_severity(p.get("severity", "0"))
                parts = [
                    f"# Problem: {p.get('name', 'Unknown')}",
                    "",
                    f"**Event ID:** {p.get('eventid', '?')}",
                    f"**Severity:** {severity}",
                    f"**Started:** {_ts(p.get('clock', '0'))}",
                    f"**Acknowledged:** {'Yes' if p.get('acknowledged') == '1' else 'No'}",
                    f"**Suppressed:** {'Yes' if p.get('suppressed') == '1' else 'No'}",
                ]

                tags = p.get("tags", [])
                if tags:
                    parts.append("")
                    parts.append("## Tags")
                    for t in tags:
                        parts.append(f"- {t.get('tag', '?')}: {t.get('value', '')}")

                acks = p.get("acknowledges", [])
                if acks:
                    parts.append("")
                    parts.append(f"## Acknowledgements ({len(acks)})")
                    for a in acks:
                        parts.append(
                            f"- **{a.get('alias', '?')}** ({_ts(a.get('clock', '0'))}): "
                            f"{a.get('message', '')}"
                        )

                return "\n".join(parts)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error querying Zabbix: {e}"

    if "acknowledge_problem" not in skip:

        @mcp.tool()
        async def acknowledge_problem(
            event_id: str,
            message: str = "",
            close: bool = False,
            instance: str = "",
        ) -> str:
            """Acknowledge a Zabbix problem and optionally close it.

            Args:
                event_id: Zabbix event ID to acknowledge
                message: Optional acknowledgement message
                close: Also close the problem (default: False)
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)

                # action bitmask: 2=acknowledge, 1=close, 4=add message
                action = 2  # acknowledge
                if close:
                    action |= 1
                if message:
                    action |= 4

                await client.call("event.acknowledge", {
                    "eventids": [event_id],
                    "action": action,
                    "message": message,
                })

                parts = [f"Problem {event_id} acknowledged."]
                if close:
                    parts.append("Problem marked for closing.")
                if message:
                    parts.append(f"Message: {message}")
                return " ".join(parts)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error querying Zabbix: {e}"
