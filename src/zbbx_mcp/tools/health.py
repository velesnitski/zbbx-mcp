import httpx

from zbbx_mcp.resolver import InstanceResolver


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()) -> None:

    if "check_connection" not in skip:

        @mcp.tool()
        async def check_connection(instance: str = "") -> str:
            """Check connectivity to a Zabbix server and return its version.

            Args:
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)
                version = await client.call("apiinfo.version", {})
                return f"Connected. Zabbix version: {version}"
            except (httpx.HTTPError, ValueError) as e:
                return f"Connection failed: {e}"
