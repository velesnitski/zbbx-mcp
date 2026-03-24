import httpx

from zbbx_mcp.resolver import InstanceResolver

SCRIPT_TYPES = {"0": "Script", "1": "IPMI", "5": "Webhook"}
EXECUTE_ON = {"0": "Zabbix agent", "1": "Zabbix server", "2": "Zabbix server (proxy)"}


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()):

    if "get_scripts" not in skip:

        @mcp.tool()
        async def get_scripts(
            host_id: str = "",
            search: str = "",
            instance: str = "",
        ) -> str:
            """Get available Zabbix scripts.

            Args:
                host_id: Get scripts available for this host (optional)
                search: Search pattern for script name (optional)
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)
                params = {
                    "output": ["scriptid", "name", "type", "execute_on", "command",
                               "description", "scope"],
                    "sortfield": "name",
                }
                if host_id:
                    params["hostids"] = [host_id]
                if search:
                    params["search"] = {"name": search}
                    params["searchWildcardsEnabled"] = True

                data = await client.call("script.get", params)

                if not data:
                    return "No scripts found."

                lines = []
                for s in data:
                    stype = SCRIPT_TYPES.get(s.get("type", "0"), "?")
                    exec_on = EXECUTE_ON.get(s.get("execute_on", "1"), "?")
                    desc = ""
                    if s.get("description"):
                        desc = f"\n  {s['description'][:100]}"
                    lines.append(
                        f"- **{s.get('name', '?')}** ({stype}, runs on: {exec_on}) "
                        f"— scriptid: {s.get('scriptid', '?')}{desc}"
                    )

                return f"**Found: {len(data)} scripts**\n\n" + "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error querying Zabbix: {e}"

    if "execute_script" not in skip:

        @mcp.tool()
        async def execute_script(
            script_id: str,
            host_id: str,
            instance: str = "",
        ) -> str:
            """Execute a script on a Zabbix host.

            Args:
                script_id: Script ID to execute
                host_id: Host ID to execute the script on
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)
                result = await client.call("script.execute", {
                    "scriptid": script_id,
                    "hostid": host_id,
                })

                response = result.get("response", "success")
                value = result.get("value", "")

                parts = [f"**Script execution: {response}**"]
                if value:
                    parts.append(f"\n```\n{value}\n```")

                return "\n".join(parts)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error executing script: {e}"
