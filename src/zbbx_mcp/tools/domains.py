"""Domain monitoring: SSL certs, WHOIS expiry, HTTPS uptime."""

from __future__ import annotations

import socket
import ssl
import time as _time
import urllib.request
from datetime import datetime, timezone

import httpx

from zbbx_mcp.classify import detect_provider
from zbbx_mcp.resolver import InstanceResolver


def _resolve_domain(domain: str) -> tuple[str, str]:
    """Resolve domain to IP and detect provider. Returns (ip, provider)."""
    try:
        ip = socket.gethostbyname(domain)
        return ip, detect_provider(ip)
    except (socket.gaierror, OSError):
        return "", ""


def _get_ssl_expiry(domain: str) -> tuple[int, str]:
    """Get SSL certificate days until expiry and issuer. Returns (days, issuer)."""
    try:
        ctx = ssl.create_default_context()
        with ctx.wrap_socket(socket.socket(), server_hostname=domain) as conn:
            conn.settimeout(5)
            conn.connect((domain, 443))
            cert = conn.getpeercert()
        expiry = datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        days = (expiry - datetime.now(timezone.utc)).days
        issuer_parts = dict(x[0] for x in cert.get("issuer", ()))
        issuer = issuer_parts.get("organizationName", issuer_parts.get("commonName", "?"))
        return days, issuer
    except Exception:
        return -1, ""


def _get_response_time(domain: str) -> int:
    """HTTPS response time in ms. Returns -1 on failure."""
    try:
        start = _time.monotonic()
        urllib.request.urlopen(f"https://{domain}", timeout=5)  # noqa: S310
        return round((_time.monotonic() - start) * 1000)
    except Exception:
        return -1

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
            format: str = "table",
            max_results: int = 50,
            instance: str = "",
        ) -> str:
            """Domain health dashboard — SSL, WHOIS, HTTPS status for all monitored domains.

            Args:
                search: Filter domains by name (optional)
                only_problems: Show only domains with issues (default: False)
                format: Output format: 'table' or 'csv' (default: table)
                max_results: Maximum results (default: 50)
                instance: Zabbix instance name (optional)
            """
            try:
                client = resolver.resolve(instance)

                # Find hosts that have domain check items
                items = await client.call("item.get", {
                    "output": ["itemid", "hostid", "key_", "lastvalue", "lastclock"],
                    "filter": {"key_": [_KEY_CERT, _KEY_WHOIS, _KEY_HTTPS], "status": "0"},
                    "selectHosts": ["hostid", "host", "groups"],
                    "sortfield": "key_",
                })

                if not items:
                    return "No domain monitoring items found."

                # Group by host (skip non-domain hostnames)
                domains: dict[str, dict] = {}
                for it in items:
                    host = it["hosts"][0] if it.get("hosts") else {}
                    hostname = host.get("host", "?")
                    if "." not in hostname:
                        continue  # not a real domain name
                    if search and search.lower() not in hostname.lower():
                        continue
                    groups = ", ".join(g.get("name", "") for g in host.get("groups", []))
                    d = domains.setdefault(hostname, {"cert": None, "whois": None, "https": None, "groups": groups})
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

                # Build output — resolve IP, SSL expiry, response time in parallel
                import asyncio
                loop = asyncio.get_event_loop()

                rows = []
                problems = 0
                domain_names = sorted(domains)

                # Batch all DNS/SSL/HTTP lookups via thread pool
                async def _enrich(name: str):
                    ip, provider = await loop.run_in_executor(None, _resolve_domain, name)
                    ssl_days, ssl_issuer = await loop.run_in_executor(None, _get_ssl_expiry, name)
                    resp_ms = await loop.run_in_executor(None, _get_response_time, name)
                    return name, ip, provider, ssl_days, ssl_issuer, resp_ms

                enriched = await asyncio.gather(*[_enrich(n) for n in domain_names])
                enrich_map = {e[0]: e[1:] for e in enriched}

                for name in domain_names:
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

                    ip, provider, ssl_days, ssl_issuer, resp_ms = enrich_map.get(name, ("", "", -1, "", -1))
                    ssl_days_s = str(ssl_days) if ssl_days >= 0 else "N/A"
                    resp_ms_s = f"{resp_ms}" if resp_ms >= 0 else "N/A"
                    grp = d.get("groups", "")

                    rows.append((name, cert_s, whois_s, https_s, ip, provider, ssl_days_s, ssl_issuer, resp_ms_s, grp))

                shown = rows[:max_results]
                total = len(domains)

                if format == "csv":
                    import os
                    output_dir = os.path.expanduser("~/Downloads")
                    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                    filepath = os.path.join(output_dir, f"domains-{date_str}.csv")
                    with open(filepath, "w") as f:
                        f.write("Domain,SSL Cert,WHOIS,HTTPS,IP,Provider,SSL Days Left,SSL Issuer,Response ms,Group\n")
                        for r in shown:
                            f.write(",".join(r) + "\n")
                    return f"**Exported {len(shown)} domains to `{filepath}`**\n{problems} with issues" if problems else f"**Exported {len(shown)} domains to `{filepath}`**\nAll healthy"

                header = f"**Domain Status: {total} domains"
                if problems:
                    header += f", {problems} with issues"
                header += "**\n"

                lines = []
                for name, cert, whois, https, ip, prov, ssl_d, _ssl_i, resp, _g in shown:
                    lines.append(f"| {name} | {cert} | {whois} | {https} | {ip} | {prov} | {ssl_d}d | {resp}ms |")
                result = header + "\n".join([
                    "| Domain | SSL | WHOIS | HTTPS | IP | Provider | SSL Expiry | Response |",
                    "|--------|-----|-------|-------|----|---------:|-----------|---------|",
                ] + lines)
                if len(rows) > max_results:
                    result += f"\n\n*{len(rows) - max_results} more omitted*"
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
