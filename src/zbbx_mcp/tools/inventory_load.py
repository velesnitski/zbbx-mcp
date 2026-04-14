"""Inventory load tools: server load analysis, CPU thresholds, provider identification."""

import asyncio
import socket

import httpx

from zbbx_mcp.classify import (
    classify_host as _classify_host,
)
from zbbx_mcp.classify import (
    detect_provider,
)
from zbbx_mcp.data import TRAFFIC_IN_KEYS, build_parent_map, extract_country
from zbbx_mcp.resolver import InstanceResolver
from zbbx_mcp.tools.items import _format_value


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()):

    if "get_server_load" not in skip:

        @mcp.tool()
        async def get_server_load(
            product: str = "",
            tier: str = "",
            country: str = "",
            sort_by: str = "cpu",
            max_results: int = 50,
            instance: str = "",
        ) -> str:
            """Server load (CPU, memory, network) sorted by utilization.

            Args:
                product: Filter by product name (optional)
                tier: Filter by tier (e.g., 'Free', 'Premium') (optional)
                country: Filter by country code in hostname (optional)
                sort_by: Sort by 'cpu' (CPU usage), 'load' (load avg), or 'traffic' (network) (default: cpu)
                max_results: Maximum results (default: 50)
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)

                # Get all enabled hosts with groups and interfaces
                hosts = await client.call("host.get", {
                    "output": ["hostid", "host", "name", "status"],
                    "selectGroups": ["name"],
                    "selectInterfaces": ["ip"],
                    "filter": {"status": "0"},
                    "sortfield": "host",
                })

                # Filter by product/tier/country
                filtered = []
                for h in hosts:
                    prod, t = _classify_host(h.get("groups", []))
                    if not prod:
                        continue
                    if product and product.lower() not in prod.lower():
                        continue
                    if tier and tier.lower() not in t.lower():
                        continue
                    if country and extract_country(h.get("host", "")).lower() != country.lower():
                        continue
                    h["_product"] = prod
                    h["_tier"] = t
                    filtered.append(h)

                if not filtered:
                    return "No servers match the filters."

                # Resolve parent hosts for metric inheritance
                p_map = build_parent_map(hosts)
                hostids = [h["hostid"] for h in filtered]
                parent_ids = list({p_map[hid] for hid in hostids if hid in p_map} - set(hostids))
                lookup_ids = hostids + parent_ids

                # Batch-fetch CPU/load/mem + traffic in parallel
                items, net_items = await asyncio.gather(
                    client.call("item.get", {
                        "hostids": lookup_ids,
                        "output": ["hostid", "itemid", "key_", "lastvalue", "units"],
                        "filter": {"key_": [
                            "system.cpu.util[,idle]",
                            "system.cpu.load[percpu,avg1]",
                            "vm.memory.size[available]",
                        ]},
                        "sortfield": "key_",
                    }),
                    client.call("item.get", {
                        "hostids": lookup_ids,
                        "output": ["hostid", "key_", "lastvalue", "units"],
                        "filter": {"key_": TRAFFIC_IN_KEYS, "status": "0"},
                    }),
                )

                # Build per-host metrics
                metrics: dict[str, dict] = {}
                for item in items:
                    hid = item["hostid"]
                    m = metrics.setdefault(hid, {})
                    key = item["key_"]
                    try:
                        val = float(item.get("lastvalue", "0"))
                    except (ValueError, TypeError):
                        val = 0
                    if "idle" in key:
                        m["cpu_used"] = round(100 - val, 1)
                    elif "load" in key:
                        m["load_avg1"] = round(val, 2)
                    elif "memory" in key:
                        m["mem_avail_gb"] = round(val / 1_073_741_824, 1)

                for item in net_items:
                    hid = item["hostid"]
                    m = metrics.setdefault(hid, {})
                    try:
                        val = float(item.get("lastvalue", "0"))
                    except (ValueError, TypeError):
                        val = 0
                    # Keep highest traffic interface
                    current = m.get("traffic_bps", 0)
                    if val > current:
                        m["traffic_bps"] = val

                # Inherit parent metrics for child hosts missing own data
                for h in filtered:
                    hid = h["hostid"]
                    pid = p_map.get(hid)
                    if pid and hid not in metrics and pid in metrics:
                        metrics[hid] = dict(metrics[pid])
                    elif pid and pid in metrics:
                        parent_m = metrics[pid]
                        child_m = metrics.setdefault(hid, {})
                        for k, v in parent_m.items():
                            if k not in child_m:
                                child_m[k] = v

                # Sort
                def sort_key(h):
                    m = metrics.get(h["hostid"], {})
                    if sort_by == "load":
                        return -(m.get("load_avg1", 0))
                    elif sort_by == "traffic":
                        return -(m.get("traffic_bps", 0))
                    return -(m.get("cpu_used", 0))

                filtered.sort(key=sort_key)
                filtered = filtered[:max_results]


                parts = [
                    "| Server | Country | Product | Tier | IP | Provider | CPU% | Load | Mem Avail | Traffic In |",
                    "|--------|---------|---------|------|-----|----------|------|------|-----------|------------|",
                ]

                for h in filtered:
                    m = metrics.get(h["hostid"], {})
                    ip = ""
                    for iface in h.get("interfaces", []):
                        if iface.get("ip") and iface["ip"] != "127.0.0.1":
                            ip = iface["ip"]
                            break

                    provider = detect_provider(ip) if ip else ""
                    ctry = extract_country(h.get("host", ""))
                    cpu = f"{m.get('cpu_used', 'N/A')}%" if "cpu_used" in m else "N/A"
                    load = str(m.get("load_avg1", "N/A"))
                    mem = f"{m.get('mem_avail_gb', 'N/A')} GB" if "mem_avail_gb" in m else "N/A"
                    traffic = _format_value(str(m.get("traffic_bps", "")), "bps") if "traffic_bps" in m else "N/A"

                    parts.append(
                        f"| {h.get('host', '?')} | {ctry} | {h['_product']} | {h['_tier']} | "
                        f"{ip} | {provider} | {cpu} | {load} | {mem} | {traffic} |"
                    )

                header = f"**Server Load ({len(filtered)} servers, sorted by {sort_by})**\n\n"
                return header + "\n".join(parts)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

    if "get_high_cpu_servers" not in skip:

        @mcp.tool()
        async def get_high_cpu_servers(
            threshold: float = 80.0,
            product: str = "",
            country: str = "",
            instance: str = "",
        ) -> str:
            """Find servers with CPU usage above a threshold.

            Args:
                threshold: CPU usage percentage threshold (default: 80%)
                product: Filter by product name (optional)
                country: Filter by country code in hostname (optional)
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)

                # Get all enabled hosts
                hosts = await client.call("host.get", {
                    "output": ["hostid", "host", "status"],
                    "selectGroups": ["name"],
                    "selectInterfaces": ["ip"],
                    "filter": {"status": "0"},
                })

                # Filter by product if specified
                host_map = {}
                for h in hosts:
                    prod, tier = _classify_host(h.get("groups", []))
                    if not prod:
                        continue
                    if product and product.lower() not in prod.lower():
                        continue
                    if country and extract_country(h.get("host", "")).lower() != country.lower():
                        continue
                    h["_product"] = prod
                    h["_tier"] = tier
                    host_map[h["hostid"]] = h

                if not host_map:
                    return "No servers match the filter."

                p_map = build_parent_map(hosts)
                hids = list(host_map.keys())
                parent_ids = list({p_map[h] for h in hids if h in p_map} - set(hids))

                items = await client.call("item.get", {
                    "hostids": hids + parent_ids,
                    "output": ["hostid", "lastvalue"],
                    "filter": {"key_": "system.cpu.util[,idle]"},
                })

                # Build CPU map with parent fallback
                cpu_by_host: dict[str, float] = {}
                for item in items:
                    try:
                        idle = float(item.get("lastvalue", "100"))
                    except (ValueError, TypeError):
                        continue
                    cpu_by_host[item["hostid"]] = round(100 - idle, 1)

                # Find overloaded servers
                overloaded = []
                for hid, h in host_map.items():
                    cpu_used = cpu_by_host.get(hid)
                    if cpu_used is None and hid in p_map:
                        cpu_used = cpu_by_host.get(p_map[hid])
                    if cpu_used is not None and cpu_used >= threshold:
                        overloaded.append((cpu_used, h))

                overloaded.sort(key=lambda x: -x[0])

                if not overloaded:
                    return f"No servers above {threshold}% CPU usage."

                parts = [
                    "| Server | Product | Tier | IP | CPU% |",
                    "|--------|---------|------|-----|------|",
                ]

                for cpu, h in overloaded:
                    ip = ""
                    for iface in h.get("interfaces", []):
                        if iface.get("ip") and iface["ip"] != "127.0.0.1":
                            ip = iface["ip"]
                            break
                    parts.append(
                        f"| {h.get('host', '?')} | {h['_product']} | "
                        f"{h['_tier']} | {ip} | **{cpu}%** |"
                    )

                header = f"**{len(overloaded)} servers above {threshold}% CPU**\n\n"
                return header + "\n".join(parts)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

    if "get_underloaded_servers" not in skip:

        @mcp.tool()
        async def get_underloaded_servers(
            threshold: float = 10.0,
            product: str = "",
            country: str = "",
            instance: str = "",
        ) -> str:
            """Find servers with CPU usage below a threshold (potentially idle/wasteful).

            Args:
                threshold: CPU usage percentage threshold (default: 10%)
                product: Filter by product name (optional)
                country: Filter by country code in hostname (optional)
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)

                hosts = await client.call("host.get", {
                    "output": ["hostid", "host", "status"],
                    "selectGroups": ["name"],
                    "selectInterfaces": ["ip"],
                    "filter": {"status": "0"},
                })

                host_map = {}
                for h in hosts:
                    prod, tier = _classify_host(h.get("groups", []))
                    if not prod:
                        continue
                    if product and product.lower() not in prod.lower():
                        continue
                    if country and extract_country(h.get("host", "")).lower() != country.lower():
                        continue
                    h["_product"] = prod
                    h["_tier"] = tier
                    host_map[h["hostid"]] = h

                if not host_map:
                    return "No servers match the filter."

                p_map = build_parent_map(hosts)
                hids = list(host_map.keys())
                parent_ids = list({p_map[h] for h in hids if h in p_map} - set(hids))
                lookup_ids = hids + parent_ids

                # Fetch CPU + traffic in parallel
                items, traffic_items = await asyncio.gather(
                    client.call("item.get", {
                        "hostids": lookup_ids,
                        "output": ["hostid", "lastvalue"],
                        "filter": {"key_": "system.cpu.util[,idle]"},
                    }),
                    client.call("item.get", {
                        "hostids": lookup_ids,
                        "output": ["hostid", "lastvalue"],
                        "filter": {"key_": TRAFFIC_IN_KEYS, "status": "0"},
                    }),
                )

                # Traffic: max per host
                traffic_map: dict[str, float] = {}
                for i in traffic_items:
                    try:
                        val = float(i["lastvalue"])
                        hid = i["hostid"]
                        if val > traffic_map.get(hid, 0):
                            traffic_map[hid] = val
                    except (ValueError, TypeError):
                        pass

                # Build CPU map with parent fallback
                cpu_by_host: dict[str, float] = {}
                for item in items:
                    try:
                        cpu_by_host[item["hostid"]] = round(100 - float(item.get("lastvalue", "0")), 1)
                    except (ValueError, TypeError):
                        pass

                underloaded = []
                for hid, h in host_map.items():
                    cpu_used = cpu_by_host.get(hid)
                    if cpu_used is None and hid in p_map:
                        cpu_used = cpu_by_host.get(p_map[hid])
                    if cpu_used is not None and cpu_used <= threshold:
                        traffic = traffic_map.get(hid) or (traffic_map.get(p_map.get(hid, ""), 0))
                        underloaded.append((cpu_used, traffic, h))

                underloaded.sort(key=lambda x: x[0])

                if not underloaded:
                    return f"No servers below {threshold}% CPU usage."

                parts = [
                    "| Server | Product | Tier | IP | CPU% | Traffic Mbps |",
                    "|--------|---------|------|-----|------|-------------|",
                ]

                for cpu, traffic, h in underloaded:
                    ip = ""
                    for iface in h.get("interfaces", []):
                        if iface.get("ip") and iface["ip"] != "127.0.0.1":
                            ip = iface["ip"]
                            break
                    t_str = f"{traffic/1e6:.1f}" if traffic else "N/A"
                    parts.append(
                        f"| {h.get('host', '?')} | {h['_product']} | "
                        f"{h['_tier']} | {ip} | {cpu}% | {t_str} |"
                    )

                header = f"**{len(underloaded)} servers below {threshold}% CPU**\n\n"
                return header + "\n".join(parts)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

    if "get_provider_summary" not in skip:

        @mcp.tool()
        async def get_provider_summary(
            product: str = "",
            instance: str = "",
        ) -> str:
            """Server distribution across hosting providers.

            Args:
                product: Filter by product name (optional)
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)
                hosts = await client.call("host.get", {
                    "output": ["hostid", "host", "status"],
                    "selectGroups": ["name"],
                    "selectInterfaces": ["ip"],
                    "filter": {"status": "0"},
                })

                # Product × Provider matrix
                matrix: dict[str, dict[str, int]] = {}
                provider_totals: dict[str, int] = {}

                for h in hosts:
                    prod, tier = _classify_host(h.get("groups", []))
                    if not prod or prod == "Unknown":
                        continue
                    if product and product.lower() not in prod.lower():
                        continue

                    ip = ""
                    for iface in h.get("interfaces", []):
                        if iface.get("ip") and iface["ip"] != "127.0.0.1":
                            ip = iface["ip"]
                            break
                    if not ip:
                        continue

                    provider = detect_provider(ip)
                    key = f"{prod} / {tier}"
                    matrix.setdefault(key, {})
                    matrix[key][provider] = matrix[key].get(provider, 0) + 1
                    provider_totals[provider] = provider_totals.get(provider, 0) + 1

                if not matrix:
                    return "No servers match the filter."

                # Provider totals
                parts = ["## Servers by Provider\n",
                         "| Provider | Servers |",
                         "|----------|---------|"]
                for prov, count in sorted(provider_totals.items(), key=lambda x: -x[1]):
                    parts.append(f"| {prov} | {count} |")

                # Product × Provider breakdown
                all_providers = sorted(provider_totals.keys(), key=lambda x: -provider_totals[x])
                parts.append("\n## Product × Provider\n")
                header_cols = " | ".join(all_providers)
                parts.append(f"| Product / Tier | {header_cols} |")
                parts.append(f"|{'---|' * (len(all_providers) + 1)}")

                for key in sorted(matrix):
                    row = [str(matrix[key].get(p, "")) for p in all_providers]
                    parts.append(f"| {key} | {' | '.join(row)} |")

                total = sum(provider_totals.values())
                return f"**Provider Summary: {total} servers**\n\n" + "\n".join(parts)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

    if "get_unknown_providers" not in skip:

        @mcp.tool()
        async def get_unknown_providers(
            max_results: int = 20,
            instance: str = "",
        ) -> str:
            """Show unclassified server IPs grouped by /16 prefix for provider identification.

            Args:
                max_results: Maximum /16 groups to show (default: 20)
                instance: Zabbix instance name (optional)
            """
            try:
                client = resolver.resolve(instance)
                hosts = await client.call("host.get", {
                    "output": ["hostid", "host"],
                    "selectInterfaces": ["ip"],
                    "selectGroups": ["name"],
                    "filter": {"status": "0"},
                })

                prefixes: dict[str, dict] = {}
                for h in hosts:
                    ip = ""
                    for iface in h.get("interfaces", []):
                        if iface.get("ip") and iface["ip"] != "127.0.0.1":
                            ip = iface["ip"]
                            break
                    if not ip:
                        continue
                    if detect_provider(ip) != "Other":
                        continue
                    parts = ip.split(".")
                    prefix = f"{parts[0]}.{parts[1]}.0.0/16"
                    entry = prefixes.setdefault(prefix, {"count": 0, "sample": ip, "hosts": []})
                    entry["count"] += 1
                    if len(entry["hosts"]) < 3:
                        prod, _ = _classify_host(h.get("groups", []))
                        entry["hosts"].append(f"{h['host']} ({prod})")

                if not prefixes:
                    return "No unclassified servers."

                sorted_pfx = sorted(prefixes.items(), key=lambda x: -x[1]["count"])[:max_results]
                total_other = sum(p["count"] for p in prefixes.values())

                lines = [f"**{total_other} unclassified servers** in {len(prefixes)} /16 prefixes\n"]
                lines.append("| /16 Prefix | Servers | Sample IP | Hosts |")
                lines.append("|-----------|---------|-----------|-------|")
                for pfx, data in sorted_pfx:
                    hosts_str = ", ".join(data["hosts"])
                    lines.append(f"| {pfx} | {data['count']} | {data['sample']} | {hosts_str} |")

                if len(prefixes) > max_results:
                    lines.append(f"\n*{len(prefixes) - max_results} more prefixes omitted*")

                return "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

    if "identify_providers" not in skip:

        @mcp.tool()
        async def identify_providers(
            min_servers: int = 2,
            max_results: int = 20,
            instance: str = "",
        ) -> str:
            """Auto-identify unclassified providers via reverse DNS lookup.

            Args:
                min_servers: Minimum servers per /16 to investigate (default: 2)
                max_results: Maximum prefixes to resolve (default: 20)
                instance: Zabbix instance name (optional)
            """
            try:
                client = resolver.resolve(instance)
                hosts = await client.call("host.get", {
                    "output": ["hostid", "host"],
                    "selectInterfaces": ["ip"],
                    "filter": {"status": "0"},
                })

                # Group "Other" IPs by /16
                prefixes: dict[str, dict] = {}
                for h in hosts:
                    ip = ""
                    for iface in h.get("interfaces", []):
                        if iface.get("ip") and iface["ip"] != "127.0.0.1":
                            ip = iface["ip"]
                            break
                    if not ip or detect_provider(ip) != "Other":
                        continue
                    parts = ip.split(".")
                    pfx = f"{parts[0]}.{parts[1]}.0.0/16"
                    entry = prefixes.setdefault(pfx, {"count": 0, "ips": []})
                    entry["count"] += 1
                    if len(entry["ips"]) < 2:
                        entry["ips"].append(ip)

                # Filter by min_servers, sort by count
                targets = sorted(
                    [(pfx, d) for pfx, d in prefixes.items() if d["count"] >= min_servers],
                    key=lambda x: -x[1]["count"],
                )[:max_results]

                if not targets:
                    return f"No unclassified prefixes with {min_servers}+ servers."

                # Reverse DNS lookups (async batched)
                def _rdns(ip: str) -> str:
                    try:
                        hostname = socket.gethostbyaddr(ip)[0]
                        # Extract domain: take last 2-3 parts
                        parts = hostname.split(".")
                        if len(parts) >= 3:
                            return ".".join(parts[-3:])
                        return hostname
                    except (socket.herror, socket.gaierror, OSError):
                        return ""

                loop = asyncio.get_running_loop()
                rdns_tasks = []
                for pfx, data in targets:
                    for ip in data["ips"][:1]:  # one lookup per prefix
                        rdns_tasks.append((pfx, ip, loop.run_in_executor(None, _rdns, ip)))

                results = []
                for pfx, ip, task in rdns_tasks:
                    rdns = await task
                    results.append((pfx, prefixes[pfx]["count"], ip, rdns))

                # Group by rDNS domain to suggest provider names
                domain_groups: dict[str, list] = {}
                for pfx, count, _ip, rdns in results:
                    key = rdns if rdns else "no-rdns"
                    domain_groups.setdefault(key, []).append((pfx, count))

                lines = [f"**Provider identification** ({len(results)} prefixes)\n"]
                lines.append("| /16 Prefix | Servers | Sample rDNS | Suggested Add |")
                lines.append("|-----------|---------|-------------|---------------|")
                for pfx, count, _ip, rdns in sorted(results, key=lambda x: -x[1]):
                    suggest = f'"{pfx}"' if count >= 3 else "low count"
                    lines.append(f"| {pfx} | {count} | {rdns or 'no rDNS'} | {suggest} |")

                # Summary: group by rDNS domain
                if domain_groups:
                    lines.append("\n**Suggested CIDR additions:**")
                    for domain in sorted(domain_groups, key=lambda d: -sum(c for _, c in domain_groups[d])):
                        pfxs = domain_groups[domain]
                        total = sum(c for _, c in pfxs)
                        if total < 3 or domain == "no-rdns":
                            continue
                        cidr_list = ", ".join(f'"{p}"' for p, _ in pfxs)
                        lines.append(f'- **{domain}** ({total} servers): [{cidr_list}]')

                return "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

    if "get_low_disk_servers" not in skip:

        @mcp.tool()
        async def get_low_disk_servers(
            threshold: float = 70.0,
            max_results: int = 30,
            instance: str = "",
        ) -> str:
            """Find servers with low disk space across the fleet.

            Args:
                threshold: Disk usage % to flag (default: 70)
                max_results: Maximum results (default: 30)
                instance: Zabbix instance name (optional)
            """
            try:
                client = resolver.resolve(instance)

                # Get disk utilization from both standard and custom keys
                import asyncio as _aio
                vfs_task = client.call("item.get", {
                    "output": ["itemid", "hostid", "key_", "lastvalue"],
                    "search": {"key_": "vfs.fs.size"},
                    "searchWildcardsEnabled": True,
                    "filter": {"status": "0"},
                })
                custom_task = client.call("item.get", {
                    "output": ["itemid", "hostid", "key_", "lastvalue"],
                    "filter": {"key_": "disk.fs.root", "status": "0"},
                })
                vfs_items, custom_items = await _aio.gather(vfs_task, custom_task)

                # Group by host: pick the highest utilization per host
                host_disk: dict[str, dict] = {}
                for it in (vfs_items if isinstance(vfs_items, list) else []):
                    key = it.get("key_", "")
                    if ",pused]" in key:
                        try:
                            pct = float(it.get("lastvalue", 0))
                        except (ValueError, TypeError):
                            continue
                    elif ",pfree]" in key:
                        try:
                            pct = 100 - float(it.get("lastvalue", 0))
                        except (ValueError, TypeError):
                            continue
                    else:
                        continue
                    hid = it["hostid"]
                    mount = key.split("[")[1].split(",")[0] if "[" in key else "/"
                    if hid not in host_disk or pct > host_disk[hid]["pct"]:
                        host_disk[hid] = {"pct": round(pct, 1), "mount": mount}

                for it in (custom_items if isinstance(custom_items, list) else []):
                    try:
                        pct = float(it.get("lastvalue", 0))
                    except (ValueError, TypeError):
                        continue
                    hid = it["hostid"]
                    if hid not in host_disk or pct > host_disk[hid]["pct"]:
                        host_disk[hid] = {"pct": round(pct, 1), "mount": "/"}

                if not host_disk:
                    return "No disk utilization data found."

                flagged = [(hid, d) for hid, d in host_disk.items() if d["pct"] >= threshold]
                flagged.sort(key=lambda x: -x[1]["pct"])

                if not flagged:
                    return f"No servers above {threshold}% disk usage."

                hids = [hid for hid, _ in flagged[:max_results]]
                hosts = await client.call("host.get", {
                    "hostids": hids,
                    "output": ["hostid", "host"],
                    "selectGroups": ["name"],
                    "selectInterfaces": ["ip"],
                })
                host_map = {h["hostid"]: h for h in hosts}

                lines = [f"**{len(flagged)} servers above {threshold}% disk usage**\n"]
                lines.append("| Host | Disk % | Mount | Product | Provider |")
                lines.append("|------|--------|-------|---------|----------|")

                for hid, d in flagged[:max_results]:
                    h = host_map.get(hid, {})
                    hostname = h.get("host", hid)
                    prod, _ = _classify_host(h.get("groups", []))
                    ip = ""
                    for iface in h.get("interfaces", []):
                        if iface.get("ip") and iface["ip"] != "127.0.0.1":
                            ip = iface["ip"]
                            break
                    prov = detect_provider(ip) if ip else "?"
                    tag = "CRITICAL" if d["pct"] >= 90 else "WARNING" if d["pct"] >= 80 else "ALERT"
                    lines.append(f"| {hostname} | **{d['pct']}%** ({tag}) | {d['mount']} | {prod} | {prov} |")

                if len(flagged) > max_results:
                    lines.append(f"\n*{len(flagged) - max_results} more not shown*")

                return "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

    if "get_low_memory_servers" not in skip:

        @mcp.tool()
        async def get_low_memory_servers(
            threshold_gb: float = 0.5,
            max_results: int = 30,
            instance: str = "",
        ) -> str:
            """Find servers with low available memory.

            Args:
                threshold_gb: Flag servers below this GB free (default: 0.5)
                max_results: Maximum results (default: 30)
                instance: Zabbix instance name (optional)
            """
            try:
                client = resolver.resolve(instance)
                items = await client.call("item.get", {
                    "output": ["hostid", "lastvalue"],
                    "filter": {"key_": "vm.memory.size[available]", "status": "0"},
                })

                host_mem: dict[str, float] = {}
                for it in items:
                    try:
                        avail_gb = float(it.get("lastvalue", 0)) / 1_073_741_824
                        hid = it["hostid"]
                        if hid not in host_mem or avail_gb < host_mem[hid]:
                            host_mem[hid] = round(avail_gb, 2)
                    except (ValueError, TypeError):
                        pass

                if not host_mem:
                    return "No memory data found."

                flagged = [(hid, mem) for hid, mem in host_mem.items() if mem < threshold_gb]
                flagged.sort(key=lambda x: x[1])

                if not flagged:
                    return f"No servers below {threshold_gb} GB free memory."

                hids = [hid for hid, _ in flagged[:max_results]]
                hosts = await client.call("host.get", {
                    "hostids": hids,
                    "output": ["hostid", "host"],
                    "selectGroups": ["name"],
                    "selectInterfaces": ["ip"],
                })
                host_map = {h["hostid"]: h for h in hosts}

                lines = [f"**{len(flagged)} servers below {threshold_gb} GB free**\n"]
                lines.append("| Host | Free GB | Product | Provider |")
                lines.append("|------|---------|---------|----------|")
                for hid, mem in flagged[:max_results]:
                    h = host_map.get(hid, {})
                    hostname = h.get("host", hid)
                    prod, _ = _classify_host(h.get("groups", []))
                    ip = ""
                    for iface in h.get("interfaces", []):
                        if iface.get("ip") and iface["ip"] != "127.0.0.1":
                            ip = iface["ip"]
                            break
                    prov = detect_provider(ip) if ip else "?"
                    tag = "CRITICAL" if mem < 0.1 else "WARNING" if mem < 0.3 else "LOW"
                    lines.append(f"| {hostname} | **{mem} GB** ({tag}) | {prod} | {prov} |")

                if len(flagged) > max_results:
                    lines.append(f"\n*{len(flagged) - max_results} more not shown*")
                return "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"
