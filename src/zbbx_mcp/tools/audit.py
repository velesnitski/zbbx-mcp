"""Zabbix audit log queries."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from zbbx_mcp.data import AUDIT_RESOURCE_HOST
from zbbx_mcp.resolver import InstanceResolver

# auditlog.get resourcetype values — the Zabbix 6.0+ audit constants.
#
# The previous table was offset from reality: it read 4 as "Trigger" (really
# Host) and 15 as "Host group" (really Item), so audit output confidently
# mislabelled every row — host operations shown as triggers, item operations
# as host groups — while the `resource=` filter selected a different object
# class than the caller asked for. Verified live against this instance for
# the two load-bearing codes; the remainder are the documented constants.
# Anything unmapped renders as "Type N" rather than borrowing a wrong label.
_RESOURCE_NAMES = {
    0: "User", 3: "Media type", 4: "Host", 5: "Action", 6: "Graph",
    11: "User group", 13: "Trigger", 14: "Host group", 15: "Item",
    16: "Image", 17: "Value map", 18: "Service", 19: "Map",
    22: "Web scenario", 23: "Discovery rule", 25: "Script", 26: "Relay",
    27: "Maintenance", 28: "Regular expression", 29: "Macro",
    30: "Template", 31: "Trigger prototype", 32: "Icon map",
    33: "Dashboard", 34: "Event correlation", 35: "Graph prototype",
    37: "Host prototype", 38: "Autoregistration", 39: "Module",
    40: "Settings", 41: "Housekeeping", 42: "Authentication",
    43: "Template dashboard", 44: "User role", 45: "API token",
    46: "Scheduled report", 47: "HA node", 48: "SLA",
    49: "User directory", 50: "Template group", 51: "Connector",
}

# Audit actions. The old table mapped 4 to "Login" (4 is Logout), invented
# 5 for "Failed login" (unassigned — so that filter could never match), and
# fabricated 6-9 as timeperiod operations.
_ACTION_NAMES = {
    0: "Add", 1: "Update", 2: "Delete", 4: "Logout", 7: "Execute",
    8: "Login", 9: "Failed login", 10: "History clear",
}


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()) -> None:

    if "get_audit_log" not in skip:

        @mcp.tool()
        async def get_audit_log(
            resource: str = "",
            action: str = "",
            user: str = "",
            host_id: str = "",
            time_from: str = "",
            time_till: str = "",
            max_results: int = 50,
            instance: str = "",
        ) -> str:
            """Query Zabbix audit log for host creation dates, user actions, and change history.

            Args:
                resource: Resource type: host, item, trigger, user, template, maintenance, endpoint (optional)
                action: Action filter: add, update, delete, login (optional)
                user: Filter by username (optional)
                host_id: Filter audit records related to a specific host ID (optional)
                time_from: Start time as YYYY-MM-DD or unix timestamp (optional)
                time_till: End time as YYYY-MM-DD or unix timestamp (optional)
                max_results: Maximum results (default: 50)
                instance: Zabbix instance (optional)
            """
            try:
                client = resolver.resolve(instance)

                params: dict = {
                    "output": "extend",
                    "sortfield": "clock",
                    "sortorder": "DESC",
                    "limit": max_results,
                }

                # Resource type filter
                resource_map = {
                    "user": 0, "media": 3, "host": AUDIT_RESOURCE_HOST,
                    "action": 5, "graph": 6, "usergroup": 11, "trigger": 13,
                    "hostgroup": 14, "host group": 14, "item": 15,
                    "service": 18, "map": 19, "discovery": 23, "script": 25,
                    "relay": 26, "maintenance": 27, "template": 30,
                    "dashboard": 33, "endpoint": AUDIT_RESOURCE_HOST,
                }
                if resource:
                    rid = resource_map.get(resource.lower())
                    if rid is not None:
                        params["filter"] = params.get("filter", {})
                        params["filter"]["resourcetype"] = rid

                # Action filter
                action_map = {
                    "add": 0, "create": 0, "update": 1, "delete": 2,
                    "logout": 4, "execute": 7, "login": 8,
                    "failed login": 9, "history clear": 10,
                }
                if action:
                    aid = action_map.get(action.lower())
                    if aid is not None:
                        params["filter"] = params.get("filter", {})
                        params["filter"]["action"] = aid

                # User filter
                if user:
                    users = await client.call("user.get", {
                        "output": ["userid"],
                        "filter": {"username": user},
                    })
                    if users:
                        params["userids"] = users[0]["userid"]

                # Host ID filter — search in resourceid
                if host_id:
                    params["filter"] = params.get("filter", {})
                    params["filter"]["resourcetype"] = AUDIT_RESOURCE_HOST
                    params["filter"]["resourceid"] = host_id

                # Time filters
                def _parse_time(val: str) -> int:
                    if val.isdigit():
                        return int(val)
                    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M"):
                        try:
                            dt = datetime.strptime(val, fmt).replace(tzinfo=timezone.utc)
                            return int(dt.timestamp())
                        except ValueError:
                            continue
                    return 0

                if time_from:
                    ts = _parse_time(time_from)
                    if ts:
                        params["time_from"] = ts
                if time_till:
                    ts = _parse_time(time_till)
                    if ts:
                        params["time_till"] = ts

                records = await client.call("auditlog.get", params)

                if not records:
                    return "No audit records found."

                parts = [
                    f"**Audit Log ({len(records)} records)**\n",
                    "| Time | User | Action | Resource | Name | Details |",
                    "|------|------|--------|----------|------|---------|",
                ]

                for r in records:
                    ts = datetime.fromtimestamp(int(r.get("clock", 0)), tz=timezone.utc)
                    time_str = ts.strftime("%Y-%m-%d %H:%M")
                    username = r.get("username", "")
                    act = _ACTION_NAMES.get(int(r.get("action", -1)), str(r.get("action", "")))
                    rt_raw = int(r.get("resourcetype", -1))
                    res_type = _RESOURCE_NAMES.get(rt_raw, f"Type {rt_raw}")
                    name = r.get("resourcename", "")
                    # Extract meaningful details from recordsetid/details
                    details = r.get("details", "")
                    if isinstance(details, str) and len(details) > 80:
                        details = details[:77] + "..."

                    parts.append(f"| {time_str} | {username} | {act} | {res_type} | {name} | {details} |")

                return "\n".join(parts)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error querying Zabbix: {e}"
