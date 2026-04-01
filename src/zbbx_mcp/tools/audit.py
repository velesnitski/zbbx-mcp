"""Zabbix audit log queries."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from zbbx_mcp.resolver import InstanceResolver

# auditlog.get resourcetype values
_RESOURCE_NAMES = {
    0: "User", 2: "Host", 3: "Item", 4: "Trigger", 5: "Graph",
    6: "Template", 7: "Action", 12: "Script", 13: "Proxy",
    14: "Maintenance", 15: "Host group", 18: "Map", 19: "Discovery rule",
    22: "Media type", 23: "User group", 25: "Template link",
    29: "Discovery check", 33: "Dashboard", 34: "Service",
}

_ACTION_NAMES = {
    0: "Add", 1: "Update", 2: "Delete", 4: "Login", 5: "Failed login",
    6: "History clear", 7: "Timeperiod add", 8: "Timeperiod update",
    9: "Timeperiod delete",
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
                resource: Resource type: host, item, trigger, user, template, maintenance, proxy (optional)
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
                    "user": 0, "host": 2, "item": 3, "trigger": 4,
                    "graph": 5, "template": 6, "action": 7, "script": 12,
                    "proxy": 13, "maintenance": 14, "hostgroup": 15,
                    "host group": 15, "map": 18, "discovery": 19,
                    "media": 22, "usergroup": 23, "dashboard": 33,
                    "service": 34,
                }
                if resource:
                    rid = resource_map.get(resource.lower())
                    if rid is not None:
                        params["filter"] = params.get("filter", {})
                        params["filter"]["resourcetype"] = rid

                # Action filter
                action_map = {
                    "add": 0, "create": 0, "update": 1, "delete": 2,
                    "login": 4, "failed login": 5,
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
                    params["filter"]["resourcetype"] = 2  # Host
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
                    res_type = _RESOURCE_NAMES.get(int(r.get("resourcetype", -1)), str(r.get("resourcetype", "")))
                    name = r.get("resourcename", "")
                    # Extract meaningful details from recordsetid/details
                    details = r.get("details", "")
                    if isinstance(details, str) and len(details) > 80:
                        details = details[:77] + "..."

                    parts.append(f"| {time_str} | {username} | {act} | {res_type} | {name} | {details} |")

                return "\n".join(parts)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error querying Zabbix: {e}"
