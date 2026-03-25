import httpx

from zbbx_mcp.resolver import InstanceResolver
from zbbx_mcp.formatters import format_hostgroup_list


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()) -> None:

    if "get_hostgroups" not in skip:

        @mcp.tool()
        async def get_hostgroups(query: str = "", include_hosts: bool = False, instance: str = "") -> str:
            """Get Zabbix host groups.

            Args:
                query: Search pattern for group name (optional)
                include_hosts: Include list of hosts in each group (default: False)
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)
                params = {
                    "output": ["groupid", "name"],
                    "sortfield": "name",
                }
                if query:
                    params["search"] = {"name": query}
                    params["searchWildcardsEnabled"] = True
                if include_hosts:
                    params["selectHosts"] = ["hostid", "host", "name"]

                data = await client.call("hostgroup.get", params)

                result = format_hostgroup_list(data)
                count = len(data)
                if count == 0:
                    return result
                return f"**Found: {count} host groups**\n\n{result}"
            except (httpx.HTTPError, ValueError) as e:
                return f"Error querying Zabbix: {e}"

    if "create_hostgroup" not in skip:

        @mcp.tool()
        async def create_hostgroup(name: str, instance: str = "") -> str:
            """Create a new host group.

            Args:
                name: Host group name
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)
                result = await client.call("hostgroup.create", {"name": name})
                gid = result.get("groupids", ["?"])[0]
                client.record_create("hostgroup", gid, f"Created host group '{name}'")
                return f"Host group created. ID: {gid}"
            except (httpx.HTTPError, ValueError) as e:
                return f"Error creating host group: {e}"

    if "delete_hostgroup" not in skip:

        @mcp.tool()
        async def delete_hostgroup(group_id: str, instance: str = "") -> str:
            """Delete a host group.

            Args:
                group_id: Host group ID to delete
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)
                await client.snapshot_and_record("delete", "hostgroup", group_id, f"Deleted host group {group_id}")
                await client.call("hostgroup.delete", [group_id])
                return f"Host group {group_id} deleted."
            except (httpx.HTTPError, ValueError) as e:
                return f"Error deleting host group: {e}"
