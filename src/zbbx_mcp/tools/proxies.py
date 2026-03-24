"""Zabbix proxy management."""

import httpx

from zbbx_mcp.resolver import InstanceResolver
from zbbx_mcp.formatters import _ts

# Zabbix 6.4 uses "host" field for proxy name, "status" for mode
PROXY_MODE = {"5": "Active", "6": "Passive"}


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()):

    if "get_proxies" not in skip:

        @mcp.tool()
        async def get_proxies(instance: str = "") -> str:
            """Get Zabbix proxies with their status and host counts.

            Args:
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)

                # Zabbix 6.4 proxy fields
                data = await client.call("proxy.get", {
                    "output": ["proxyid", "host", "status", "description",
                               "lastaccess", "tls_connect", "tls_accept"],
                    "selectHosts": ["hostid"],
                    "sortfield": "host",
                })

                if not data:
                    return "No proxies found."

                lines = []
                for p in data:
                    mode = PROXY_MODE.get(p.get("status", ""), p.get("status", "?"))
                    host_count = len(p.get("hosts", []))
                    last = _ts(p.get("lastaccess", "0"))
                    tls = ""
                    if p.get("tls_connect", "1") != "1":
                        tls = " [TLS]"
                    desc = ""
                    if p.get("description"):
                        desc = f"\n  {p['description'][:100]}"
                    lines.append(
                        f"- **{p.get('host', '?')}** ({mode}, {host_count} hosts){tls}\n"
                        f"  Last seen: {last} | "
                        f"proxyid: {p.get('proxyid', '?')}{desc}"
                    )

                return f"**Found: {len(data)} proxies**\n\n" + "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error querying Zabbix: {e}"
