import re
import time

import httpx

from zbbx_mcp.formatters import _ts, cell
from zbbx_mcp.resolver import InstanceResolver
from zbbx_mcp.utils import parse_time, resolve_group_ids

_DELAY_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_DELAY_RE = re.compile(r"^\s*(\d+)\s*([smhd]?)\s*$", re.IGNORECASE)


def _collapse_dependent_chain(stale: list[dict]) -> list[dict]:
    """Fold downstream dependent stale items into their stale master.

    Each input record needs a ``master_itemid`` (empty string when the
    item has no master) and an ``itemid``. When a stale item's master is
    also in the stale list, the child is suppressed from the visible
    output and contributed to the master's ``affected_count``. Two-hop
    chains collapse correctly (grandchild folds into root, not into the
    intermediate).

    Returns a new list — input is not mutated.
    """
    by_id = {s["itemid"]: dict(s, affected_count=0) for s in stale}
    stale_ids = set(by_id.keys())

    def root_of(itemid: str) -> str:
        seen: set[str] = set()
        cur = itemid
        while True:
            if cur in seen:
                return cur  # circular ref — stop at first revisit
            seen.add(cur)
            master = by_id.get(cur, {}).get("master_itemid", "")
            if not master or master not in stale_ids:
                return cur
            cur = master

    visible_roots: dict[str, dict] = {}
    for s in stale:
        root_id = root_of(s["itemid"])
        if root_id == s["itemid"]:
            visible_roots.setdefault(root_id, by_id[root_id])
        else:
            visible_roots.setdefault(root_id, by_id[root_id])
            visible_roots[root_id]["affected_count"] += 1
    return list(visible_roots.values())


