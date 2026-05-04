import time

import httpx

from zbbx_mcp.formatters import _ts, format_severity
from zbbx_mcp.resolver import InstanceResolver
from zbbx_mcp.utils import parse_time, resolve_group_ids

TRIGGER_STATES = {"0": "Normal", "1": "Unknown"}
TRIGGER_STATUS = {"0": "Enabled", "1": "Disabled"}
TRIGGER_PRIORITY = {
    "0": "Not classified", "1": "Information", "2": "Warning",
    "3": "Average", "4": "High", "5": "Disaster",
}


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()) -> None:

    if "get_triggers" not in skip:

        @mcp.tool()
        async def get_triggers(
            host_id: str = "",
            group: str = "",
            min_severity: int = 0,
            only_problems: bool = False,
            search: str = "",
            max_results: int = 50,
            instance: str = "",
        ) -> str:
            """Get Zabbix triggers with filtering.

            Args:
                host_id: Filter by host ID (optional)
                group: Filter by host group name (optional)
                min_severity: Minimum severity 0-5 (default: 0)
                only_problems: Only show triggers in problem state (default: False)
                search: Search pattern for trigger description (optional)
                max_results: Maximum number of results (default: 50)
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)
                params = {
                    "output": ["triggerid", "description", "priority", "value",
                               "state", "status", "lastchange", "expression"],
                    "selectHosts": ["host"],
                    "sortfield": "priority",
                    "sortorder": "DESC",
                    "limit": max_results,
                    "expandDescription": True,
                }
                if host_id:
                    params["hostids"] = [host_id]
                if min_severity > 0:
                    params["min_severity"] = min_severity
                if only_problems:
                    params["only_true"] = True
                    params["filter"] = {"value": "1"}
                if search:
                    params["search"] = {"description": search}
                    params["searchWildcardsEnabled"] = True
                if group:
                    gids = await resolve_group_ids(client, group)
                    if gids is None:
                        return f"Host group '{group}' not found."
                    params["groupids"] = gids

                data = await client.call("trigger.get", params)

                if not data:
                    return "No triggers found."

                lines = []
                for t in data:
                    sev = format_severity(t.get("priority", "0"))
                    state = "PROBLEM" if t.get("value") == "1" else "OK"
                    hosts = ", ".join(h["host"] for h in t.get("hosts", []))
                    changed = _ts(t.get("lastchange", "0"))
                    enabled = "" if t.get("status") == "0" else " [DISABLED]"
                    lines.append(
                        f"- **[{sev}]** {t.get('description', '?')} "
                        f"[{state}]{enabled} — {hosts}\n"
                        f"  changed: {changed} | triggerid: {t.get('triggerid', '?')}"
                    )

                header = f"**Found: {len(data)} triggers**"
                if len(data) >= max_results:
                    header += f" (showing first {max_results}, more may exist)"
                return f"{header}\n\n" + "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error querying Zabbix: {e}"

    if "create_trigger" not in skip:

        @mcp.tool()
        async def create_trigger(
            description: str,
            expression: str,
            priority: int = 0,
            comments: str = "",
            instance: str = "",
        ) -> str:
            """Create a new Zabbix trigger.

            Args:
                description: Trigger name/description
                expression: Trigger expression (e.g., 'avg(/host/key,5m)>80')
                priority: Severity 0-5 (0=Not classified, 5=Disaster, default: 0)
                comments: Additional comments (optional)
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)
                params = {
                    "description": description,
                    "expression": expression,
                    "priority": priority,
                }
                if comments:
                    params["comments"] = comments

                result = await client.call("trigger.create", params)
                tid = result.get("triggerids", ["?"])[0]
                client.record_create("trigger", tid, f"Created trigger '{description}'")
                return f"Trigger created. ID: {tid}"
            except (httpx.HTTPError, ValueError) as e:
                return f"Error creating trigger: {e}"

    if "update_trigger" not in skip:

        @mcp.tool()
        async def update_trigger(
            trigger_id: str,
            description: str = "",
            priority: int = -1,
            status: int = -1,
            comments: str = "",
            instance: str = "",
        ) -> str:
            """Update an existing Zabbix trigger.

            Args:
                trigger_id: Trigger ID to update
                description: New description (optional)
                priority: New severity 0-5 (optional, -1 to skip)
                status: 0=enabled, 1=disabled (optional, -1 to skip)
                comments: New comments (optional, empty string clears)
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)
                await client.snapshot_and_record("update", "trigger", trigger_id, f"Updated trigger {trigger_id}")

                params = {"triggerid": trigger_id}
                if description:
                    params["description"] = description
                if priority >= 0:
                    params["priority"] = priority
                if status >= 0:
                    params["status"] = status
                if comments:
                    params["comments"] = comments

                await client.call("trigger.update", params)
                return f"Trigger {trigger_id} updated."
            except (httpx.HTTPError, ValueError) as e:
                return f"Error updating trigger: {e}"

    if "delete_trigger" not in skip:

        @mcp.tool()
        async def delete_trigger(trigger_id: str, instance: str = "") -> str:
            """Delete a Zabbix trigger.

            Args:
                trigger_id: Trigger ID to delete
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)
                await client.snapshot_and_record("delete", "trigger", trigger_id, f"Deleted trigger {trigger_id}")
                await client.call("trigger.delete", [trigger_id])
                return f"Trigger {trigger_id} deleted."
            except (httpx.HTTPError, ValueError) as e:
                return f"Error deleting trigger: {e}"

    if "get_trigger_timeline" not in skip:

        @mcp.tool()
        async def get_trigger_timeline(
            trigger_id: str,
            hours: int = 24,
            time_from: str = "",
            time_till: str = "",
            max_results: int = 100,
            instance: str = "",
        ) -> str:
            """Return OK↔PROBLEM transitions for a trigger over a window.

            Args:
                trigger_id: Zabbix trigger ID
                hours: Lookback window (default: 24; ignored if time_from is set)
                time_from: Start of window — epoch, ISO, or relative (optional)
                time_till: End of window (optional, defaults to now)
                max_results: Max transitions to return (default: 100)
                instance: Zabbix instance name (optional)
            """
            try:
                client = resolver.resolve(instance)
                now = int(time.time())

                if time_from:
                    try:
                        tf = parse_time(time_from)
                    except ValueError as e:
                        return f"Invalid time_from: {e}"
                else:
                    tf = now - hours * 3600

                tt = now
                if time_till:
                    try:
                        tt = parse_time(time_till)
                    except ValueError as e:
                        return f"Invalid time_till: {e}"

                triggers = await client.call("trigger.get", {
                    "triggerids": [trigger_id],
                    "output": ["triggerid", "description", "priority"],
                    "selectHosts": ["host"],
                    "expandDescription": True,
                })
                if not triggers:
                    return f"Trigger '{trigger_id}' not found."

                trig = triggers[0]
                trig_hosts = ", ".join(h.get("host", "?") for h in trig.get("hosts", []))

                events = await client.call("event.get", {
                    "objectids": [trigger_id],
                    "source": 0, "object": 0,
                    "time_from": tf, "time_till": tt,
                    "output": ["eventid", "clock", "value", "acknowledged"],
                    "sortfield": ["clock"],
                    "sortorder": ["ASC"],
                    "limit": max_results,
                })

                if not events:
                    return (
                        f"No transitions for trigger '{trig.get('description', trigger_id)}' "
                        f"in window ({_ts(str(tf))} → {_ts(str(tt))})."
                    )

                transitions = 0
                last_state = None
                for e in events:
                    if e.get("value") != last_state:
                        transitions += 1
                        last_state = e.get("value")

                parts = [
                    f"# Timeline: {trig.get('description', '?')}",
                    "",
                    f"**Trigger ID:** {trigger_id}",
                    f"**Hosts:** {trig_hosts or '?'}",
                    f"**Severity:** {format_severity(trig.get('priority', '0'))}",
                    f"**Window:** {_ts(str(tf))} → {_ts(str(tt))}",
                    f"**Transitions:** {transitions}",
                    "",
                    "| Time | State | Event ID | Ack |",
                    "|------|-------|----------|-----|",
                ]
                for e in events:
                    state = "PROBLEM" if e.get("value") == "1" else "OK"
                    ack = "yes" if e.get("acknowledged") == "1" else "no"
                    parts.append(
                        f"| {_ts(e.get('clock', '0'))} | {state} | "
                        f"{e.get('eventid', '?')} | {ack} |"
                    )

                if len(events) >= max_results:
                    parts.append(f"\n*limit {max_results} reached — narrow the window for full history*")

                return "\n".join(parts)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"
