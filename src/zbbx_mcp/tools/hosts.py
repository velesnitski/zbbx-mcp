import httpx

from zbbx_mcp.resolver import InstanceResolver
from zbbx_mcp.formatters import format_host_list, format_host_detail


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()):

    if "search_hosts" not in skip:

        @mcp.tool()
        async def search_hosts(query: str = "", group: str = "", max_results: int = 50, instance: str = "") -> str:
            """Search Zabbix hosts by name pattern or host group.

            Args:
                query: Host name search pattern (wildcards supported, e.g., 'web*')
                group: Filter by host group name
                max_results: Maximum number of results (default: 50)
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)
                params = {
                    "output": ["hostid", "host", "name", "status", "available"],
                    "limit": max_results,
                    "sortfield": "host",
                }
                if query:
                    params["search"] = {"host": query, "name": query}
                    params["searchWildcardsEnabled"] = True
                    params["searchByAny"] = True
                if group:
                    groups = await client.call("hostgroup.get", {
                        "output": ["groupid"],
                        "filter": {"name": [group]},
                    })
                    if groups:
                        params["groupids"] = [g["groupid"] for g in groups]
                    else:
                        return f"Host group '{group}' not found."

                data = await client.call("host.get", params)

                result = format_host_list(data)
                count = len(data)
                if count == 0:
                    return result
                header = f"**Found: {count} hosts**"
                if count >= max_results:
                    header += f" (showing first {max_results}, more may exist)"
                return f"{header}\n\n{result}"
            except (httpx.HTTPError, ValueError) as e:
                return f"Error querying Zabbix: {e}"

    if "create_host" not in skip:

        @mcp.tool()
        async def create_host(
            host: str,
            group_ids: str,
            name: str = "",
            ip: str = "",
            dns: str = "",
            port: str = "10050",
            template_ids: str = "",
            description: str = "",
            instance: str = "",
        ) -> str:
            """Create a new Zabbix host.

            Args:
                host: Technical host name (must be unique)
                group_ids: Comma-separated host group IDs (required)
                name: Visible name (optional, defaults to host)
                ip: IP address for agent interface (optional)
                dns: DNS name for agent interface (optional)
                port: Agent port (default: 10050)
                template_ids: Comma-separated template IDs to link (optional)
                description: Host description (optional)
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)
                params = {
                    "host": host,
                    "groups": [{"groupid": g.strip()} for g in group_ids.split(",")],
                }
                if name:
                    params["name"] = name
                if description:
                    params["description"] = description
                if template_ids:
                    params["templates"] = [{"templateid": t.strip()} for t in template_ids.split(",")]

                if ip or dns:
                    params["interfaces"] = [{
                        "type": 1,
                        "main": 1,
                        "useip": 1 if ip else 0,
                        "ip": ip or "",
                        "dns": dns or "",
                        "port": port,
                    }]

                result = await client.call("host.create", params)
                hid = result.get("hostids", ["?"])[0]
                client.record_create("host", hid, f"Created host '{host}'")
                return f"Host created. ID: {hid}"
            except (httpx.HTTPError, ValueError) as e:
                return f"Error creating host: {e}"

    if "update_host" not in skip:

        @mcp.tool()
        async def update_host(
            host_id: str,
            name: str = "",
            status: int = -1,
            description: str = "",
            instance: str = "",
        ) -> str:
            """Update an existing Zabbix host.

            Args:
                host_id: Host ID to update
                name: New visible name (optional)
                status: 0=enabled, 1=disabled (optional, -1 to skip)
                description: New description (optional)
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)
                await client.snapshot_and_record("update", "host", host_id, f"Updated host {host_id}")

                params = {"hostid": host_id}
                if name:
                    params["name"] = name
                if status >= 0:
                    params["status"] = status
                if description:
                    params["description"] = description

                await client.call("host.update", params)
                return f"Host {host_id} updated."
            except (httpx.HTTPError, ValueError) as e:
                return f"Error updating host: {e}"

    if "delete_host" not in skip:

        @mcp.tool()
        async def delete_host(host_id: str, instance: str = "") -> str:
            """Delete a Zabbix host.

            Args:
                host_id: Host ID to delete
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)
                await client.snapshot_and_record("delete", "host", host_id, f"Deleted host {host_id}")
                await client.call("host.delete", [host_id])
                return f"Host {host_id} deleted."
            except (httpx.HTTPError, ValueError) as e:
                return f"Error deleting host: {e}"

    if "get_host" not in skip:

        @mcp.tool()
        async def get_host(host_id: str, instance: str = "") -> str:
            """Get full details of a specific Zabbix host.

            Args:
                host_id: Zabbix host ID or hostname (auto-resolved)
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)

                # Resolve hostname to ID if not numeric
                if not host_id.isdigit():
                    lookup = await client.call("host.get", {
                        "output": ["hostid"],
                        "filter": {"host": [host_id]},
                    })
                    if not lookup:
                        lookup = await client.call("host.get", {
                            "output": ["hostid"],
                            "search": {"host": host_id, "name": host_id},
                            "searchByAny": True, "searchWildcardsEnabled": True,
                            "limit": 1,
                        })
                    if not lookup:
                        return f"Host '{host_id}' not found."
                    host_id = lookup[0]["hostid"]

                data = await client.call("host.get", {
                    "hostids": [host_id],
                    "output": "extend",
                    "selectGroups": ["name"],
                    "selectInterfaces": ["type", "ip", "port"],
                })

                if not data:
                    return f"Host '{host_id}' not found."
                return format_host_detail(data[0])
            except (httpx.HTTPError, ValueError) as e:
                return f"Error querying Zabbix: {e}"
