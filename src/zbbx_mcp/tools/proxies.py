"""Zabbix proxy management."""

import httpx

from zbbx_mcp.formatters import _ts
from zbbx_mcp.resolver import InstanceResolver

# proxy.get `operating_mode` values (Zabbix 7.0+)
PROXY_MODE = {"0": "Active", "1": "Passive"}

# proxy.get `compatibility` values (Zabbix 6.4+): proxy version vs server
_COMPAT = {
    "1": "",  # current — no annotation needed
    "2": " ⚠ OUTDATED",
    "3": " ✗ UNSUPPORTED",
}


def format_proxy_compat(compatibility: str, version: str) -> str:
    """Render a proxy's version + compatibility as a short annotation.

    ``compatibility``: 0=undefined, 1=current, 2=outdated, 3=unsupported.
    ``version``: three-part version string, "0" when unknown.
    Pure helper.
    """
    ver = "" if version in ("", "0") else f" v{version}"
    return f"{ver}{_COMPAT.get(compatibility, '')}"


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()) -> None:

    if "get_proxies" not in skip:

        @mcp.tool()
        async def get_proxies(instance: str = "") -> str:
            """Get Zabbix proxies with mode, host counts, and version compatibility.

            Flags proxies whose version is outdated or unsupported relative
            to the server (Zabbix 6.4+ reports compatibility).

            Args:
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)

                # Zabbix 7.0 proxy object: `name` / `operating_mode`
                # (pre-7.0 `host` / `status` are gone).
                data = await client.call("proxy.get", {
                    "output": ["proxyid", "name", "operating_mode", "description",
                               "lastaccess", "tls_connect", "tls_accept",
                               "version", "compatibility"],
                    "selectHosts": ["hostid"],
                    "sortfield": "name",
                })

                if not data:
                    return "No proxies found."

                lines = []
                for p in data:
                    mode = PROXY_MODE.get(
                        p.get("operating_mode", ""), p.get("operating_mode", "?")
                    )
                    host_count = len(p.get("hosts", []))
                    last = _ts(p.get("lastaccess", "0"))
                    compat = format_proxy_compat(
                        p.get("compatibility", "0"), p.get("version", "0")
                    )
                    tls = ""
                    if p.get("tls_connect", "1") != "1":
                        tls = " [TLS]"
                    desc = ""
                    if p.get("description"):
                        desc = f"\n  {p['description'][:100]}"
                    lines.append(
                        f"- **{p.get('name', '?')}**{compat} ({mode}, {host_count} hosts){tls}\n"
                        f"  Last seen: {last} | "
                        f"proxyid: {p.get('proxyid', '?')}{desc}"
                    )

                return f"**Found: {len(data)} proxies**\n\n" + "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error querying Zabbix: {e}"
