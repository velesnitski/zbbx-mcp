
import httpx

from zbbx_mcp.formatters import _ts
from zbbx_mcp.resolver import InstanceResolver
from zbbx_mcp.utils import resolve_group_ids

MAINTENANCE_TYPES = {"0": "With data collection", "1": "Without data collection"}


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()) -> None:

    if "get_maintenance" not in skip:

        @mcp.tool()
        async def get_maintenance(
            host_id: str = "",
            group: str = "",
            instance: str = "",
        ) -> str:
            """Get Zabbix maintenance windows.

            Args:
                host_id: Filter by host ID (optional)
                group: Filter by host group name (optional)
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)
                params = {
                    "output": ["maintenanceid", "name", "description",
                               "active_since", "active_till", "maintenance_type"],
                    "selectHosts": ["hostid", "host"],
                    "selectGroups": ["groupid", "name"],
                    "selectTimeperiods": "extend",
                    "sortfield": "name",
                }
                if host_id:
                    params["hostids"] = [host_id]
                if group:
                    gids = await resolve_group_ids(client, group)
                    if gids is None:
                        return f"Host group '{group}' not found."
                    params["groupids"] = gids

                data = await client.call("maintenance.get", params)

                if not data:
                    return "No maintenance windows found."

                lines = []
                for m in data:
                    mtype = MAINTENANCE_TYPES.get(m.get("maintenance_type", "0"), "?")
                    since = _ts(m.get("active_since", "0"))
                    till = _ts(m.get("active_till", "0"))
                    host_count = len(m.get("hosts", []))
                    group_count = len(m.get("groups", []))
                    desc = ""
                    if m.get("description"):
                        desc = f"\n  {m['description'][:100]}"
                    lines.append(
                        f"- **{m.get('name', '?')}** ({mtype})\n"
                        f"  {since} → {till} | "
                        f"{host_count} hosts, {group_count} groups "
                        f"(id: {m.get('maintenanceid', '?')}){desc}"
                    )

                return f"**Found: {len(data)} maintenance windows**\n\n" + "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error querying Zabbix: {e}"

    if "create_maintenance" not in skip:

        @mcp.tool()
        async def create_maintenance(
            name: str,
            active_since: str,
            active_till: str,
            host_ids: str = "",
            group_ids: str = "",
            collect_data: bool = True,
            description: str = "",
            instance: str = "",
        ) -> str:
            """Create a maintenance window.

            Args:
                name: Maintenance window name
                active_since: Start time as Unix timestamp
                active_till: End time as Unix timestamp
                host_ids: Comma-separated host IDs (optional)
                group_ids: Comma-separated host group IDs (optional)
                collect_data: Collect data during maintenance (default: True)
                description: Description (optional)
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                try:
                    since_ts = int(active_since)
                    till_ts = int(active_till)
                except (ValueError, TypeError):
                    return "Invalid timestamps. Use Unix timestamps (e.g., 1710000000)."
                if till_ts <= since_ts:
                    return "active_till must be after active_since."
                if till_ts - since_ts > 365 * 86400:
                    return "Maintenance window cannot exceed 1 year."

                client = resolver.resolve(instance)
                params = {
                    "name": name,
                    "active_since": since_ts,
                    "active_till": till_ts,
                    "maintenance_type": 0 if collect_data else 1,
                    "timeperiods": [{"timeperiod_type": 0, "period": till_ts - since_ts}],
                }
                if host_ids:
                    params["hostids"] = [h.strip() for h in host_ids.split(",")]
                if group_ids:
                    params["groupids"] = [g.strip() for g in group_ids.split(",")]
                if description:
                    params["description"] = description

                result = await client.call("maintenance.create", params)
                mid = result.get("maintenanceids", ["?"])[0]
                client.record_create("maintenance", mid, f"Created maintenance '{name}'")
                return f"Maintenance window created. ID: {mid}"
            except (httpx.HTTPError, ValueError) as e:
                return f"Error creating maintenance: {e}"

    if "delete_maintenance" not in skip:

        @mcp.tool()
        async def delete_maintenance(maintenance_id: str, instance: str = "") -> str:
            """Delete a maintenance window.

            Args:
                maintenance_id: Maintenance window ID to delete
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)
                await client.snapshot_and_record("delete", "maintenance", maintenance_id, f"Deleted maintenance {maintenance_id}")
                await client.call("maintenance.delete", [maintenance_id])
                return f"Maintenance window {maintenance_id} deleted."
            except (httpx.HTTPError, ValueError) as e:
                return f"Error deleting maintenance: {e}"
