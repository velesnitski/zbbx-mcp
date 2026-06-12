import asyncio
import time

import httpx

from zbbx_mcp.data import filter_suppressed
from zbbx_mcp.formatters import _ts, format_problem_list, format_severity
from zbbx_mcp.resolver import InstanceResolver
from zbbx_mcp.tag_filter import parse_tag_filter
from zbbx_mcp.utils import parse_time, resolve_group_ids

_EVENT_VALUE_LABEL = {"0": "OK", "1": "PROBLEM"}


def _build_ack_action(
    *,
    close: bool = False,
    message: str = "",
    severity: int = -1,
    unack: bool = False,
    suppress: bool = False,
    unsuppress: bool = False,
) -> int:
    """Compute the Zabbix event.acknowledge action bitmask.

    Bits per the Zabbix API (since 6.0):
      1  = close the problem
      2  = acknowledge
      4  = add message
      8  = change severity
      16 = unacknowledge (mutually exclusive with 2)
      32 = suppress (with ``suppress_until``; ADR 059)
      64 = unsuppress

    Pure helper — testable without a Zabbix server.
    """
    action = 16 if unack else 2
    if close:
        action |= 1
    if message:
        action |= 4
    if 0 <= severity <= 5:
        action |= 8
    if suppress:
        action |= 32
    if unsuppress:
        action |= 64
    return action


def _build_rank_action(*, unrank: bool = False, message: str = "") -> int:
    """Compute the event.acknowledge bitmask for cause/symptom ranking.

    Bits (Zabbix 6.4+): 256 = change rank to symptom (requires
    ``cause_eventid``), 128 = change rank back to cause (independent),
    4 = add message. Pure helper (ADR 060).
    """
    action = 128 if unrank else 256
    if message:
        action |= 4
    return action


