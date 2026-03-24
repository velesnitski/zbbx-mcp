"""Server cost management via {$COST_MONTH} host macros."""

import httpx

from zbbx_mcp.resolver import InstanceResolver


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()):

    if "import_server_costs" not in skip:

        @mcp.tool()
        async def import_server_costs(
            costs_json: str,
            instance: str = "",
        ) -> str:
            """Bulk-set monthly costs on servers using {$COST_MONTH} host macros.

            Pass a JSON object mapping hostname patterns to cost in USD.
            Patterns support prefix matching with '*' (e.g., 'srv-free-*').

            Example:
                {"srv-free-*": 20, "srv-prem-*": 95, "srv-lite-*": 15, "srv-01": 45}

            Args:
                costs_json: JSON string mapping hostname patterns to monthly cost in USD
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            import json
            import fnmatch

            try:
                cost_rules = json.loads(costs_json)
            except json.JSONDecodeError:
                return "Invalid JSON. Expected: {\"hostname-pattern\": cost, ...}"

            if not isinstance(cost_rules, dict):
                return "Expected a JSON object mapping hostname patterns to costs."

            try:
                client = resolver.resolve(instance)

                # Get all enabled hosts
                hosts = await client.call("host.get", {
                    "output": ["hostid", "host"],
                    "filter": {"status": "0"},
                })

                # Get existing {$COST_MONTH} macros
                existing = await client.call("usermacro.get", {
                    "hostids": [h["hostid"] for h in hosts],
                    "output": ["hostmacroid", "hostid", "value"],
                    "filter": {"macro": "{$COST_MONTH}"},
                })
                existing_map = {m["hostid"]: m for m in existing}

                # Match hosts to cost rules
                matched = 0
                created = 0
                updated = 0
                skipped = 0
                errors = []

                for h in hosts:
                    hostname = h["host"]
                    hid = h["hostid"]
                    cost = None

                    # Check rules in order (exact match first, then patterns)
                    for pattern, value in cost_rules.items():
                        if hostname == pattern or fnmatch.fnmatch(hostname, pattern):
                            cost = value
                            break

                    if cost is None:
                        continue

                    matched += 1
                    cost_str = str(cost)

                    try:
                        if hid in existing_map:
                            # Update if value changed
                            if existing_map[hid]["value"] != cost_str:
                                await client.call("usermacro.update", {
                                    "hostmacroid": existing_map[hid]["hostmacroid"],
                                    "value": cost_str,
                                })
                                updated += 1
                            else:
                                skipped += 1
                        else:
                            # Create new macro
                            await client.call("usermacro.create", {
                                "hostid": hid,
                                "macro": "{$COST_MONTH}",
                                "value": cost_str,
                                "description": "Monthly server cost in USD",
                            })
                            created += 1
                    except (httpx.HTTPError, ValueError) as e:
                        errors.append(f"{hostname}: {e}")
                        if len(errors) >= 10:
                            break

                parts = [
                    f"**Cost import complete**",
                    f"",
                    f"Matched: {matched} servers",
                    f"Created: {created} new macros",
                    f"Updated: {updated} existing macros",
                    f"Unchanged: {skipped}",
                    f"Unmatched: {len(hosts) - matched} servers (no matching pattern)",
                ]
                if errors:
                    parts.append(f"\nErrors ({len(errors)}):")
                    for e in errors:
                        parts.append(f"- {e}")

                return "\n".join(parts)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error importing costs: {e}"

    if "set_bulk_cost" not in skip:

        @mcp.tool()
        async def set_bulk_cost(
            group: str,
            cost: float,
            instance: str = "",
        ) -> str:
            """Set monthly cost for all servers in a host group.

            Args:
                group: Zabbix host group name
                cost: Monthly cost in USD per server
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)

                # Resolve group
                groups = await client.call("hostgroup.get", {
                    "output": ["groupid"],
                    "filter": {"name": [group]},
                })
                if not groups:
                    return f"Host group '{group}' not found."

                # Get hosts in group
                hosts = await client.call("host.get", {
                    "output": ["hostid", "host"],
                    "groupids": [groups[0]["groupid"]],
                    "filter": {"status": "0"},
                })
                if not hosts:
                    return f"No enabled hosts in group '{group}'."

                # Get existing macros
                hostids = [h["hostid"] for h in hosts]
                existing = await client.call("usermacro.get", {
                    "hostids": hostids,
                    "output": ["hostmacroid", "hostid", "value"],
                    "filter": {"macro": "{$COST_MONTH}"},
                })
                existing_map = {m["hostid"]: m for m in existing}

                cost_str = str(cost)
                created = 0
                updated = 0
                skipped = 0

                for h in hosts:
                    hid = h["hostid"]
                    if hid in existing_map:
                        if existing_map[hid]["value"] != cost_str:
                            await client.call("usermacro.update", {
                                "hostmacroid": existing_map[hid]["hostmacroid"],
                                "value": cost_str,
                            })
                            updated += 1
                        else:
                            skipped += 1
                    else:
                        await client.call("usermacro.create", {
                            "hostid": hid,
                            "macro": "{$COST_MONTH}",
                            "value": cost_str,
                            "description": "Monthly server cost in USD",
                        })
                        created += 1

                return (
                    f"Set ${cost}/month on {len(hosts)} servers in '{group}'. "
                    f"Created: {created}, Updated: {updated}, Unchanged: {skipped}."
                )
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

    if "get_cost_summary" not in skip:

        @mcp.tool()
        async def get_cost_summary(instance: str = "") -> str:
            """Get a summary of server costs from {$COST_MONTH} macros.

            Args:
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                from zbbx_mcp.tools.inventory import _classify_host, detect_provider

                client = resolver.resolve(instance)

                hosts, macros = await __import__("asyncio").gather(
                    client.call("host.get", {
                        "output": ["hostid", "host"],
                        "selectGroups": ["name"],
                        "selectInterfaces": ["ip"],
                        "filter": {"status": "0"},
                    }),
                    client.call("usermacro.get", {
                        "output": ["hostid", "value"],
                        "filter": {"macro": "{$COST_MONTH}"},
                    }),
                )

                host_map = {h["hostid"]: h for h in hosts}
                cost_map = {}
                for m in macros:
                    try:
                        cost_map[m["hostid"]] = float(m["value"])
                    except (ValueError, TypeError):
                        pass

                # Aggregate by product and provider
                prod_costs: dict[str, dict] = {}
                prov_costs: dict[str, dict] = {}

                for hid, cost in cost_map.items():
                    h = host_map.get(hid)
                    if not h:
                        continue
                    prod, tier = _classify_host(h.get("groups", []))
                    ip = next((i["ip"] for i in h.get("interfaces", []) if i.get("ip") != "127.0.0.1"), "")
                    provider = detect_provider(ip) if ip else "No IP"

                    key = f"{prod} / {tier}"
                    p = prod_costs.setdefault(key, {"count": 0, "total": 0.0})
                    p["count"] += 1
                    p["total"] += cost

                    pv = prov_costs.setdefault(provider, {"count": 0, "total": 0.0})
                    pv["count"] += 1
                    pv["total"] += cost

                total = sum(cost_map.values())
                costed = len(cost_map)
                uncosted = len(hosts) - costed

                parts = [
                    f"**Cost Summary: ${total:,.2f}/month (${total * 12:,.2f}/year)**",
                    f"Servers with cost: {costed} | Without: {uncosted}",
                    f"",
                    f"## By Product",
                    f"| Product / Tier | Servers | Cost/Month | Cost/Year |",
                    f"|---|---|---|---|",
                ]
                for key in sorted(prod_costs, key=lambda x: -prod_costs[x]["total"]):
                    p = prod_costs[key]
                    parts.append(f"| {key} | {p['count']} | ${p['total']:,.2f} | ${p['total']*12:,.2f} |")

                parts.extend([
                    f"",
                    f"## By Provider",
                    f"| Provider | Servers | Cost/Month | Cost/Year |",
                    f"|---|---|---|---|",
                ])
                for prov in sorted(prov_costs, key=lambda x: -prov_costs[x]["total"]):
                    p = prov_costs[prov]
                    parts.append(f"| {prov} | {p['count']} | ${p['total']:,.2f} | ${p['total']*12:,.2f} |")

                return "\n".join(parts)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"
