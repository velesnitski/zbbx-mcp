"""Infrastructure analysis: server role analysis, log correlation, IP audit, external IP classification."""

from __future__ import annotations

import ipaddress
import json

import httpx

from zbbx_mcp.classify import classify_host as _classify_host
from zbbx_mcp.classify import detect_provider, resolve_datacenter
from zbbx_mcp.data import extract_country, fetch_enabled_hosts, host_ip
from zbbx_mcp.resolver import InstanceResolver
from zbbx_mcp.utils import resolve_group_ids

_PHYSICAL_PREFIXES = ("eth", "eno", "enp", "ens", "bond", "ppp")
_KNOWN_SYSTEM = ("lo", "docker", "br-", "veth", "virbr")


def _is_tunnel(iface: str) -> bool:
    """Detect tunnel interfaces by exclusion: not physical and not system."""
    return not _is_physical(iface) and not any(iface.startswith(p) for p in _KNOWN_SYSTEM)


def _is_physical(iface: str) -> bool:
    return any(iface.startswith(p) for p in _PHYSICAL_PREFIXES)


def _iface_from_key(key: str) -> str:
    """Extract interface name from item key like net.if.in[eth0]."""
    if "[" in key:
        return key.split("[")[1].rstrip("]")
    return ""


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()) -> None:

    if "analyze_server_roles" not in skip:

        @mcp.tool()
        async def analyze_server_roles(
            country: str = "",
            product: str = "",
            server_type: str = "",
            max_results: int = 50,
            instance: str = "",
        ) -> str:
            """Classify servers as relay, endpoint, mixed, or idle based on interface traffic.

            Args:
                country: Filter by 2-letter country code
                product: Filter by product name
                server_type: Filter: relay, endpoint, mixed, idle
                max_results: Max results (default: 50)
                instance: Zabbix instance (optional)
            """
            try:
                client = resolver.resolve(instance)
                hosts = await fetch_enabled_hosts(client, extra_output=["name"])

                hostids = [h["hostid"] for h in hosts]
                items = await client.call("item.get", {
                    "hostids": hostids,
                    "output": ["hostid", "lastvalue", "key_"],
                    "search": {"key_": "net.if.in["},
                    "searchWildcardsEnabled": True,
                    "filter": {"status": "0"},
                })

                # Build per-host traffic maps
                host_phys: dict[str, float] = {}
                host_tunnel: dict[str, float] = {}
                for it in items:
                    try:
                        iface = _iface_from_key(it.get("key_", ""))
                        if not iface or iface in ("lo", "docker0", "unbound_tun1", "unbound_tun2"):
                            continue
                        mbps = float(it.get("lastvalue", 0)) / 1_000_000
                        hid = it["hostid"]
                        if _is_physical(iface):
                            host_phys[hid] = max(host_phys.get(hid, 0), mbps)
                        elif _is_tunnel(iface):
                            host_tunnel[hid] = host_tunnel.get(hid, 0) + mbps
                    except (ValueError, TypeError):
                        pass

                min_traffic = 0.1  # Mbps threshold
                rows = []
                for h in hosts:
                    hid = h["hostid"]
                    phys = host_phys.get(hid, 0)
                    tun = host_tunnel.get(hid, 0)

                    if phys >= min_traffic and tun < min_traffic:
                        stype = "relay"
                    elif tun >= min_traffic and phys < min_traffic:
                        stype = "endpoint"
                    elif phys >= min_traffic and tun >= min_traffic:
                        stype = "mixed"
                    else:
                        stype = "idle" if phys < 0.01 and tun < 0.01 else "endpoint" if tun >= phys else "relay"

                    if server_type and stype != server_type.lower():
                        continue
                    if country and extract_country(h["host"]).lower() != country.lower():
                        continue
                    if product:
                        p, _ = _classify_host(h.get("groups", []))
                        if product.lower() not in (p or "").lower():
                            continue

                    ip = host_ip(h)
                    prov = detect_provider(ip) if ip else "?"
                    cc = extract_country(h["host"])
                    rows.append((h["host"], stype, phys, tun, prov, cc))

                rows.sort(key=lambda r: (-r[2] - r[3], r[0]))

                counts = {}
                for r in rows:
                    counts[r[1]] = counts.get(r[1], 0) + 1

                summary = ", ".join(f"{t}: {c}" for t, c in sorted(counts.items()))
                parts = [f"Server Classification ({len(rows)} hosts): {summary}\n"]
                parts.append("| Server | Type | Main Mbps | Tunnel Mbps | Provider | Country |")
                parts.append("|--------|------|-----------|-------------|----------|---------|")
                for r in rows[:max_results]:
                    parts.append(
                        f"| {r[0]} | {r[1]} | {r[2]:.1f} | {r[3]:.1f} | {r[4]} | {r[5]} |"
                    )
                if len(rows) > max_results:
                    parts.append(f"\n*{len(rows) - max_results} more hosts omitted*")

                return "\n".join(parts)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

    if "correlate_logs" not in skip:

        @mcp.tool()
        async def correlate_logs(
            log_data: str,
            max_lines: int = 1000,
            instance: str = "",
        ) -> str:
            """Correlate JSON server logs with Zabbix host data. Detects IP mismatches.

            Args:
                log_data: JSON lines (each with host_id, r_ip, d_ip fields)
                max_lines: Max log lines to parse (default: 1000)
                instance: Zabbix instance (optional)
            """
            try:
                client = resolver.resolve(instance)

                # Parse log lines
                host_ips: dict[str, set[str]] = {}
                host_dips: dict[str, set[str]] = {}
                host_events: dict[str, int] = {}
                parse_errors = 0
                for i, line in enumerate(log_data.strip().split("\n")):
                    if i >= max_lines:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        hid = entry.get("host_id", "")
                        rip = entry.get("r_ip", "")
                        dip = entry.get("d_ip", "")
                        if hid:
                            host_ips.setdefault(hid, set())
                            host_events[hid] = host_events.get(hid, 0) + 1
                            if rip:
                                host_ips[hid].add(rip)
                            if dip:
                                host_dips.setdefault(hid, set()).add(dip)
                    except (json.JSONDecodeError, TypeError):
                        parse_errors += 1

                if not host_ips:
                    return "No valid log entries found."

                # Fetch matching hosts from Zabbix
                all_hosts = await fetch_enabled_hosts(client, extra_output=["name"])
                zabbix_map: dict[str, dict] = {h["host"]: h for h in all_hosts}

                parts = [f"Log Correlation: {len(host_ips)} hosts, {sum(host_events.values())} events\n"]
                parts.append("| Log Host | Zabbix | Log IP | Zabbix IP | Match | Events |")
                parts.append("|----------|--------|--------|-----------|-------|--------|")

                mismatches = 0
                not_found = 0
                for hid in sorted(host_ips.keys()):
                    log_rips = sorted(host_ips[hid])
                    log_ip_str = ", ".join(log_rips[:3])
                    events = host_events[hid]

                    zh = zabbix_map.get(hid)
                    if not zh:
                        parts.append(f"| {hid} | NOT FOUND | {log_ip_str} | — | — | {events} |")
                        not_found += 1
                        continue

                    zabbix_ip = host_ip(zh)
                    # Check if any log IP matches Zabbix IP or is in same /24
                    match = "✓"
                    if zabbix_ip and log_rips:
                        try:
                            z_net = ipaddress.ip_network(f"{zabbix_ip}/24", strict=False)
                            if zabbix_ip in log_rips:
                                match = "✓ exact"
                            elif any(ipaddress.ip_address(ip) in z_net for ip in log_rips):
                                match = "~ /24"
                                mismatches += 1
                            else:
                                match = "✗ MISMATCH"
                                mismatches += 1
                        except ValueError:
                            match = "?"

                    parts.append(
                        f"| {hid} | {zh['host']} | {log_ip_str} | {zabbix_ip} | {match} | {events} |"
                    )

                summary = []
                if mismatches:
                    summary.append(f"**{mismatches} IP mismatch(es)**")
                if not_found:
                    summary.append(f"**{not_found} host(s) not in Zabbix**")
                if parse_errors:
                    summary.append(f"{parse_errors} unparseable lines skipped")
                if summary:
                    parts.append("\n" + " | ".join(summary))

                return "\n".join(parts)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

    if "audit_host_ips" not in skip:

        @mcp.tool()
        async def audit_host_ips(
            group: str = "",
            product: str = "",
            country: str = "",
            max_results: int = 50,
            instance: str = "",
        ) -> str:
            """IP consistency audit — find hosts with interfaces in different /24 subnets.

            Args:
                group: Filter by host group name
                product: Filter by product name
                country: Filter by 2-letter country code
                max_results: Max results (default: 50)
                instance: Zabbix instance (optional)
            """
            try:
                client = resolver.resolve(instance)

                params: dict = {
                    "output": ["hostid", "host", "name"],
                    "selectInterfaces": ["ip", "type", "main"],
                    "selectGroups": ["name"],
                    "filter": {"status": "0"},
                    "sortfield": "host",
                }
                if group:
                    gids = await resolve_group_ids(client, group)
                    if gids is None:
                        return f"Host group '{group}' not found."
                    params["groupids"] = gids

                hosts = await client.call("host.get", params)

                rows = []
                for h in hosts:
                    if country and extract_country(h["host"]).lower() != country.lower():
                        continue
                    if product:
                        p, _ = _classify_host(h.get("groups", []))
                        if product.lower() not in (p or "").lower():
                            continue

                    ips = sorted({
                        i["ip"] for i in h.get("interfaces", [])
                        if i.get("ip") and i["ip"] != "127.0.0.1"
                    })
                    if len(ips) < 2:
                        continue

                    subnets = set()
                    providers = set()
                    for ip in ips:
                        try:
                            subnets.add(str(ipaddress.ip_network(f"{ip}/24", strict=False)))
                        except ValueError:
                            pass
                        providers.add(detect_provider(ip))

                    flag = ""
                    if len(subnets) > 1:
                        flag = "MULTI-SUBNET"
                    if len(providers) > 1:
                        flag = "MULTI-PROVIDER" if not flag else f"{flag} + MULTI-PROVIDER"

                    if flag:
                        rows.append((h["host"], ", ".join(ips), len(subnets), ", ".join(sorted(providers)), flag))

                if not rows:
                    return "No IP mismatches found."

                parts = [f"IP Mismatches: {len(rows)} hosts with multi-subnet interfaces\n"]
                parts.append("| Server | IPs | Subnets | Provider(s) | Flag |")
                parts.append("|--------|-----|---------|-------------|------|")
                for r in rows[:max_results]:
                    parts.append(f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} | {r[4]} |")
                if len(rows) > max_results:
                    parts.append(f"\n*{len(rows) - max_results} more hosts omitted*")

                return "\n".join(parts)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

    if "classify_external_ips" not in skip:

        @mcp.tool()
        async def classify_external_ips(
            input_data: str,
            max_ips: int = 500,
            instance: str = "",
        ) -> str:
            """Classify external IPs by hosting provider. Accepts IPs or JSON logs.

            Args:
                input_data: Comma-separated IPs or JSON log lines (auto-detected)
                max_ips: Max unique IPs to process (default: 500)
                instance: Zabbix instance (optional)
            """
            try:
                # Auto-detect input format
                ips: set[str] = set()
                first_line = input_data.strip().split("\n")[0].strip()
                is_json = first_line.startswith("{")

                if is_json:
                    for line in input_data.strip().split("\n"):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                            dip = entry.get("d_ip", "")
                            if dip:
                                ips.add(dip)
                        except (json.JSONDecodeError, TypeError):
                            pass
                else:
                    for part in input_data.replace("\n", ",").split(","):
                        ip = part.strip()
                        if ip:
                            try:
                                ipaddress.ip_address(ip)
                                ips.add(ip)
                            except ValueError:
                                pass

                if not ips:
                    return "No valid IPs found in input."

                ip_list = sorted(ips)[:max_ips]

                # Classify each IP
                provider_data: dict[str, dict] = {}
                for ip in ip_list:
                    prov, city = resolve_datacenter(ip)
                    key = f"{prov}|{city}" if city else prov
                    if key not in provider_data:
                        provider_data[key] = {"provider": prov, "city": city, "count": 0, "samples": []}
                    provider_data[key]["count"] += 1
                    if len(provider_data[key]["samples"]) < 3:
                        provider_data[key]["samples"].append(ip)

                total = len(ip_list)
                sorted_provs = sorted(provider_data.values(), key=lambda x: -x["count"])

                parts = [f"External IP Distribution: {total} unique IPs, {len(provider_data)} providers\n"]
                parts.append("| Provider | City | Count | % | Sample IPs |")
                parts.append("|----------|------|-------|---|------------|")
                for pd in sorted_provs:
                    pct = pd["count"] / total * 100
                    samples = ", ".join(pd["samples"])
                    city = pd["city"] or "—"
                    parts.append(
                        f"| {pd['provider']} | {city} | {pd['count']} | {pct:.0f}% | {samples} |"
                    )

                if len(ips) > max_ips:
                    parts.append(f"\n*{len(ips) - max_ips} IPs over limit, not processed*")

                return "\n".join(parts)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"
