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


def _probe_domain(domain: str) -> dict:
    """Full domain probe: DNS, SSL, HTTP in one pass. Returns enrichment dict."""
    result = {
        "ip": "", "all_ips": "", "ipv6": "no", "provider": "",
        "ssl_days": -1, "ssl_issuer": "", "ssl_valid_from": "", "ssl_key": "", "ssl_sans": 0,
        "http_status": -1, "http_server": "", "http_redirect": "", "hsts": "no",
        "resp_ms": -1,
    }

    # --- DNS ---
    try:
        ips = sorted(set(socket.gethostbyname_ex(domain)[2]))
        result["ip"] = ips[0] if ips else ""
        result["all_ips"] = "; ".join(ips)
        result["provider"] = detect_provider(ips[0]) if ips else ""
    except (socket.gaierror, OSError):
        pass

    try:
        socket.getaddrinfo(domain, 443, socket.AF_INET6)
        result["ipv6"] = "yes"
    except (socket.gaierror, OSError):
        pass

    # --- SSL ---
    try:
        ctx = ssl.create_default_context()
        with ctx.wrap_socket(socket.socket(), server_hostname=domain) as conn:
            conn.settimeout(5)
            conn.connect((domain, 443))
            cert = conn.getpeercert()
            cipher = conn.cipher()

        expiry = datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        valid_from = datetime.strptime(cert["notBefore"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        result["ssl_days"] = (expiry - datetime.now(timezone.utc)).days
        result["ssl_valid_from"] = valid_from.strftime("%Y-%m-%d")

        issuer_parts = dict(x[0] for x in cert.get("issuer", ()))
        result["ssl_issuer"] = issuer_parts.get("organizationName", issuer_parts.get("commonName", "?"))
        result["ssl_key"] = cipher[0] if cipher else ""

        sans = cert.get("subjectAltName", ())
        result["ssl_sans"] = len(sans)
    except Exception:
        pass

    # --- HTTP ---
    try:
        start = _time.monotonic()
        req = urllib.request.Request(f"https://{domain}", method="HEAD")
        resp = urllib.request.urlopen(req, timeout=5)  # noqa: S310
        result["resp_ms"] = round((_time.monotonic() - start) * 1000)
        result["http_status"] = resp.status
        result["http_server"] = resp.headers.get("Server", "")[:30]
        result["hsts"] = "yes" if resp.headers.get("Strict-Transport-Security") else "no"
        if resp.status in (301, 302, 307, 308):
            result["http_redirect"] = resp.headers.get("Location", "")[:60]
    except urllib.error.HTTPError as e:
        result["http_status"] = e.code
        result["resp_ms"] = round((_time.monotonic() - start) * 1000)
        result["http_server"] = e.headers.get("Server", "")[:30] if e.headers else ""
    except Exception:
        pass

    return result

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

                # Probe all domains in parallel via thread pool
                import asyncio
                loop = asyncio.get_event_loop()

                domain_names = sorted(domains)
                probes = await asyncio.gather(
                    *[loop.run_in_executor(None, _probe_domain, n) for n in domain_names]
                )
                probe_map = dict(zip(domain_names, probes, strict=True))

                rows = []
                problems = 0
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

                    p = probe_map.get(name, {})
                    rows.append({
                        "domain": name,
                        "cert": cert_s, "whois": whois_s, "https": https_s,
                        "ip": p.get("ip", ""), "all_ips": p.get("all_ips", ""),
                        "ipv6": p.get("ipv6", "no"), "provider": p.get("provider", ""),
                        "ssl_days": str(p["ssl_days"]) if p.get("ssl_days", -1) >= 0 else "N/A",
                        "ssl_issuer": p.get("ssl_issuer", ""),
                        "ssl_valid_from": p.get("ssl_valid_from", ""),
                        "ssl_key": p.get("ssl_key", ""),
                        "ssl_sans": str(p.get("ssl_sans", 0)),
                        "http_status": str(p["http_status"]) if p.get("http_status", -1) > 0 else "N/A",
                        "http_server": p.get("http_server", ""),
                        "http_redirect": p.get("http_redirect", ""),
                        "hsts": p.get("hsts", "no"),
                        "resp_ms": str(p["resp_ms"]) if p.get("resp_ms", -1) >= 0 else "N/A",
                        "group": d.get("groups", ""),
                    })

                shown = rows[:max_results]
                total = len(domains)

                _CSV_FIELDS = [
                    "domain", "cert", "whois", "https", "ip", "all_ips", "ipv6",
                    "provider", "ssl_days", "ssl_issuer", "ssl_valid_from", "ssl_key",
                    "ssl_sans", "http_status", "http_server", "http_redirect", "hsts",
                    "resp_ms", "group",
                ]
                _CSV_HEADERS = [
                    "Domain", "SSL Cert", "WHOIS", "HTTPS", "IP", "All IPs", "IPv6",
                    "Provider", "SSL Days Left", "SSL Issuer", "SSL Valid From", "SSL Cipher",
                    "SSL SANs", "HTTP Status", "Server", "Redirect", "HSTS",
                    "Response ms", "Group",
                ]

                if format == "csv":
                    import os
                    output_dir = os.path.expanduser("~/Downloads")
                    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                    filepath = os.path.join(output_dir, f"domains-{date_str}.csv")
                    with open(filepath, "w") as f:
                        f.write(",".join(_CSV_HEADERS) + "\n")
                        for r in shown:
                            vals = [r.get(k, "").replace(",", ";") for k in _CSV_FIELDS]
                            f.write(",".join(vals) + "\n")
                    return f"**Exported {len(shown)} domains ({len(_CSV_FIELDS)} fields) to `{filepath}`**\n{problems} with issues" if problems else f"**Exported {len(shown)} domains ({len(_CSV_FIELDS)} fields) to `{filepath}`**\nAll healthy"

                # Table view — compact (key fields only)
                header = f"**Domain Status: {total} domains"
                if problems:
                    header += f", {problems} with issues"
                header += "**\n"

                lines = []
                for r in shown:
                    lines.append(
                        f"| {r['domain']} | {r['cert']} | {r['https']} | {r['ip']} | {r['provider']} | "
                        f"{r['ssl_days']}d | {r['http_status']} | {r['http_server']} | {r['hsts']} | {r['resp_ms']}ms |"
                    )
                result = header + "\n".join([
                    "| Domain | SSL | HTTPS | IP | Provider | Expiry | Status | Server | HSTS | Resp |",
                    "|--------|-----|-------|----|---------:|--------|--------|--------|------|-----:|",
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
