import httpx

from zbbx_mcp.resolver import InstanceResolver
from zbbx_mcp.formatters import format_severity, _ts

EVENT_SOURCES = {"0": "Trigger", "1": "Discovery", "2": "Autoregistration", "3": "Internal"}
EVENT_VALUES = {
    "0": {"0": "OK", "1": "PROBLEM"},
    "1": {"0": "Up", "1": "Down", "2": "Discovered", "3": "Lost"},
    "2": {"0": "Registered"},
    "3": {"0": "Normal", "1": "Unknown"},
}


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()):

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
