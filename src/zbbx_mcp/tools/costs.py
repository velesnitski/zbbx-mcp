"""Server cost management via {$COST_MONTH} host macros."""

import asyncio
import csv as _csv
import fnmatch
import json
import os
import re

import httpx

from zbbx_mcp.data import host_ip
from zbbx_mcp.resolver import InstanceResolver
from zbbx_mcp.utils import resolve_group_ids

_IP_RE = re.compile(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b")


def _load_billing_csv(path: str) -> list[dict]:
    """Read billing CSV with ip + billing_name + price_monthly columns.

    Tolerates column aliases (ip/ipaddress, name/billing_name/hostname,
    price/price_monthly/cost). Returns list of {ip, name, price} dicts.
    Skips invalid/reserved IPs and zero/negative prices.
    """
    rows: list[dict] = []
    with open(os.path.expanduser(path)) as f:
        reader = _csv.DictReader(f)
        for raw in reader:
            # Normalize headers
            norm = {k.strip().lower(): (v or "").strip() for k, v in raw.items() if k}
            ip = norm.get("ip") or norm.get("ipaddress") or norm.get("ip_address") or ""
            name = norm.get("billing_name") or norm.get("name") or norm.get("hostname") or ""
            price_raw = (
                norm.get("price_monthly") or norm.get("price")
                or norm.get("cost") or norm.get("cost_month") or ""
            )
            try:
                price = float(price_raw)
            except (ValueError, TypeError):
                continue
            if price <= 0 or not ip:
                continue
            # Skip reserved / bogus IPs
            octets = ip.split(".")
            if len(octets) != 4:
                continue
            try:
                a, b = int(octets[0]), int(octets[1])
            except ValueError:
                continue
            if a in (0, 10, 127, 169, 172, 192, 224, 255) and (a != 172 or 16 <= b <= 31):
                # Allow most, skip only clearly-reserved
                if a in (0, 127, 224, 255):
                    continue
            rows.append({"ip": ip, "name": name, "price": price})
    return rows


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()) -> None:

    if "import_server_costs" not in skip:

        @mcp.tool()
        async def import_server_costs(
            costs_json: str,
            only_if_empty: bool = False,
            instance: str = "",
        ) -> str:
            """Bulk-set monthly costs on servers using {$COST_MONTH} host macros.

            Args:
                costs_json: JSON mapping hostname patterns to USD cost (e.g. {"srv-nl-*": 20})
                only_if_empty: Skip hosts that already have a non-zero {$COST_MONTH} set
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
                            existing_val = (existing_map[hid].get("value") or "").strip()
                            if only_if_empty and existing_val not in ("", "0", "0.0", "0.00"):
                                skipped += 1
                                continue
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
            only_if_empty: bool = False,
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
                only_if_empty: Skip hosts that already have a non-zero {$COST_MONTH} set
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

            def _extract_price(v):
                """Extract numeric price from value (may be dict with price/cost field or number)."""
                if isinstance(v, dict):
                    for key in ("price", "cost", "price_monthly", "monthly"):
                        if key in v:
                            return v[key]
                    return None
                return v

            def _in_range(v):
                p = _extract_price(v)
                if p is None:
                    return False
                try:
                    return min_cost <= float(p) <= max_cost
                except (ValueError, TypeError):
                    return False

            safe_ips = {k: float(_extract_price(v)) for k, v in ip_costs.items() if _in_range(v)}
            safe_names = {k: float(_extract_price(v)) for k, v in name_costs.items() if _in_range(v)}

            # If IP values are dicts with 'name' field, also populate safe_names for fuzzy matching
            for _ip, v in ip_costs.items():
                if isinstance(v, dict) and _in_range(v):
                    name = (v.get("name") or "").strip()
                    if name and name not in safe_names:
                        safe_names[name] = float(_extract_price(v))
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

                # Filter out hosts with an existing non-zero cost if only_if_empty
                skipped_existing = 0
                if only_if_empty and host_costs:
                    existing_pre = await client.call("usermacro.get", {
                        "hostids": list(host_costs.keys()),
                        "output": ["hostid", "value"],
                        "filter": {"macro": "{$COST_MONTH}"},
                    })
                    nonempty = {
                        m["hostid"] for m in existing_pre
                        if (m.get("value") or "").strip() not in ("", "0", "0.0", "0.00")
                    }
                    skipped_existing = len(nonempty)
                    for hid in nonempty:
                        host_costs.pop(hid, None)
                        host_source.pop(hid, None)

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
                    ]
                    if only_if_empty:
                        lines.append(f"Skipped (already costed): {skipped_existing}")
                    lines.append(f"**Total: ${total_cost:,.2f}/mo** (${total_cost*12:,.2f}/yr)\n")
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

    if "import_cluster_ip_fees" not in skip:

        @mcp.tool()
        async def import_cluster_ip_fees(
            file_path: str = "",
            fees_json: str = "",
            dry_run: bool = True,
            instance: str = "",
        ) -> str:
            """Add extra-IP billing fees to cluster primary {$COST_MONTH} macros.

            For each entry, finds the Zabbix host by name, reads the existing
            {$COST_MONTH}, adds the extra_cost_month, and writes back with an
            audit description ("base X + N extra IPs (Y)").

            Input format: list of {"cluster": str, "extra_ips": [str], "extra_cost_month": float}

            Args:
                file_path: Path to JSON file with cluster fee entries (optional)
                fees_json: Inline JSON (optional; use this OR file_path)
                dry_run: Preview changes without applying (default: True)
                instance: Zabbix instance (optional)
            """
            try:
                client = resolver.resolve(instance)

                # Parse input
                if file_path:
                    with open(os.path.expanduser(file_path)) as f:
                        data = json.load(f)
                elif fees_json:
                    data = json.loads(fees_json)
                else:
                    return "Provide either file_path or fees_json."

                if not isinstance(data, list):
                    return "Expected JSON array of {cluster, extra_ips, extra_cost_month}."

                # Batch lookup: all cluster names in one host.get call
                host_names = [e.get("cluster", "") for e in data if e.get("cluster")]
                if not host_names:
                    return "No cluster names in input."

                hosts = await client.call("host.get", {
                    "output": ["hostid", "host"],
                    "filter": {"host": host_names},
                    "selectMacros": ["hostmacroid", "macro", "value"],
                })
                host_by_name = {h["host"]: h for h in hosts}

                # Build update plan
                updates = []
                missing = []
                for entry in data:
                    name = entry.get("cluster", "")
                    extras = float(entry.get("extra_cost_month", 0))
                    ip_count = len(entry.get("extra_ips", []))
                    if not name or extras <= 0:
                        continue
                    h = host_by_name.get(name)
                    if not h:
                        missing.append(name)
                        continue

                    existing_macro = None
                    current = 0.0
                    for m in h.get("macros", []):
                        if m["macro"] == "{$COST_MONTH}":
                            existing_macro = m
                            try:
                                current = float(m["value"])
                            except (ValueError, TypeError):
                                current = 0.0
                            break

                    new_val = round(current + extras, 2)
                    desc = f"base {current:.2f} + {ip_count} extra IP{'s' if ip_count > 1 else ''} ({extras:.2f})"
                    updates.append({
                        "hostid": h["hostid"],
                        "host": name,
                        "macroid": existing_macro["hostmacroid"] if existing_macro else None,
                        "current": current,
                        "extras": extras,
                        "new": new_val,
                        "desc": desc,
                    })

                # Build response
                total_delta = sum(u["extras"] for u in updates)
                header = f"**Cluster IP fees import** — {len(updates)} clusters, ${total_delta:,.2f}/mo added"
                if missing:
                    header += f" ({len(missing)} missing)"

                lines = [header + ("  DRY RUN" if dry_run else ""), ""]
                lines.append("| Cluster | Current | +Extras | New | IPs |")
                lines.append("|---------|---------|---------|-----|-----|")
                for u in sorted(updates, key=lambda x: -x["extras"])[:25]:
                    ip_count = u["desc"].split()[3]
                    lines.append(f"| {u['host']} | ${u['current']:.2f} | ${u['extras']:.2f} | ${u['new']:.2f} | {ip_count} |")
                if len(updates) > 25:
                    lines.append(f"*+{len(updates) - 25} more clusters*")
                if missing:
                    lines.append(f"\n**Missing (not found in Zabbix):** {', '.join(missing[:10])}")

                if dry_run:
                    lines.append("\n*Set dry_run=false to apply.*")
                    return "\n".join(lines)

                # Apply — parallel usermacro update/create
                async def _apply(u):
                    if u["macroid"]:
                        return await client.call("usermacro.update", {
                            "hostmacroid": u["macroid"],
                            "value": str(u["new"]),
                            "description": u["desc"],
                        })
                    return await client.call("usermacro.create", {
                        "hostid": u["hostid"],
                        "macro": "{$COST_MONTH}",
                        "value": str(u["new"]),
                        "description": u["desc"],
                    })

                results = await asyncio.gather(*[_apply(u) for u in updates], return_exceptions=True)
                errors = [str(r)[:80] for r in results if isinstance(r, Exception)]
                applied = sum(1 for r in results if not isinstance(r, Exception))

                lines.append(f"\n**Applied: {applied}/{len(updates)}**")
                if errors:
                    lines.append(f"Errors: {len(errors)}")
                    for e in errors[:3]:
                        lines.append(f"  - {e}")
                return "\n".join(lines)
            except (httpx.HTTPError, ValueError, OSError) as e:
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

    if "analyze_cost_import" not in skip:

        @mcp.tool()
        async def analyze_cost_import(
            file_path: str,
            output_csv: str = "",
            output_json: str = "",
            instance: str = "",
        ) -> str:
            """Tiered probability analysis of unmatched cost entries.

            Reads a JSON file of {ip: price} entries, finds which don't match
            Zabbix hosts by IP, then scores each with signals:
            - /24 subnet match (40pt)
            - Billing name fuzzy match (35pt strong / 15pt partial)
            - Known provider CIDR (15pt)

            Tiers: HIGH (>=70), MEDIUM (>=40), LOW (>=15), UNKNOWN (<15).
            Writes CSV + JSON for human review.

            Args:
                file_path: Path to JSON cost file {ip: price} or {ip: {name, price, ...}}
                output_csv: Path for CSV output (default: ~/Downloads/cost_import_analysis.csv)
                output_json: Path for JSON output (default: ~/Downloads/cost_import_analysis.json)
                instance: Zabbix instance name (optional)
            """
            from collections import defaultdict

            from zbbx_mcp.classify import detect_provider

            try:
                path = os.path.expanduser(file_path)
                if not os.path.isfile(path):
                    return f"File not found: {path}"
                with open(path) as f:
                    raw = json.load(f)

                # Normalize input: accept both {ip: price} and {ip: {name, price, ...}}
                costs: dict[str, dict] = {}
                for ip, val in raw.items():
                    if isinstance(val, dict):
                        price = val.get("price") or val.get("cost") or 0
                        name = val.get("name", "")
                    else:
                        try:
                            price = float(val)
                            name = ""
                        except (ValueError, TypeError):
                            continue
                    if price > 0:
                        costs[ip.strip()] = {"name": str(name), "price": float(price)}

                if not costs:
                    return "No valid cost entries found in file."

                client = resolver.resolve(instance)
                hosts = await client.call("host.get", {
                    "output": ["hostid", "host", "name"],
                    "selectInterfaces": ["ip"],
                })

                zabbix_ips: dict[str, str] = {}
                zabbix_by_24: dict[str, list] = defaultdict(list)
                zabbix_names: list[str] = []
                for h in hosts:
                    zabbix_names.append(h["host"].lower())
                    for iface in h.get("interfaces", []):
                        ip = iface.get("ip", "")
                        if ip and ip != "127.0.0.1":
                            zabbix_ips[ip] = h["host"]
                            parts = ip.split(".")
                            if len(parts) == 4:
                                zabbix_by_24[f"{parts[0]}.{parts[1]}.{parts[2]}"].append((ip, h["host"]))

                def _tokens(s):
                    if not s:
                        return []
                    tokens = re.findall(r"[a-z]{2,}\d*|\d{2,}", s.lower())
                    stop = {"dedicated", "server", "custom", "dual", "standard", "gold", "price", "incl", "excl", "taxes"}
                    return [t for t in tokens if t not in stop and len(t) >= 2]

                results = []
                _PRIV = ("10.", "192.168.", "172.16.", "172.17.", "172.18.", "172.19.",
                         "172.20.", "172.21.", "172.22.", "172.23.", "172.24.", "172.25.",
                         "172.26.", "172.27.", "172.28.", "172.29.", "172.30.", "172.31.")

                for ip, info in costs.items():
                    if ip.startswith(_PRIV):
                        continue
                    if ip in zabbix_ips:
                        continue  # already matched

                    score = 0
                    signals = []

                    parts = ip.split(".")
                    if len(parts) == 4:
                        subnet = zabbix_by_24.get(f"{parts[0]}.{parts[1]}.{parts[2]}", [])
                        if subnet:
                            score += 40
                            signals.append(f"/24 match: {subnet[0][1]} (+{len(subnet) - 1} more)")

                    name_tokens = _tokens(info["name"])
                    matched_hosts = []
                    if name_tokens:
                        for hn in zabbix_names:
                            if all(t in hn for t in name_tokens[:2]):
                                matched_hosts.append(hn)
                                if len(matched_hosts) >= 3:
                                    break
                        if matched_hosts:
                            score += 35
                            signals.append(f"name match: {', '.join(matched_hosts[:2])}")
                        else:
                            for hn in zabbix_names:
                                for t in name_tokens:
                                    if len(t) >= 4 and t in hn:
                                        matched_hosts.append(hn)
                                        break
                                if len(matched_hosts) >= 2:
                                    break
                            if matched_hosts:
                                score += 15
                                signals.append(f"partial name: {matched_hosts[0]}")

                    prov = detect_provider(ip)
                    if prov not in ("Unknown", "Other"):
                        score += 15
                        signals.append(f"provider: {prov}")

                    if score >= 70:
                        tier = "HIGH"
                        sug = "Likely same server (different IP or hostname) — verify then add or update"
                    elif score >= 40:
                        tier = "MEDIUM"
                        sug = "Possible match — review subnet/name before importing"
                    elif score >= 15:
                        tier = "LOW"
                        sug = "Weak signal — new server not onboarded, or decommissioned"
                    else:
                        tier = "UNKNOWN"
                        sug = "No signal — external infra, abandoned, or needs manual investigation"

                    results.append({
                        "ip": ip, "name": info["name"], "price_monthly": info["price"],
                        "tier": tier, "confidence": score, "signals": signals, "suggestion": sug,
                    })

                tier_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "UNKNOWN": 3}
                results.sort(key=lambda x: (tier_order[x["tier"]], -x["price_monthly"]))

                by_tier: dict[str, dict] = defaultdict(lambda: {"count": 0, "cost": 0.0})
                for r in results:
                    by_tier[r["tier"]]["count"] += 1
                    by_tier[r["tier"]]["cost"] += r["price_monthly"]

                downloads = os.path.expanduser("~/Downloads")
                out_json = os.path.expanduser(output_json) if output_json else os.path.join(downloads, "cost_import_analysis.json")
                out_csv = os.path.expanduser(output_csv) if output_csv else os.path.join(downloads, "cost_import_analysis.csv")

                with open(out_json, "w") as f:
                    json.dump(results, f, indent=2)

                import csv as _csv
                with open(out_csv, "w", newline="") as f:
                    w = _csv.writer(f)
                    w.writerow(["Tier", "Confidence", "IP", "Billing Name", "Monthly $",
                                "Annual $", "Signals", "Suggestion"])
                    for r in results:
                        w.writerow([r["tier"], r["confidence"], r["ip"], r["name"],
                                    f"{r['price_monthly']:.2f}",
                                    f"{r['price_monthly'] * 12:.2f}",
                                    " | ".join(r["signals"]), r["suggestion"]])

                total_unmatched = sum(r["price_monthly"] for r in results)
                lines = [
                    f"**Cost Import Analysis: {len(results)} unmatched IPs = ${total_unmatched:,.2f}/mo**\n",
                    "| Tier | Count | Monthly $ | Annual $ |",
                    "|------|-------|-----------|----------|",
                ]
                for t in ["HIGH", "MEDIUM", "LOW", "UNKNOWN"]:
                    d = by_tier[t]
                    lines.append(f"| {t} | {d['count']} | ${d['cost']:,.2f} | ${d['cost'] * 12:,.2f} |")
                lines.append(f"\nSaved: `{out_json}`")
                lines.append(f"Saved: `{out_csv}`")

                return "\n".join(lines)
            except (httpx.HTTPError, ValueError, OSError) as e:
                return f"Error: {e}"

    if "reconcile_billing_audit" not in skip:

        @mcp.tool()
        async def reconcile_billing_audit(
            file_path: str,
            output_dir: str = "",
            instance: str = "",
        ) -> str:
            """Categorize billing entries into finance action buckets.

            Buckets: importable, already_costed, stale_ip, subnet_match, onboard, cancel.

            Args:
                file_path: CSV with ip, billing_name, price_monthly columns
                output_dir: Directory to write per-bucket CSVs (default: same as input)
                instance: Zabbix instance (optional)
            """
            try:
                rows = _load_billing_csv(file_path)
            except (FileNotFoundError, OSError) as e:
                return f"Failed to read CSV: {e}"
            if not rows:
                return "No valid billing rows found."

            try:
                client = resolver.resolve(instance)
                hosts = await client.call("host.get", {
                    "output": ["hostid", "host"],
                    "selectInterfaces": ["ip"],
                    "filter": {"status": "0"},
                })
                macros = await client.call("usermacro.get", {
                    "output": ["hostid", "value"],
                    "filter": {"macro": "{$COST_MONTH}"},
                })
                has_cost = {m["hostid"] for m in macros if m.get("value") and m["value"] != "0"}
            except httpx.HTTPError as e:
                return f"Zabbix error: {e}"

            ip_to_host: dict[str, dict] = {}
            subnet_to_host: dict[str, list] = {}
            name_to_host: dict[str, dict] = {}
            for h in hosts:
                hip = host_ip(h)
                if hip:
                    ip_to_host[hip] = h
                    subnet_to_host.setdefault(".".join(hip.split(".")[:3]), []).append(h)
                name_to_host[h["host"].lower()] = h

            buckets: dict[str, list] = {
                "importable": [], "already_costed": [], "stale_ip": [],
                "subnet_match": [], "onboard": [], "cancel": [],
            }

            for r in rows:
                ip, name = r["ip"], r["name"]
                h = ip_to_host.get(ip)
                if h:
                    entry = {**r, "zabbix_host": h["host"]}
                    buckets["already_costed" if h["hostid"] in has_cost else "importable"].append(entry)
                    continue
                low = name.lower()
                matched = None
                if low:
                    for key, hh in name_to_host.items():
                        first = low.split()[0] if low else ""
                        if key == low or (first and key == first):
                            matched = hh
                            break
                if matched:
                    buckets["stale_ip"].append({
                        **r, "zabbix_host": matched["host"], "zabbix_ip": host_ip(matched),
                    })
                    continue
                subnet = ".".join(ip.split(".")[:3])
                if subnet in subnet_to_host:
                    sample = subnet_to_host[subnet][0]
                    buckets["subnet_match"].append({
                        **r, "zabbix_host": sample["host"], "zabbix_ip": host_ip(sample),
                    })
                    continue
                if name and any(c.isalpha() for c in name) and "." not in name[:5]:
                    buckets["onboard"].append(r)
                else:
                    buckets["cancel"].append(r)

            if not output_dir:
                output_dir = os.path.dirname(os.path.expanduser(file_path)) or os.getcwd()
            base = os.path.splitext(os.path.basename(file_path))[0]
            written = []
            for bucket, items in buckets.items():
                if not items:
                    continue
                out = os.path.join(output_dir, f"{base}__{bucket}.csv")
                cols = sorted({k for it in items for k in it})
                with open(out, "w", newline="") as f:
                    w = _csv.DictWriter(f, fieldnames=cols)
                    w.writeheader()
                    w.writerows(items)
                written.append(out)

            actions = {
                "importable": "Safe to import (IP match, no cost)",
                "already_costed": "Skip — cost already set",
                "stale_ip": "Billing team: update IP",
                "subnet_match": "Likely new member — review",
                "onboard": "Ops: add host to Zabbix",
                "cancel": "Finance: cancel or investigate",
            }
            lines = [f"**Reconciliation: {len(rows)} billing rows**\n",
                     "| Bucket | Count | $/mo | Action |",
                     "|--------|------:|-----:|--------|"]
            for b, items in buckets.items():
                if not items:
                    continue
                total = sum(i["price"] for i in items)
                lines.append(f"| {b} | {len(items)} | ${total:,.2f} | {actions[b]} |")
            lines.append(f"\nWrote {len(written)} bucket CSVs to `{output_dir}`")
            return "\n".join(lines)

    if "find_stale_billing_ips" not in skip:

        @mcp.tool()
        async def find_stale_billing_ips(
            file_path: str,
            instance: str = "",
        ) -> str:
            """Detect billing entries where name matches a Zabbix host but IP differs.

            Args:
                file_path: CSV with ip, billing_name, price_monthly columns
                instance: Zabbix instance (optional)
            """
            try:
                rows = _load_billing_csv(file_path)
            except (FileNotFoundError, OSError) as e:
                return f"Failed to read CSV: {e}"
            if not rows:
                return "No valid billing rows."

            try:
                client = resolver.resolve(instance)
                hosts = await client.call("host.get", {
                    "output": ["hostid", "host"],
                    "selectInterfaces": ["ip"],
                    "filter": {"status": "0"},
                })
            except httpx.HTTPError as e:
                return f"Zabbix error: {e}"

            name_to_host: dict[str, dict] = {}
            ip_to_host: dict[str, dict] = {}
            for h in hosts:
                name_to_host[h["host"].lower()] = h
                hip = host_ip(h)
                if hip:
                    ip_to_host[hip] = h

            stale: list[dict] = []
            for r in rows:
                ip, name = r["ip"], r["name"].lower()
                if not name or ip in ip_to_host:
                    continue
                matched = name_to_host.get(name)
                if not matched and name:
                    first = name.split()[0]
                    matched = name_to_host.get(first)
                if matched:
                    zip_ = host_ip(matched)
                    if zip_ and zip_ != ip:
                        stale.append({
                            "billing_name": r["name"],
                            "billing_ip": ip,
                            "zabbix_host": matched["host"],
                            "zabbix_ip": zip_,
                            "price_monthly": r["price"],
                        })

            if not stale:
                return "No stale billing IPs detected."

            total = sum(s["price_monthly"] for s in stale)
            lines = [f"**{len(stale)} stale billing IPs** (${total:,.2f}/mo affected)\n",
                     "| Billing name | Billing IP | Zabbix IP | $/mo |",
                     "|-------------|-----------|-----------|-----:|"]
            for s in sorted(stale, key=lambda x: -x["price_monthly"])[:50]:
                lines.append(
                    f"| {s['billing_name']} | {s['billing_ip']} | {s['zabbix_ip']} | ${s['price_monthly']:.2f} |"
                )
            if len(stale) > 50:
                lines.append(f"\n*+{len(stale) - 50} more*")
            return "\n".join(lines)
