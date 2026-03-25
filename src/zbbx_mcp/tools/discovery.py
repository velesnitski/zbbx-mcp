import httpx

from zbbx_mcp.resolver import InstanceResolver
from zbbx_mcp.formatters import _ts

LLD_STATUS = {"0": "Enabled", "1": "Disabled"}
LLD_STATE = {"0": "Normal", "1": "Not supported"}
LLD_TYPES = {
    "0": "Zabbix agent", "2": "Zabbix trapper", "3": "Simple check",
    "5": "Zabbix internal", "7": "Zabbix agent (active)", "10": "External check",
    "11": "Database monitor", "12": "IPMI agent", "13": "SSH agent",
    "14": "Telnet agent", "18": "Dependent item", "19": "HTTP agent",
    "20": "SNMP agent", "21": "Script",
}


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()) -> None:

    if "get_discovery_rules" not in skip:

        @mcp.tool()
        async def get_discovery_rules(
            host_id: str = "",
            search: str = "",
            max_results: int = 50,
            instance: str = "",
        ) -> str:
            """Get low-level discovery (LLD) rules.

            Args:
                host_id: Filter by host ID (optional)
                search: Search pattern for rule name or key (optional)
                max_results: Maximum number of results (default: 50)
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)
                params = {
                    "output": ["itemid", "name", "key_", "type", "status",
                               "state", "lastclock", "lifetime", "delay"],
                    "selectHosts": ["host"],
                    "sortfield": "name",
                    "limit": max_results,
                }
                if host_id:
                    params["hostids"] = [host_id]
                if search:
                    params["search"] = {"name": search, "key_": search}
                    params["searchWildcardsEnabled"] = True
                    params["searchByAny"] = True

                data = await client.call("discoveryrule.get", params)

                if not data:
                    return "No discovery rules found."

                lines = []
                for r in data:
                    rtype = LLD_TYPES.get(r.get("type", ""), "?")
                    status = LLD_STATUS.get(r.get("status", "0"), "?")
                    state = ""
                    if r.get("state") == "1":
                        state = " [NOT SUPPORTED]"
                    hosts = ", ".join(h["host"] for h in r.get("hosts", []))
                    clock = _ts(r.get("lastclock", "0"))
                    lines.append(
                        f"- **{r.get('name', '?')}** [{status}]{state}\n"
                        f"  key: `{r.get('key_', '?')}` | type: {rtype} | "
                        f"host: {hosts} | last: {clock} | "
                        f"id: {r.get('itemid', '?')}"
                    )

                header = f"**Found: {len(data)} discovery rules**"
                if len(data) >= max_results:
                    header += f" (showing first {max_results}, more may exist)"
                return f"{header}\n\n" + "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error querying Zabbix: {e}"
