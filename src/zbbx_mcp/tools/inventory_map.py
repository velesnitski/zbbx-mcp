"""Inventory mapping tools: server maps, product grouping, product map generation."""

import json

import httpx

from zbbx_mcp.classify import (
    classify_host as _classify_host,
)
from zbbx_mcp.classify import (
    detect_provider,
    resolve_datacenter,
)
from zbbx_mcp.data import extract_country
from zbbx_mcp.resolver import InstanceResolver


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
                        parts.append("| Server | IP | Provider | City | Groups |")
                        parts.append("|--------|-----|----------|------|--------|")
                        for h in hosts_list:
                            ip = ""
                            for iface in h.get("interfaces", []):
                                if iface.get("ip") and iface["ip"] != "127.0.0.1":
                                    ip = iface["ip"]
                                    break
                            if ip:
                                provider, city = resolve_datacenter(ip)
                                if provider in ("Unknown", "Other"):
                                    provider = detect_provider(ip) or provider
                            else:
                                provider, city = "", ""
                            groups = ", ".join(g["name"] for g in h.get("groups", []))
                            parts.append(f"| {h.get('host', '?')} | {ip} | {provider} | {city} | {groups} |")
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

                free_keywords = {"free", "relay", "basic"}
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

    if "generate_product_map" not in skip:

        @mcp.tool()
        async def generate_product_map(instance: str = "") -> str:
            """Generate a starter product_map.json from Zabbix host groups.

            Args:
                instance: Zabbix instance name (optional)
            """
            try:
                client = resolver.resolve(instance)
                groups = await client.call("hostgroup.get", {
                    "output": ["groupid", "name"],
                    "selectHosts": ["hostid"],
                })

                skip_names = {"Templates", "Templates/Applications", "Templates/Databases", "Discovered hosts"}
                result = {}
                for g in sorted(groups, key=lambda x: -len(x.get("hosts", []))):
                    name = g["name"]
                    count = len(g.get("hosts", []))
                    if name in skip_names or count == 0:
                        continue
                    result[name] = [name, "Default"]

                output = json.dumps(result, indent=2, ensure_ascii=False)
                return (
                    f"**Starter product_map.json** ({len(result)} groups)\n\n"
                    f"```json\n{output}\n```\n\n"
                    f"Edit the values: `[\"ProductName\", \"Tier\"]` or `[\"skip\"]` to hide.\n"
                    f"Save as `product_map.json` and set `ZABBIX_PRODUCT_MAP=/path/to/product_map.json`."
                )
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"
