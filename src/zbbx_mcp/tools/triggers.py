import httpx

from zbbx_mcp.resolver import InstanceResolver
from zbbx_mcp.formatters import format_severity, _ts

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
                    groups = await client.call("hostgroup.get", {
                        "output": ["groupid"], "filter": {"name": [group]},
                    })
                    if not groups:
                        return f"Host group '{group}' not found."
                    params["groupids"] = [g["groupid"] for g in groups]

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
