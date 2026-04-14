"""Domain monitoring: SSL certs, WHOIS expiry, HTTPS uptime."""

from __future__ import annotations

import httpx

from zbbx_mcp.resolver import InstanceResolver

# Item keys for domain health checks (configurable via env in future)
_KEY_CERT = "web_cert_check.sh[{HOST.NAME}]"
_KEY_WHOIS = "web_registration_check.sh[{HOST.NAME}]"
_KEY_HTTPS = "web_https_check.sh[{HOST.NAME}]"


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()) -> None:

    if "get_domain_status" not in skip:

        @mcp.tool()
        async def get_domain_status(
            search: str = "",
            only_problems: bool = False,
            max_results: int = 50,
            instance: str = "",
        ) -> str:
            """Domain health dashboard — SSL, WHOIS, HTTPS status for all monitored domains.

            Args:
                search: Filter domains by name (optional)
                only_problems: Show only domains with issues (default: False)
                max_results: Maximum results (default: 50)
                instance: Zabbix instance name (optional)
            """
            try:
                client = resolver.resolve(instance)

                # Find hosts that have domain check items
                items = await client.call("item.get", {
                    "output": ["itemid", "hostid", "key_", "lastvalue", "lastclock"],
                    "filter": {"key_": [_KEY_CERT, _KEY_WHOIS, _KEY_HTTPS], "status": "0"},
                    "selectHosts": ["hostid", "host"],
                    "sortfield": "key_",
                })

                if not items:
                    return "No domain monitoring items found."

                # Group by host
                domains: dict[str, dict] = {}
                for it in items:
                    host = it["hosts"][0] if it.get("hosts") else {}
                    hostname = host.get("host", "?")
                    if search and search.lower() not in hostname.lower():
                        continue
                    d = domains.setdefault(hostname, {"cert": None, "whois": None, "https": None})
                    key = it.get("key_", "")
                    val = it.get("lastvalue", "")
                    try:
                        v = int(float(val))
                    except (ValueError, TypeError):
                        v = -1
                    if "cert_check" in key:
                        d["cert"] = v
                    elif "registration_check" in key:
                        d["whois"] = v
                    elif "https_check" in key:
                        d["https"] = v

                if not domains:
                    return "No domains match the filter."

                # Build output
                lines = []
                problems = 0
                for name in sorted(domains):
                    d = domains[name]
                    cert_ok = d["cert"] == 1
                    whois_ok = d["whois"] == 1
                    https_ok = d["https"] == 1
                    has_problem = not (cert_ok and whois_ok and https_ok)
                    if only_problems and not has_problem:
                        continue
                    if has_problem:
                        problems += 1

                    cert_s = "OK" if cert_ok else ("EXPIRED" if d["cert"] == 0 else "N/A")
                    whois_s = "OK" if whois_ok else ("EXPIRED" if d["whois"] == 0 else "N/A")
                    https_s = "UP" if https_ok else ("DOWN" if d["https"] == 0 else "N/A")

                    lines.append(f"| {name} | {cert_s} | {whois_s} | {https_s} |")

                shown = lines[:max_results]
                total = len(domains)
                header = f"**Domain Status: {total} domains"
                if problems:
                    header += f", {problems} with issues"
                header += "**\n"

                result = header + "\n".join([
                    "| Domain | SSL Cert | WHOIS | HTTPS |",
                    "|--------|---------|-------|-------|",
                ] + shown)
                if len(lines) > max_results:
                    result += f"\n\n*{len(lines) - max_results} more omitted*"
                return result
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

    if "get_ssl_expiry" not in skip:

        @mcp.tool()
        async def get_ssl_expiry(
            only_problems: bool = True,
            max_results: int = 30,
            instance: str = "",
        ) -> str:
            """SSL certificate status for all monitored domains.

            Args:
                only_problems: Show only expired/failing certs (default: True)
                max_results: Maximum results (default: 30)
                instance: Zabbix instance name (optional)
            """
            try:
                client = resolver.resolve(instance)
                items = await client.call("item.get", {
                    "output": ["itemid", "hostid", "lastvalue", "lastclock"],
                    "filter": {"key_": _KEY_CERT, "status": "0"},
                    "selectHosts": ["hostid", "host"],
                })

                if not items:
                    return "No SSL cert check items found."

                ok = 0
                problems = []
                for it in items:
                    hostname = it["hosts"][0]["host"] if it.get("hosts") else "?"
                    try:
                        val = int(float(it.get("lastvalue", "-1")))
                    except (ValueError, TypeError):
                        val = -1
                    if val == 1:
                        ok += 1
                        if not only_problems:
                            problems.append((hostname, "OK"))
                    else:
                        problems.append((hostname, "EXPIRED" if val == 0 else "ERROR"))

                lines = [f"**SSL Certs: {ok} OK, {len(items) - ok} issues**\n"]
                lines.append("| Domain | Status |")
                lines.append("|--------|--------|")
                for name, status in sorted(problems)[:max_results]:
                    lines.append(f"| {name} | {status} |")
                return "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

    if "get_domain_list" not in skip:

        @mcp.tool()
        async def get_domain_list(
            group: str = "",
            max_results: int = 100,
            instance: str = "",
        ) -> str:
            """Export all monitored domains from Zabbix.

            Args:
                group: Filter by host group (optional)
                max_results: Maximum results (default: 100)
                instance: Zabbix instance name (optional)
            """
            try:
                client = resolver.resolve(instance)

                # Find all hosts that have any domain check item
                items = await client.call("item.get", {
                    "output": ["hostid"],
                    "filter": {"key_": [_KEY_CERT, _KEY_WHOIS, _KEY_HTTPS], "status": "0"},
                })
                if not items:
                    return "No domain monitoring items found."

                hostids = list({it["hostid"] for it in items})

                # Get host details
                params = {
                    "hostids": hostids,
                    "output": ["hostid", "host", "name", "status"],
                    "selectGroups": ["name"],
                    "sortfield": "host",
                }
                hosts = await client.call("host.get", params)

                if group:
                    hosts = [h for h in hosts if any(group.lower() in g["name"].lower() for g in h.get("groups", []))]

                if not hosts:
                    return "No domains match the filter."

                lines = [f"**{len(hosts)} monitored domains**\n"]
                for h in hosts[:max_results]:
                    status = "enabled" if h.get("status") == "0" else "disabled"
                    groups = ", ".join(g["name"] for g in h.get("groups", []))
                    lines.append(f"- {h['host']} [{status}] ({groups})")

                if len(hosts) > max_results:
                    lines.append(f"\n*{len(hosts) - max_results} more omitted*")
                return "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"
