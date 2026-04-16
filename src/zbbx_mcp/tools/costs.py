"""Server cost management via {$COST_MONTH} host macros."""

import asyncio
import fnmatch
import json
import os
import re

import httpx

from zbbx_mcp.data import host_ip
from zbbx_mcp.resolver import InstanceResolver
from zbbx_mcp.utils import resolve_group_ids

_IP_RE = re.compile(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b")


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
            file_path: str = "",
            costs_json: str = "",
            dry_run: bool = True,
            min_cost: float = 1,
            max_cost: float = 5000,
            export_unmatched: str = "",
            instance: str = "",
        ) -> str:
            """Smart cost import: match by IP, then hostname (7-pass fuzzy).

            Name matching: exact → split-on-dash → IP-in-name → prefix → contains → billing name translation.

            Args:
                file_path: Path to JSON cost file (preferred over costs_json)
                costs_json: Inline JSON (alternative to file_path)
                dry_run: Preview only, don't write (default: True)
                min_cost: Skip prices below this (default: $1)
                max_cost: Skip prices above this (default: $5000)
                export_unmatched: File path to save unmatched entries as JSON (optional)
                instance: Zabbix instance (optional)
            """
            # Load data
            try:
                if file_path:
                    with open(os.path.expanduser(file_path)) as f:
                        raw = json.load(f)
                elif costs_json:
                    raw = json.loads(costs_json)
                else:
                    return "Provide file_path or costs_json."
            except (json.JSONDecodeError, FileNotFoundError, OSError) as e:
                return f"Failed to load: {e}"

            if isinstance(raw, dict) and ("by_ip" in raw or "by_name" in raw):
                ip_costs = raw.get("by_ip", {})
                name_costs = raw.get("by_name", {})
            elif isinstance(raw, dict):
                ip_costs = raw
                name_costs = {}
            else:
                return "Expected JSON object with by_ip/by_name or flat IP→cost map."

            def _in_range(v):
                try:
                    return min_cost <= float(v) <= max_cost
                except (ValueError, TypeError):
                    return False

            safe_ips = {k: float(v) for k, v in ip_costs.items() if _in_range(v)}
            safe_names = {k: float(v) for k, v in name_costs.items() if _in_range(v)}
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
                name_list: list[str] = []
                for h in hosts:
                    ip = host_ip(h)
                    if ip:
                        ip_to_host[ip] = h
                    lname = h["host"].lower()
                    name_to_host[lname] = h
                    name_list.append(lname)

                # --- Pass 1: by_ip (IP is source of truth) ---
                host_costs: dict[str, float] = {}  # hostid → cost
                host_source: dict[str, str] = {}   # hostid → match method

                for ip, cost in safe_ips.items():
                    ip = ip.strip()
                    h = ip_to_host.get(ip)
                    if h:
                        hid = h["hostid"]
                        host_costs[hid] = max(host_costs.get(hid, 0), cost)
                        host_source[hid] = "ip"

                # --- Pass 1b: partial IP /24 prefix match (3-octet IPs) ---
                # Matches "x.y.z" against hosts with IP "x.y.z.*"
                # Runtime only — no IPs stored in code
                for ip_key, cost in safe_ips.items():
                    ip_key = ip_key.strip()
                    parts = ip_key.split(".")
                    if len(parts) == 3:
                        prefix = ip_key + "."
                        for full_ip, h in ip_to_host.items():
                            if full_ip.startswith(prefix):
                                hid = h["hostid"]
                                if hid not in host_costs or cost > host_costs[hid]:
                                    host_costs[hid] = cost
                                    host_source[hid] = "ip/24"

                # --- Pass 2-5: by_name (fill gaps, use max if IP already matched) ---
                unmatched_names: dict[str, float] = {}

                for name, cost in safe_names.items():
                    name_lower = name.lower().strip()
                    matched_host = None

                    # Pass 2: exact lowercase match
                    if name_lower in name_to_host:
                        matched_host = name_to_host[name_lower]

                    # Pass 3: split on " - ", try last segment
                    if not matched_host and " - " in name:
                        segments = name.split(" - ")
                        for seg in reversed(segments):
                            seg_clean = seg.strip().lower()
                            if seg_clean in name_to_host:
                                matched_host = name_to_host[seg_clean]
                                break

                    # Pass 4: extract IP from name, match via interface
                    if not matched_host:
                        ip_match = _IP_RE.search(name)
                        if ip_match:
                            found_ip = ip_match.group(1)
                            if found_ip in ip_to_host:
                                matched_host = ip_to_host[found_ip]

                    # Pass 5: prefix match (name is start of a Zabbix hostname)
                    if not matched_host and len(name_lower) >= 4:
                        for zname in name_list:
                            if zname.startswith(name_lower) or name_lower.startswith(zname):
                                matched_host = name_to_host[zname]
                                break

                    # Pass 6: billing name translation
                    # Billing often uses reversed naming: cc+num+suffix → suffix-cc+num
                    if not matched_host:
                        candidates = []
                        # Reverse: ccNNN-suffix -> suffix-cc0NNN
                        m = re.match(r"^([a-z]{2})(\d+)-(.+)$", name_lower)
                        if m:
                            cc, num, suffix = m.group(1), m.group(2), m.group(3)
                            candidates.extend([
                                f"{suffix}-{cc}{num.zfill(4)}",
                                f"{suffix}-{cc}{num}",
                            ])
                        # Prefix swap: pfx-ccNNN-suffix -> pfx-suffix-cc0NNN
                        m = re.match(r"^([a-z]{2,4})-([a-z]{2})(\d+)-(.+)$", name_lower)
                        if m:
                            pfx, cc, num, suffix = m.group(1), m.group(2), m.group(3), m.group(4)
                            candidates.append(f"{pfx}-{suffix}-{cc}{num.zfill(4)}")
                        # Configurable renames via env var
                        # ZABBIX_BILLING_RENAMES="old1:new1,old2:new2"
                        renames_raw = os.environ.get("ZABBIX_BILLING_RENAMES", "")
                        if renames_raw:
                            for pair in renames_raw.split(","):
                                if ":" in pair:
                                    old, new = pair.strip().split(":", 1)
                                    if old in name_lower:
                                        candidates.append(name_lower.replace(old, new))

                        for cand in candidates:
                            if cand in name_to_host:
                                matched_host = name_to_host[cand]
                                host_source[matched_host["hostid"]] = "translated"
                                break

                    if matched_host:
                        hid = matched_host["hostid"]
                        # max(ip_cost, name_cost) when both exist
                        host_costs[hid] = max(host_costs.get(hid, 0), cost)
                        if hid not in host_source:
                            host_source[hid] = "name"
                    else:
                        unmatched_names[name] = cost

                # Build match list
                host_map = {h["hostid"]: h for h in hosts}
                matches = []
                for hid, cost in host_costs.items():
                    h = host_map.get(hid)
                    if h:
                        ip = host_ip(h)
                        matches.append((h["host"], hid, ip or "", cost, host_source.get(hid, "?")))

                ip_matches = sum(1 for s in host_source.values() if s == "ip")
                ip24_matches = sum(1 for s in host_source.values() if s == "ip/24")
                name_matches = sum(1 for s in host_source.values() if s == "name")
                translated_matches = sum(1 for s in host_source.values() if s == "translated")

                # Export unmatched
                if export_unmatched and unmatched_names:
                    path = os.path.expanduser(export_unmatched)
                    with open(path, "w") as f:
                        json.dump(unmatched_names, f, indent=2, ensure_ascii=False)

                if dry_run:
                    total_cost = sum(c for _, _, _, c, _ in matches)
                    lines = [
                        f"**DRY RUN — {len(matches)} hosts matched**",
                        f"By IP: {ip_matches} | By /24: {ip24_matches} | By name: {name_matches} | Translated: {translated_matches}",
                        f"Unmatched: {len(unmatched_names)} name entries",
                        f"Skipped: {skipped_range} outside ${min_cost}-${max_cost}",
                        f"**Total: ${total_cost:,.2f}/mo** (${total_cost*12:,.2f}/yr)\n",
                    ]
                    lines.append("| Host | Match | $/mo |")
                    lines.append("|------|-------|------|")
                    for hostname, _hid, _ip, cost, src in sorted(matches, key=lambda x: -x[3])[:25]:
                        lines.append(f"| {hostname} | {src} | ${cost:.2f} |")
                    if len(matches) > 25:
                        lines.append(f"| ... | +{len(matches) - 25} more | |")
                    if export_unmatched and unmatched_names:
                        lines.append(f"\nUnmatched exported to `{export_unmatched}`")
                    lines.append("\nSet `dry_run=false` to apply.")
                    return "\n".join(lines)

                # Apply: set {$COST_MONTH} macros
                existing = await client.call("usermacro.get", {
                    "hostids": [m[1] for m in matches],
                    "output": ["hostmacroid", "hostid", "value"],
                    "filter": {"macro": "{$COST_MONTH}"},
                })
                existing_map = {m["hostid"]: m for m in existing}

                created = updated = unchanged = 0
                errors = []
                for hostname, hid, _ip, cost, _src in matches:
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

                total_cost = sum(c for _, _, _, c, _ in matches)
                parts = [
                    f"**Cost import complete — {len(matches)} servers**",
                    f"By IP: {ip_matches} | By name: {name_matches}",
                    f"Created: {created} | Updated: {updated} | Unchanged: {unchanged}",
                    f"Total: ${total_cost:,.2f}/mo (${total_cost*12:,.2f}/yr)",
                ]
                if unmatched_names:
                    parts.append(f"Unmatched: {len(unmatched_names)} name entries")
                if export_unmatched and unmatched_names:
                    parts.append(f"Exported to `{export_unmatched}`")
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

    if "get_cost_gaps" not in skip:

        @mcp.tool()
        async def get_cost_gaps(
            max_results: int = 30,
            instance: str = "",
        ) -> str:
            """Servers without cost data, grouped by provider and product.

            Args:
                max_results: Max rows (default: 30)
                instance: Zabbix instance name (optional)
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
                        "output": ["hostid"],
                        "filter": {"macro": "{$COST_MONTH}"},
                    }),
                )

                has_cost = {m["hostid"] for m in macros}
                total = len(hosts)
                costed = sum(1 for h in hosts if h["hostid"] in has_cost)

                gaps: dict[str, dict] = {}
                no_ip = 0
                for h in hosts:
                    if h["hostid"] in has_cost:
                        continue
                    ip = host_ip(h)
                    if not ip:
                        no_ip += 1
                        continue
                    prod, tier = _classify_host(h.get("groups", []))
                    prov = detect_provider(ip)
                    key = f"{prov} / {prod}"
                    entry = gaps.setdefault(key, {"count": 0, "hosts": []})
                    entry["count"] += 1
                    if len(entry["hosts"]) < 3:
                        entry["hosts"].append(h["host"])

                lines = [
                    f"**Cost gaps: {total - costed} without cost** ({costed}/{total} covered)\n",
                    "| Provider / Product | Missing | Sample Hosts |",
                    "|-------------------|---------|-------------|",
                ]
                for key in sorted(gaps, key=lambda k: -gaps[k]["count"])[:max_results]:
                    g = gaps[key]
                    lines.append(f"| {key} | {g['count']} | {', '.join(g['hosts'])} |")

                if no_ip:
                    lines.append(f"\n*{no_ip} hosts without IP (monitoring) — no cost expected*")
                return "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

    if "get_cost_efficiency" not in skip:

        @mcp.tool()
        async def get_cost_efficiency(
            max_results: int = 20,
            instance: str = "",
        ) -> str:
            """Cost per Gbps by country and provider — find overpay and waste.

            Args:
                max_results: Max rows (default: 20)
                instance: Zabbix instance name (optional)
            """
            try:
                from zbbx_mcp.classify import detect_provider
                from zbbx_mcp.data import extract_country, fetch_traffic_map

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

                cost_map = {}
                for m in macros:
                    try:
                        cost_map[m["hostid"]] = float(m["value"])
                    except (ValueError, TypeError):
                        pass

                traffic_map = await fetch_traffic_map(client, list(cost_map.keys()))

                by_country: dict[str, dict] = {}
                by_provider: dict[str, dict] = {}
                waste = []

                for hid, cost in cost_map.items():
                    h = next((x for x in hosts if x["hostid"] == hid), None)
                    if not h:
                        continue
                    traffic_mbps = traffic_map.get(hid, 0)
                    traffic_gbps = traffic_mbps / 1000
                    cc = extract_country(h["host"])
                    ip = host_ip(h)
                    prov = detect_provider(ip) if ip else "?"

                    if cc:
                        c = by_country.setdefault(cc, {"servers": 0, "cost": 0, "traffic": 0})
                        c["servers"] += 1
                        c["cost"] += cost
                        c["traffic"] += traffic_gbps

                    p = by_provider.setdefault(prov, {"servers": 0, "cost": 0, "traffic": 0})
                    p["servers"] += 1
                    p["cost"] += cost
                    p["traffic"] += traffic_gbps

                    if traffic_mbps < 1 and cost > 50:
                        waste.append((h["host"], cc, prov, cost))

                lines = [f"**Cost Efficiency** ({len(cost_map)} servers)\n"]
                lines.append("**By Country (most expensive per Gbps first):**")
                lines.append("| Country | Servers | $/mo | Gbps | $/Gbps |")
                lines.append("|---------|---------|------|------|--------|")
                sorted_cc = sorted(by_country.items(), key=lambda x: -(x[1]["cost"] / max(x[1]["traffic"], 0.001)))
                for cc, c in sorted_cc[:max_results]:
                    if c["traffic"] < 0.01:
                        lines.append(f"| {cc} | {c['servers']} | ${c['cost']:,.0f} | idle | N/A |")
                    else:
                        per = c["cost"] / c["traffic"]
                        lines.append(f"| {cc} | {c['servers']} | ${c['cost']:,.0f} | {c['traffic']:.1f} | ${per:,.0f} |")

                lines.append("\n**By Provider:**")
                lines.append("| Provider | Servers | $/mo | Gbps | $/Gbps |")
                lines.append("|----------|---------|------|------|--------|")
                for prov in sorted(by_provider, key=lambda p: -by_provider[p]["cost"])[:15]:
                    p = by_provider[prov]
                    if p["traffic"] < 0.01:
                        lines.append(f"| {prov} | {p['servers']} | ${p['cost']:,.0f} | idle | N/A |")
                    else:
                        per = p["cost"] / p["traffic"]
                        lines.append(f"| {prov} | {p['servers']} | ${p['cost']:,.0f} | {p['traffic']:.1f} | ${per:,.0f} |")

                if waste:
                    waste.sort(key=lambda w: -w[3])
                    total_waste = sum(w[3] for w in waste)
                    lines.append(f"\n**Waste: {len(waste)} servers paying ${total_waste:,.0f}/mo with 0 traffic:**")
                    lines.append("| Server | Country | Provider | $/mo |")
                    lines.append("|--------|---------|----------|------|")
                    for host, cc, prov, cost in waste[:10]:
                        lines.append(f"| {host} | {cc or '?'} | {prov} | ${cost:.0f} |")
                    if len(waste) > 10:
                        lines.append(f"*+{len(waste) - 10} more*")

                return "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"