def _parse_delay_seconds(delay: str, default: int = 300) -> int:
    """Parse a Zabbix item delay into seconds.

    Simple forms: "60", "60s", "5m", "1h". Complex schedules
    (e.g. "30s;wd1-5,9:00-18:00/1m") fall back to default.
    """
    if not delay:
        return default
    head = delay.split(";", 1)[0].strip()
    m = _DELAY_RE.match(head)
    if not m:
        return default
    qty = int(m.group(1))
    unit = (m.group(2) or "s").lower()
    return qty * _DELAY_UNITS.get(unit, 1)


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


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()) -> None:

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
                    q = search if "*" in search else f"*{search}*"
                    params["search"] = {"name": q, "key_": q}
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

    if "search_items" not in skip:

        @mcp.tool()
        async def search_items(
            search: str,
            group: str = "",
            country: str = "",
            max_results: int = 30,
            instance: str = "",
        ) -> str:
            """Search items across ALL hosts by name or key pattern.

            Args:
                search: Item name or key pattern (substring match)
                group: Filter by host group name (optional)
                country: Filter by country code (optional)
                max_results: Maximum results (default: 30)
                instance: Zabbix instance name (optional)
            """
            try:
                client = resolver.resolve(instance)
                from zbbx_mcp.data import extract_country

                # Build host filter
                host_params: dict = {"output": ["hostid", "host"], "filter": {"status": "0"}}
                if group:
                    groups = await client.call("hostgroup.get", {
                        "output": ["groupid"], "filter": {"name": [group]},
                    })
                    if groups:
                        host_params["groupids"] = [g["groupid"] for g in groups]
                    else:
                        return f"Host group '{group}' not found."

                hosts = await client.call("host.get", host_params)
                if country:
                    hosts = [h for h in hosts if extract_country(h["host"]).lower() == country.lower()]

                if not hosts:
                    return "No hosts match the filter."

                hids = [h["hostid"] for h in hosts]
                host_map = {h["hostid"]: h["host"] for h in hosts}

                q = search if "*" in search else f"*{search}*"
                items = await client.call("item.get", {
                    "hostids": hids,
                    "output": ["itemid", "hostid", "name", "key_", "lastvalue", "units", "status"],
                    "search": {"name": q, "key_": q},
                    "searchWildcardsEnabled": True,
                    "searchByAny": True,
                    "filter": {"status": "0"},
                    "limit": max_results,
                    "sortfield": "name",
                })

                if not items:
                    return f"No items matching '{search}' found across {len(hosts)} hosts."

                lines = []
                for item in items:
                    hostname = host_map.get(item["hostid"], "?")
                    value = _format_value(item.get("lastvalue", ""), item.get("units", ""))
                    lines.append(
                        f"| {hostname} | {item.get('name', '?')} | `{item.get('key_', '?')}` | {value} |"
                    )

                total = len(items)
                header = f"**{total} items** matching '{search}' across {len(hosts)} hosts"
                if total >= max_results:
                    header += f" (limit {max_results})"
                table = "| Host | Item | Key | Value |\n|------|------|-----|-------|\n"
                return f"{header}\n\n{table}" + "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

    if "get_item_history" not in skip:

        @mcp.tool()
        async def get_item_history(
            item_id: str,
            value_type: int = 0,
            limit: int = 20,
            time_from: str = "",
            time_till: str = "",
            instance: str = "",
        ) -> str:
            """Get history data for a specific Zabbix item.

            Args:
                item_id: Zabbix item ID
                value_type: 0=float, 1=character, 2=log, 3=unsigned int, 4=text (default: 0)
                limit: Number of history records to return (default: 20)
                time_from: Start time — epoch, ISO ("2026-04-19"), ISO datetime, or relative ("24h", "7d")
                time_till: End time — same formats as time_from (optional)
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
                    try:
                        params["time_from"] = parse_time(time_from)
                    except ValueError as e:
                        return f"Invalid time_from: {e}"
                if time_till:
                    try:
                        params["time_till"] = parse_time(time_till)
                    except ValueError as e:
                        return f"Invalid time_till: {e}"

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

    if "get_stale_items" not in skip:

        @mcp.tool()
        async def get_stale_items(
            host_id: str = "",
            group: str = "",
            stale_multiplier: float = 3.0,
            include_triggers: bool = True,
            collapse_dependencies: bool = False,
            max_results: int = 30,
            instance: str = "",
        ) -> str:
            """Find items whose monitoring is broken — unsupported or polling stopped.

            Args:
                host_id: Filter by host ID (optional)
                group: Filter by host group name (optional)
                stale_multiplier: Flag items whose lastclock is older than N x delay (default: 3.0)
                include_triggers: Also list triggers that depend on stale items (default: True)
                collapse_dependencies: Show only root-cause items, fold downstream dependents into "+N affected" (default: False)
                max_results: Max items to report (default: 30)
                instance: Zabbix instance name (optional)
            """
            try:
                client = resolver.resolve(instance)
                now = int(time.time())

                output_fields = ["itemid", "hostid", "name", "key_", "state",
                                 "lastclock", "delay", "error"]
                if collapse_dependencies:
                    output_fields.append("master_itemid")
                params = {
                    "output": output_fields,
                    "selectHosts": ["host"],
                    "filter": {"status": "0"},
                    "sortfield": "name",
                    "limit": 5000,
                }
                if include_triggers:
                    params["selectTriggers"] = ["triggerid", "description", "value", "status"]
                if host_id:
                    params["hostids"] = [host_id]
                if group:
                    gids = await resolve_group_ids(client, group)
                    if gids is None:
                        return f"Host group '{group}' not found."
                    params["groupids"] = gids

                items = await client.call("item.get", params)

                stale = []
                for it in items:
                    state = it.get("state", "0")
                    lastclock = int(it.get("lastclock") or 0)
                    delay_s = _parse_delay_seconds(it.get("delay", ""))
                    threshold = max(int(delay_s * stale_multiplier), 60)

                    unsupported = state == "1"
                    frozen = lastclock > 0 and (now - lastclock) > threshold
                    never_collected = lastclock == 0

                    if not (unsupported or frozen or never_collected):
                        continue

                    if unsupported:
                        reason = "UNSUPPORTED"
                    elif never_collected:
                        reason = "no data"
                    else:
                        age = now - lastclock
                        reason = f"frozen {age // 60}m (expected ≤ {threshold // 60}m)"

                    host = (it.get("hosts") or [{}])[0].get("host", "?")
                    stale.append({
                        "itemid": it.get("itemid", "?"),
                        "host": host,
                        "name": it.get("name", "?"),
                        "key": it.get("key_", "?"),
                        "reason": reason,
                        "error": it.get("error", "") or "",
                        "triggers": it.get("triggers", []) if include_triggers else [],
                        "lastclock": lastclock,
                        "master_itemid": it.get("master_itemid", "") or "",
                    })

                if not stale:
                    return (
                        f"No stale items found (checked {len(items)} items, "
                        f"threshold {stale_multiplier}x delay)."
                    )

                raw_total = len(stale)
                if collapse_dependencies:
                    stale = _collapse_dependent_chain(stale)
                total = len(stale)
                stale.sort(key=lambda x: (x["reason"], x["host"]))
                stale = stale[:max_results]

                header = f"**{total} stale items**"
                if collapse_dependencies and raw_total > total:
                    header += f" ({raw_total - total} downstream collapsed)"
                header += f" (checked {len(items)}; threshold {stale_multiplier}x delay)\n"
                lines = [header]
                affected_col = " | Affected" if collapse_dependencies else ""
                divider_col = "|----------" if collapse_dependencies else ""
                lines.append(f"| Host | Item | Key | Reason | Last value{affected_col} |")
                lines.append(f"|------|------|-----|--------|-----------{divider_col}|")
                for s in stale:
                    lv = _ts(str(s["lastclock"])) if s["lastclock"] else "never"
                    affected_cell = ""
                    if collapse_dependencies:
                        n = s.get("affected_count", 0)
                        affected_cell = f" | +{n}" if n else " | —"
                    lines.append(
                        f"| {cell(s['host'])} | {cell(s['name'])} | `{cell(s['key'])}` | "
                        f"{cell(s['reason'])} | {lv}{affected_cell} |"
                    )

                if include_triggers:
                    affected = [s for s in stale if s["triggers"]]
                    if affected:
                        lines.append("\n**Triggers depending on stale items:**")
                        for s in affected:
                            active = [
                                t for t in s["triggers"]
                                if t.get("status") == "0" and t.get("value") == "1"
                            ]
                            if not active:
                                continue
                            lines.append(f"- *{s['host']} / {s['name']}*")
                            for t in active[:5]:
                                lines.append(
                                    f"  - [PROBLEM] {t.get('description', '?')} "
                                    f"(triggerid: {t.get('triggerid', '?')}) — "
                                    f"monitoring broken, not service"
                                )

                if total > max_results:
                    lines.append(f"\n*{total - max_results} more stale items omitted*")

                return "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

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
