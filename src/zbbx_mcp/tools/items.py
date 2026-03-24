import httpx

from zbbx_mcp.resolver import InstanceResolver
from zbbx_mcp.formatters import _ts


ITEM_TYPES = {
    "0": "Zabbix agent",
    "2": "Zabbix trapper",
    "3": "Simple check",
    "5": "Zabbix internal",
    "7": "Zabbix agent (active)",
    "9": "Web item",
    "10": "External check",
    "11": "Database monitor",
    "12": "IPMI agent",
    "13": "SSH agent",
    "14": "Telnet agent",
    "15": "Calculated",
    "17": "SNMP trap",
    "18": "Dependent item",
    "19": "HTTP agent",
    "20": "SNMP agent",
    "21": "Script",
}

VALUE_TYPES = {
    "0": "float",
    "1": "character",
    "2": "log",
    "3": "unsigned int",
    "4": "text",
}


def _format_value(value: str, units: str) -> str:
    """Format a metric value with units."""
    if not value:
        return "N/A"
    try:
        num = float(value)
        if units in ("B", "Bps", "bps"):
            if num >= 1_073_741_824:
                return f"{num / 1_073_741_824:.2f} G{units}"
            if num >= 1_048_576:
                return f"{num / 1_048_576:.2f} M{units}"
            if num >= 1024:
                return f"{num / 1024:.2f} K{units}"
        elif units == "%":
            return f"{num:.1f}%"
        elif units == "s":
            if num >= 86400:
                return f"{num / 86400:.1f}d"
            if num >= 3600:
                return f"{num / 3600:.1f}h"
            if num >= 60:
                return f"{num / 60:.1f}m"
            return f"{num:.1f}s"
        if num == int(num):
            return f"{int(num)} {units}".strip()
        return f"{num:.2f} {units}".strip()
    except (ValueError, TypeError):
        return f"{value} {units}".strip()


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()):

    if "create_item" not in skip:

        @mcp.tool()
        async def create_item(
            host_id: str,
            name: str,
            key: str,
            value_type: int = 0,
            item_type: int = 7,
            delay: str = "60s",
            units: str = "",
            description: str = "",
            instance: str = "",
        ) -> str:
            """Create a new Zabbix item on a host.

            Args:
                host_id: Host ID to create the item on
                name: Item name
                key: Item key (e.g., 'system.cpu.load[all,avg1]')
                value_type: 0=float, 1=char, 2=log, 3=uint, 4=text (default: 0)
                item_type: Item type: 0=agent, 2=trapper, 7=agent(active), etc. (default: 7)
                delay: Update interval (default: 60s)
                units: Value units (optional, e.g., 'B', '%', 's')
                description: Description (optional)
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)
                params = {
                    "hostid": host_id,
                    "name": name,
                    "key_": key,
                    "type": item_type,
                    "value_type": value_type,
                    "delay": delay,
                }
                if units:
                    params["units"] = units
                if description:
                    params["description"] = description

                result = await client.call("item.create", params)
                iid = result.get("itemids", ["?"])[0]
                client.record_create("item", iid, f"Created item '{name}'")
                return f"Item created. ID: {iid}"
            except (httpx.HTTPError, ValueError) as e:
                return f"Error creating item: {e}"

    if "update_item" not in skip:

        @mcp.tool()
        async def update_item(
            item_id: str,
            name: str = "",
            delay: str = "",
            status: int = -1,
            description: str = "",
            instance: str = "",
        ) -> str:
            """Update an existing Zabbix item.

            Args:
                item_id: Item ID to update
                name: New name (optional)
                delay: New update interval (optional)
                status: 0=enabled, 1=disabled (optional, -1 to skip)
                description: New description (optional)
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)
                await client.snapshot_and_record("update", "item", item_id, f"Updated item {item_id}")

                params = {"itemid": item_id}
                if name:
                    params["name"] = name
                if delay:
                    params["delay"] = delay
                if status >= 0:
                    params["status"] = status
                if description:
                    params["description"] = description

                await client.call("item.update", params)
                return f"Item {item_id} updated."
            except (httpx.HTTPError, ValueError) as e:
                return f"Error updating item: {e}"

    if "delete_item" not in skip:

        @mcp.tool()
        async def delete_item(item_id: str, instance: str = "") -> str:
            """Delete a Zabbix item.

            Args:
                item_id: Item ID to delete
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)
                await client.snapshot_and_record("delete", "item", item_id, f"Deleted item {item_id}")
                await client.call("item.delete", [item_id])
                return f"Item {item_id} deleted."
            except (httpx.HTTPError, ValueError) as e:
                return f"Error deleting item: {e}"

    if "get_host_items" not in skip:

        @mcp.tool()
        async def get_host_items(
            host_id: str,
            search: str = "",
            max_results: int = 50,
            instance: str = "",
        ) -> str:
            """Get monitored items (metrics) for a Zabbix host.

            Args:
                host_id: Zabbix host ID
                search: Search pattern for item name or key (optional)
                max_results: Maximum number of results (default: 50)
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)
                params = {
                    "hostids": [host_id],
                    "output": ["itemid", "name", "key_", "lastvalue", "units",
                               "lastclock", "type", "value_type", "state", "status"],
                    "sortfield": "name",
                    "limit": max_results,
                    "filter": {"status": "0"},  # enabled items only
                }
                if search:
                    params["search"] = {"name": search, "key_": search}
                    params["searchWildcardsEnabled"] = True
                    params["searchByAny"] = True

                data = await client.call("item.get", params)

                if not data:
                    return "No items found."

                lines = []
                for item in data:
                    value = _format_value(item.get("lastvalue", ""), item.get("units", ""))
                    clock = _ts(item.get("lastclock", "0"))
                    state = " [UNSUPPORTED]" if item.get("state") == "1" else ""
                    lines.append(
                        f"- **{item.get('name', '?')}** = {value}{state}\n"
                        f"  key: `{item.get('key_', '?')}` | "
                        f"id: {item.get('itemid', '?')} | "
                        f"updated: {clock}"
                    )

                header = f"**Found: {len(data)} items**"
                if len(data) >= max_results:
                    header += f" (showing first {max_results}, more may exist)"
                return f"{header}\n\n" + "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error querying Zabbix: {e}"

    if "get_item_history" not in skip:

        @mcp.tool()
        async def get_item_history(
            item_id: str,
            value_type: int = 0,
            limit: int = 20,
            time_from: str = "",
            instance: str = "",
        ) -> str:
            """Get history data for a specific Zabbix item.

            Args:
                item_id: Zabbix item ID
                value_type: 0=float, 1=character, 2=log, 3=unsigned int, 4=text (default: 0)
                limit: Number of history records to return (default: 20)
                time_from: Unix timestamp to start from (optional, default: last records)
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)

                # Get item metadata first
                items = await client.call("item.get", {
                    "itemids": [item_id],
                    "output": ["itemid", "name", "key_", "units", "value_type"],
                })
                if not items:
                    return f"Item '{item_id}' not found."

                item = items[0]
                vtype = int(item.get("value_type", value_type))
                units = item.get("units", "")

                params = {
                    "itemids": [item_id],
                    "history": vtype,
                    "sortfield": "clock",
                    "sortorder": "DESC",
                    "limit": limit,
                    "output": "extend",
                }
                if time_from:
                    params["time_from"] = int(time_from)

                data = await client.call("history.get", params)

                if not data:
                    return f"No history data for item '{item.get('name', item_id)}'."

                parts = [
                    f"# History: {item.get('name', '?')}",
                    f"**Key:** `{item.get('key_', '?')}`",
                    f"**Item ID:** {item_id}",
                    f"**Records:** {len(data)}",
                    "",
                    "| Time | Value |",
                    "|------|-------|",
                ]

                for record in data:
                    ts = _ts(record.get("clock", "0"))
                    val = _format_value(record.get("value", ""), units)
                    parts.append(f"| {ts} | {val} |")

                return "\n".join(parts)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error querying Zabbix: {e}"

    if "get_graphs" not in skip:

        @mcp.tool()
        async def get_graphs(
            host_id: str,
            search: str = "",
            max_results: int = 50,
            instance: str = "",
        ) -> str:
            """List graphs defined for a Zabbix host.

            Args:
                host_id: Zabbix host ID
                search: Search pattern for graph name (optional)
                max_results: Maximum number of results (default: 50)
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)
                params = {
                    "hostids": [host_id],
                    "output": ["graphid", "name", "graphtype", "width", "height"],
                    "selectGraphItems": ["itemid", "color", "drawtype"],
                    "sortfield": "name",
                    "limit": max_results,
                }
                if search:
                    params["search"] = {"name": search}
                    params["searchWildcardsEnabled"] = True

                data = await client.call("graph.get", params)

                if not data:
                    return "No graphs found."

                graph_types = {"0": "Normal", "1": "Stacked", "2": "Pie", "3": "Exploded"}
                lines = []
                for g in data:
                    gtype = graph_types.get(g.get("graphtype", "0"), "?")
                    item_count = len(g.get("gitems", []))
                    lines.append(
                        f"- **{g.get('name', '?')}** ({gtype}, {item_count} items) "
                        f"— graphid: {g.get('graphid', '?')}"
                    )

                header = f"**Found: {len(data)} graphs**"
                if len(data) >= max_results:
                    header += f" (showing first {max_results}, more may exist)"
                return f"{header}\n\n" + "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error querying Zabbix: {e}"
