"""Server cost management via {$COST_MONTH} host macros."""

import asyncio
import fnmatch
import json

import httpx

from zbbx_mcp.data import host_ip
from zbbx_mcp.resolver import InstanceResolver
from zbbx_mcp.utils import resolve_group_ids


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()) -> None:

    if "import_server_costs" not in skip:

        @mcp.tool()
        async def import_server_costs(
            costs_json: str,
            instance: str = "",
        ) -> str:
            """Bulk-set monthly costs on servers using {$COST_MONTH} host macros.

            Args:
                costs_json: JSON mapping hostname patterns to USD cost (e.g. {"srv-nl-*": 20})
                instance: Zabbix instance (optional)
            """
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
                    "**Cost import complete**",
                    "",
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
                gids = await resolve_group_ids(client, group)
                if gids is None:
                    return f"Host group '{group}' not found."

                # Get hosts in group
                hosts = await client.call("host.get", {
                    "output": ["hostid", "host"],
                    "groupids": gids,
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

    if "import_costs_by_ip" not in skip:

        @mcp.tool()
        async def import_costs_by_ip(
            costs_json: str,
            dry_run: bool = True,
            min_cost: float = 10,
            max_cost: float = 1000,
            instance: str = "",
        ) -> str:
            """Bulk-set monthly costs by matching server IPs AND hostnames.

            Args:
                costs_json: JSON with "by_ip" and/or "by_name" maps, or flat IP→cost map
                dry_run: Preview only, don't write (default: True)
                min_cost: Skip prices below this (default: $10)
                max_cost: Skip prices above this (default: $1000)
                instance: Zabbix instance (optional)
            """
            try:
                raw = json.loads(costs_json)
            except json.JSONDecodeError:
                return "Invalid JSON."

            # Support both formats: {"by_ip": {...}, "by_name": {...}} or flat {"IP": cost}
            if isinstance(raw, dict) and ("by_ip" in raw or "by_name" in raw):
                ip_costs = raw.get("by_ip", {})
                name_costs = raw.get("by_name", {})
            elif isinstance(raw, dict):
                ip_costs = raw
                name_costs = {}
            else:
                return "Expected JSON object with IP/hostname→cost mappings."

            def _safe(d):
                return {k: float(v) for k, v in d.items() if min_cost <= float(v) <= max_cost}

            safe_ips = _safe(ip_costs)
            safe_names = _safe(name_costs)
            total_input = len(ip_costs) + len(name_costs)
            skipped_range = total_input - len(safe_ips) - len(safe_names)

            try:
                client = resolver.resolve(instance)
                hosts = await client.call("host.get", {
                    "output": ["hostid", "host"],
                    "selectInterfaces": ["ip"],
                    "filter": {"status": "0"},
                })

                # Build lookup maps
                ip_to_host: dict[str, dict] = {}
                name_to_host: dict[str, dict] = {}
                for h in hosts:
                    ip = host_ip(h)
                    if ip:
                        ip_to_host[ip] = h
                    name_to_host[h["host"].lower()] = h

                # Match by IP first, then by hostname (avoid duplicates)
                matched_hids: set[str] = set()
                matches = []

                for ip, cost in safe_ips.items():
                    ip = ip.strip().replace("ip-", "")
                    if ip in ip_to_host:
                        h = ip_to_host[ip]
                        if h["hostid"] not in matched_hids:
                            matches.append((h["host"], h["hostid"], ip, cost))
                            matched_hids.add(h["hostid"])

                for name, cost in safe_names.items():
                    h = name_to_host.get(name.lower())
                    if h and h["hostid"] not in matched_hids:
                        matches.append((h["host"], h["hostid"], "", cost))
                        matched_hids.add(h["hostid"])

                total_safe = len(safe_ips) + len(safe_names)
                if not matches:
                    return f"No matches. Input: {total_safe} entries, Zabbix: {len(hosts)} hosts."

                if dry_run:
                    lines = [f"**DRY RUN — {len(matches)} hosts matched** (of {total_safe} entries)\n"]
                    lines.append("| Host | IP | $/mo |")
                    lines.append("|------|-----|------|")
                    total = 0.0
                    for hostname, _hid, ip, cost in sorted(matches, key=lambda x: -x[3])[:30]:
                        lines.append(f"| {hostname} | {ip or 'name match'} | ${cost:.2f} |")
                        total += cost
                    if len(matches) > 30:
                        lines.append(f"| ... | {len(matches) - 30} more | |")
                        total = sum(c for _, _, _, c in matches)
                    lines.append(f"\n**Total: ${total:,.2f}/mo** (${total*12:,.2f}/yr)")
                    lines.append(f"Skipped: {skipped_range} outside ${min_cost}-${max_cost} range")
                    lines.append(f"Unmatched: {total_safe - len(matches)} entries not in Zabbix")
                    lines.append("\nSet `dry_run=false` to apply.")
                    return "\n".join(lines)

                # Get existing macros
                existing = await client.call("usermacro.get", {
                    "hostids": [m[1] for m in matches],
                    "output": ["hostmacroid", "hostid", "value"],
                    "filter": {"macro": "{$COST_MONTH}"},
                })
                existing_map = {m["hostid"]: m for m in existing}

                created = 0
                updated = 0
                unchanged = 0
                errors = []

                for hostname, hid, _ip, cost in matches:
                    cost_str = str(round(cost, 2))
                    try:
                        if hid in existing_map:
                            if existing_map[hid]["value"] != cost_str:
                                await client.call("usermacro.update", {
                                    "hostmacroid": existing_map[hid]["hostmacroid"],
                                    "value": cost_str,
                                })
                                updated += 1
                            else:
                                unchanged += 1
                        else:
                            await client.call("usermacro.create", {
                                "hostid": hid,
                                "macro": "{$COST_MONTH}",
                                "value": cost_str,
                                "description": "Monthly server cost (USD)",
                            })
                            created += 1
                    except (httpx.HTTPError, ValueError) as e:
                        errors.append(f"{hostname}: {e}")
                        if len(errors) >= 10:
                            break

                total = sum(c for _, _, _, c in matches)
                parts = [
                    f"**Cost import complete — {len(matches)} servers**",
                    f"Created: {created} | Updated: {updated} | Unchanged: {unchanged}",
                    f"Total: ${total:,.2f}/mo (${total*12:,.2f}/yr)",
                ]
                if errors:
                    parts.append(f"Errors: {len(errors)}")
                return "\n".join(parts)
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
                from zbbx_mcp.classify import classify_host as _classify_host
                from zbbx_mcp.classify import detect_provider

                client = resolver.resolve(instance)

                hosts, macros = await asyncio.gather(
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
                    ip = host_ip(h)
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
                    "",
                    "## By Product",
                    "| Product / Tier | Servers | Cost/Month | Cost/Year |",
                    "|---|---|---|---|",
                ]
                for key in sorted(prod_costs, key=lambda x: -prod_costs[x]["total"]):
                    p = prod_costs[key]
                    parts.append(f"| {key} | {p['count']} | ${p['total']:,.2f} | ${p['total']*12:,.2f} |")

                parts.extend([
                    "",
                    "## By Provider",
                    "| Provider | Servers | Cost/Month | Cost/Year |",
                    "|---|---|---|---|",
                ])
                for prov in sorted(prov_costs, key=lambda x: -prov_costs[x]["total"]):
                    p = prov_costs[prov]
                    parts.append(f"| {prov} | {p['count']} | ${p['total']:,.2f} | ${p['total']*12:,.2f} |")

                return "\n".join(parts)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"
