"""Business-level inventory tools: server maps, product grouping, load analysis."""

import asyncio

import httpx

from zbbx_mcp.classify import (
    classify_host as _classify_host,
)
from zbbx_mcp.classify import (
    detect_provider,
)
from zbbx_mcp.data import TRAFFIC_IN_KEYS, extract_country
from zbbx_mcp.resolver import InstanceResolver
from zbbx_mcp.tools.items import _format_value


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()):

    if "get_server_map" not in skip:

        @mcp.tool()
        async def get_server_map(
            product: str = "",
            tier: str = "",
            country: str = "",
            only_enabled: bool = True,
            instance: str = "",
        ) -> str:
            """Build a server map: Product → Tier → Server → IP → Status.

            Args:
                product: Filter by product name (optional)
                tier: Filter by tier (e.g., 'Free', 'Premium', 'Lite') (optional)
                country: Filter by country code in hostname (e.g., 'nl', 'de', 'us') (optional)
                only_enabled: Only show enabled hosts (default: True)
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)
                hosts = await client.call("host.get", {
                    "output": ["hostid", "host", "name", "status"],
                    "selectGroups": ["groupid", "name"],
                    "selectInterfaces": ["ip", "type"],
                    "sortfield": "host",
                })

                # Build product → tier → hosts tree
                tree: dict[str, dict[str, list]] = {}
                for h in hosts:
                    if only_enabled and h.get("status") != "0":
                        continue
                    if country and extract_country(h.get("host", "")).lower() != country.lower():
                        continue

                    prod, t = _classify_host(h.get("groups", []))
                    if not prod:
                        continue
                    if product and product.lower() not in prod.lower():
                        continue
                    if tier and tier.lower() not in t.lower():
                        continue

                    tree.setdefault(prod, {}).setdefault(t, []).append(h)

                if not tree:
                    return "No servers match the filters."

                parts = []
                total = 0
                for prod in sorted(tree):
                    prod_count = sum(len(hosts) for hosts in tree[prod].values())
                    total += prod_count
                    parts.append(f"## {prod} ({prod_count} servers)")
                    for t in sorted(tree[prod]):
                        hosts_list = tree[prod][t]
                        parts.append(f"\n### {t} ({len(hosts_list)})")
                        parts.append("| Server | IP | Provider | Groups |")
                        parts.append("|--------|-----|----------|--------|")
                        for h in hosts_list:
                            ip = ""
                            for iface in h.get("interfaces", []):
                                if iface.get("ip") and iface["ip"] != "127.0.0.1":
                                    ip = iface["ip"]
                                    break
                            provider = detect_provider(ip) if ip else ""
                            groups = ", ".join(g["name"] for g in h.get("groups", []))
                            parts.append(f"| {h.get('host', '?')} | {ip} | {provider} | {groups} |")
                    parts.append("")

                header = f"**Server Map: {total} servers**\n"
                return header + "\n".join(parts)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error building server map: {e}"

    if "get_product_summary" not in skip:

        @mcp.tool()
        async def get_product_summary(instance: str = "") -> str:
            """Get a summary of all products with server counts by tier (Free vs Paid).

            Args:
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)
                hosts = await client.call("host.get", {
                    "output": ["hostid", "host", "status"],
                    "selectGroups": ["name"],
                })

                products: dict[str, dict[str, dict]] = {}
                for h in hosts:
                    prod, tier = _classify_host(h.get("groups", []))
                    if not prod:
                        continue
                    enabled = h.get("status") == "0"
                    entry = products.setdefault(prod, {}).setdefault(tier, {"total": 0, "enabled": 0})
                    entry["total"] += 1
                    if enabled:
                        entry["enabled"] += 1

                parts = ["| Product | Tier | Total | Enabled | Type |",
                         "|---------|------|-------|---------|------|"]

                free_keywords = {"free", "filtered", "proxy"}
                for prod in sorted(products):
                    for tier in sorted(products[prod]):
                        info = products[prod][tier]
                        is_free = any(k in tier.lower() for k in free_keywords)
                        ptype = "Free" if is_free else "Paid"
                        parts.append(
                            f"| {prod} | {tier} | {info['total']} | "
                            f"{info['enabled']} | {ptype} |"
                        )

                grand_total = sum(
                    info["total"]
                    for tiers in products.values()
                    for info in tiers.values()
                )
                grand_enabled = sum(
                    info["enabled"]
                    for tiers in products.values()
                    for info in tiers.values()
                )

                header = f"**Product Summary: {grand_total} servers ({grand_enabled} enabled)**\n\n"
                return header + "\n".join(parts)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

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
            """Get server load metrics (CPU, memory, network) sorted by utilization.

            Use this to find overloaded or underloaded servers.

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

                # Batch-fetch CPU/load/mem + traffic in parallel
                hostids = [h["hostid"] for h in filtered]
                items, net_items = await asyncio.gather(
                    client.call("item.get", {
                        "hostids": hostids,
                        "output": ["hostid", "itemid", "key_", "lastvalue", "units"],
                        "filter": {"key_": [
                            "system.cpu.util[,idle]",
                            "system.cpu.load[percpu,avg1]",
                            "vm.memory.size[available]",
                        ]},
                        "sortfield": "key_",
                    }),
                    client.call("item.get", {
                        "hostids": hostids,
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

                items = await client.call("item.get", {
                    "hostids": list(host_map.keys()),
                    "output": ["hostid", "lastvalue"],
                    "filter": {"key_": "system.cpu.util[,idle]"},
                })

                # Find overloaded servers
                overloaded = []
                for item in items:
                    try:
                        idle = float(item.get("lastvalue", "100"))
                    except (ValueError, TypeError):
                        continue
                    cpu_used = round(100 - idle, 1)
                    if cpu_used >= threshold:
                        hid = item["hostid"]
                        if hid in host_map:
                            h = host_map[hid]
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

                # Fetch CPU + traffic in parallel
                hids = list(host_map.keys())
                items, traffic_items = await asyncio.gather(
                    client.call("item.get", {
                        "hostids": hids,
                        "output": ["hostid", "lastvalue"],
                        "filter": {"key_": "system.cpu.util[,idle]"},
                    }),
                    client.call("item.get", {
                        "hostids": hids,
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

                underloaded = []
                for item in items:
                    try:
                        idle = float(item.get("lastvalue", "0"))
                    except (ValueError, TypeError):
                        continue
                    cpu_used = round(100 - idle, 1)
                    if cpu_used <= threshold:
                        hid = item["hostid"]
                        if hid in host_map:
                            h = host_map[hid]
                            traffic = traffic_map.get(hid, 0)
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
            """Show server distribution across hosting providers (OVH, Scaleway, Hetzner, AWS, etc.).

            Detects provider from IP address using known CIDR ranges.

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