def _suppress_until_from_hours(suppress_hours: float, now: int) -> int | None:
    """Translate the tool-level ``suppress_hours`` into ``suppress_until``.

    ``0`` → no suppression (None); ``-1`` → indefinite (Zabbix encodes this
    as ``suppress_until = 0``, i.e. until the problem resolves); positive →
    epoch ``now + hours``. Pure helper (ADR 059).
    """
    if suppress_hours == 0:
        return None
    if suppress_hours < 0:
        return 0
    return now + int(suppress_hours * 3600)


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
            tags: str = "",
            recent: bool = True,
            acknowledged: str = "",
            max_results: int = 50,
            time_from: str = "",
            time_till: str = "",
            include_resolved: bool = False,
            include_suppressed: bool = False,
            event_eventid: str = "",
            instance: str = "",
        ) -> str:
            """Get current Zabbix problems (active triggers).

            Args:
                severity_min: Minimum severity (0=Not classified, 1=Info, 2=Warning, 3=Average, 4=High, 5=Disaster)
                host: Filter by host name (exact match)
                group: Filter by host group name
                tags: Tag filter as "key:value,key2:value2" (e.g. "role:edge,env:prod").
                    Bare key like "role" means "tag exists". AND-combined.
                recent: Only recent problems (default: True; ignored when include_resolved)
                acknowledged: Filter by acknowledgement: 'yes', 'no', or '' for all (default: all)
                max_results: Maximum number of results (default: 50)
                time_from: Start of window — epoch, ISO, or relative ("24h", "7d") (optional)
                time_till: End of window — same formats as time_from (optional)
                include_resolved: Include OK transitions in the window (default: False)
                include_suppressed: Include maintenance-suppressed problems (default: False)
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

                tag_filter = parse_tag_filter(tags) if tags else []
                if tag_filter:
                    params["tags"] = tag_filter
                    params["evaltype"] = 0

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
                data = filter_suppressed(data, include_suppressed)

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
            severity: int = -1,
            unack: bool = False,
            suppress_hours: float = 0,
            unsuppress: bool = False,
            instance: str = "",
        ) -> str:
            """Acknowledge a Zabbix problem (also close, re-prioritise, snooze, or unack).

            Snoozing (``suppress_hours``) marks the problem suppressed in
            Zabbix itself, so it drops out of every suppress-aware view —
            including all this server's problem tools (ADR 052) — until the
            timer lapses or it is unsuppressed.

            Args:
                event_id: Zabbix event ID to acknowledge
                message: Optional acknowledgement message
                close: Also close the problem (default: False)
                severity: Change severity to this value (0-5); -1 = no change
                unack: Unacknowledge instead of acknowledge (mutually exclusive)
                suppress_hours: Snooze for N hours; -1 = until the problem
                    resolves; 0 = no snooze (default)
                unsuppress: Lift an existing suppression (default: False)
                instance: Zabbix instance name (optional)
            """
            if suppress_hours != 0 and unsuppress:
                return "suppress_hours and unsuppress are mutually exclusive."
            try:
                client = resolver.resolve(instance)
                suppress_until = _suppress_until_from_hours(
                    suppress_hours, int(time.time())
                )
                action = _build_ack_action(
                    close=close, message=message,
                    severity=severity, unack=unack,
                    suppress=suppress_until is not None,
                    unsuppress=unsuppress,
                )
                payload: dict = {
                    "eventids": [event_id],
                    "action": action,
                    "message": message,
                }
                if 0 <= severity <= 5:
                    payload["severity"] = severity
                if suppress_until is not None:
                    payload["suppress_until"] = suppress_until
                await client.call("event.acknowledge", payload)

                verb = "unacknowledged" if unack else "acknowledged"
                parts = [f"Problem {event_id} {verb}."]
                if close:
                    parts.append("Marked for closing.")
                if 0 <= severity <= 5:
                    parts.append(f"Severity set to {severity}.")
                if suppress_until is not None:
                    parts.append(
                        "Snoozed until the problem resolves."
                        if suppress_until == 0
                        else f"Snoozed for {suppress_hours:g}h."
                    )
                if unsuppress:
                    parts.append("Suppression lifted.")
                if message:
                    parts.append(f"Message: {message}")
                return " ".join(parts)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error querying Zabbix: {e}"

    if "bulk_acknowledge" not in skip:

        @mcp.tool()
        async def bulk_acknowledge(
            event_ids: str,
            message: str = "",
            close: bool = False,
            severity: int = -1,
            unack: bool = False,
            suppress_hours: float = 0,
            unsuppress: bool = False,
            instance: str = "",
        ) -> str:
            """Acknowledge many events in one API call (mass-incident response).

            ``suppress_hours`` snoozes all listed problems (see
            acknowledge_problem) — useful for silencing a known-chronic
            cluster of alerts in one call.

            Args:
                event_ids: Comma- or space-separated Zabbix event IDs
                message: Optional acknowledgement message applied to all
                close: Also mark each problem for closing (default: False)
                severity: Change severity (0-5) for all listed events; -1 = no change
                unack: Unacknowledge instead of acknowledge (mutually exclusive)
                suppress_hours: Snooze all for N hours; -1 = until each
                    resolves; 0 = no snooze (default)
                unsuppress: Lift existing suppression on all (default: False)
                instance: Zabbix instance name (optional)
            """
            if suppress_hours != 0 and unsuppress:
                return "suppress_hours and unsuppress are mutually exclusive."
            try:
                ids = [e.strip() for e in event_ids.replace(",", " ").split() if e.strip()]
                if not ids:
                    return "No event IDs provided."

                client = resolver.resolve(instance)
                suppress_until = _suppress_until_from_hours(
                    suppress_hours, int(time.time())
                )
                action = _build_ack_action(
                    close=close, message=message,
                    severity=severity, unack=unack,
                    suppress=suppress_until is not None,
                    unsuppress=unsuppress,
                )
                payload: dict = {
                    "eventids": ids,
                    "action": action,
                    "message": message,
                }
                if 0 <= severity <= 5:
                    payload["severity"] = severity
                if suppress_until is not None:
                    payload["suppress_until"] = suppress_until
                await client.call("event.acknowledge", payload)

                verb = "Unacknowledged" if unack else "Acknowledged"
                parts = [f"{verb} {len(ids)} event(s)."]
                if close:
                    parts.append("Marked for closing.")
                if 0 <= severity <= 5:
                    parts.append(f"Severity set to {severity}.")
                if suppress_until is not None:
                    parts.append(
                        "Snoozed until each resolves."
                        if suppress_until == 0
                        else f"Snoozed for {suppress_hours:g}h."
                    )
                if unsuppress:
                    parts.append("Suppression lifted.")
                if message:
                    parts.append(f"Message: {message}")
                return " ".join(parts)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error querying Zabbix: {e}"

    if "rank_problem_cause" not in skip:

        @mcp.tool()
        async def rank_problem_cause(
            symptom_event_ids: str,
            cause_event_id: str = "",
            unrank: bool = False,
            message: str = "",
            instance: str = "",
        ) -> str:
            """Mark problems as symptoms of a cause event (or rank them back).

            Writes the correlation into Zabbix itself (6.4+): the UI nests
            the symptoms under the cause, and every consumer sees one
            incident instead of N. The natural follow-up to
            get_outage_clusters — paste the cluster's event IDs here to
            collapse it at the source.

            Args:
                symptom_event_ids: Comma- or space-separated event IDs to rank
                cause_event_id: The cause event the symptoms belong to
                    (required unless unrank=True)
                unrank: Rank the listed events back to independent causes
                message: Optional note recorded with the rank change
                instance: Zabbix instance name (optional)
            """
            ids = [
                e.strip() for e in symptom_event_ids.replace(",", " ").split()
                if e.strip()
            ]
            if not ids:
                return "No symptom event IDs provided."
            if not unrank and not cause_event_id:
                return "cause_event_id is required (or set unrank=True)."
            if unrank and cause_event_id:
                return "unrank does not take a cause_event_id."
            try:
                client = resolver.resolve(instance)
                payload: dict = {
                    "eventids": ids,
                    "action": _build_rank_action(unrank=unrank, message=message),
                }
                if message:
                    payload["message"] = message
                if not unrank:
                    payload["cause_eventid"] = cause_event_id
                await client.call("event.acknowledge", payload)

                if unrank:
                    return f"Ranked {len(ids)} event(s) back to independent cause."
                return (
                    f"Ranked {len(ids)} event(s) as symptoms of "
                    f"cause {cause_event_id}."
                )
            except (httpx.HTTPError, ValueError) as e:
                return f"Error querying Zabbix: {e}"
