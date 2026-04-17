"""Geo-level service health: uptime reports, health matrix, density maps, latency estimates."""

from __future__ import annotations

import asyncio
import math
import time as _time
from statistics import median

import httpx

from zbbx_mcp.classify import classify_host as _classify_host
from zbbx_mcp.classify import resolve_datacenter
from zbbx_mcp.data import (
    CAPITAL_COORDS,
    KEY_service_PRIMARY,
    KEY_service_SECONDARY,
    KEY_service_TERTIARY,
    build_value_map,
    extract_country,
    fetch_cpu_map,
    fetch_enabled_hosts,
    fetch_traffic_map,
    group_by_country,
    host_ip,
)
from zbbx_mcp.resolver import InstanceResolver


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()) -> None:

    if "get_service_uptime_report" not in skip:

        @mcp.tool()
        async def get_service_uptime_report(
            country: str = "",
            product: str = "",
            exclude_product: str = "infrastructure,monitoring",
            only_problems: bool = True,
            max_results: int = 50,
            period: str = "30d",
            instance: str = "",
        ) -> str:
            """Service uptime per server — uptime % over a period.

            Args:
                country: Country code filter (optional)
                product: Product name filter (optional)
                exclude_product: Products to exclude (default: infrastructure,monitoring)
                only_problems: Only DOWN/DEGRADED servers (default: True)
                max_results: Max servers (default: 50)
                period: Analysis period (default: 30d)
                instance: Zabbix instance (optional)
            """
            try:
                client = resolver.resolve(instance)

                hosts = await client.call("host.get", {
                    "output": ["hostid", "host"],
                    "selectGroups": ["name"],
                    "filter": {"status": "0"},
                })

                exclude_set = {p.strip().lower() for p in exclude_product.split(",") if p.strip()} if exclude_product else set()
                filtered = []
                for h in hosts:
                    prod, _ = _classify_host(h.get("groups", []))
                    if product and product.lower() not in (prod or "").lower():
                        continue
                    if exclude_set and any(p in (prod or "").lower() for p in exclude_set):
                        continue
                    if country and extract_country(h["host"]).lower() != country.lower():
                        continue
                    filtered.append(h)

                if not filtered:
                    return "No servers match the filters."

                hostids = [h["hostid"] for h in filtered]

                # Fetch trend data for service check items
                now = int(_time.time())
                from zbbx_mcp.data import _parse_period
                time_from = now - _parse_period(period)

                # Get items for service protocol checks
                service_keys = [k for k in (KEY_service_PRIMARY, KEY_service_SECONDARY) if k]
                if not service_keys:
                    return "No service check keys configured."
                service_items = await client.call("item.get", {
                    "hostids": hostids,
                    "output": ["itemid", "hostid", "key_"],
                    "filter": {"key_": service_keys, "status": "0"},
                })

                if not service_items:
                    return "No service check items found."

                # Fetch trends for service items
                item_ids = [i["itemid"] for i in service_items]
                trends = await client.call("trend.get", {
                    "itemids": item_ids,
                    "time_from": time_from,
                    "output": ["itemid", "value_avg", "num"],
                    "limit": len(item_ids) * 24 * 31,
                })

                # Map itemid -> (hostid, protocol)
                item_info: dict[str, tuple[str, str]] = {}
                for i in service_items:
                    proto = "service1" if KEY_service_PRIMARY and KEY_service_PRIMARY in i["key_"] else "service2"
                    item_info[i["itemid"]] = (i["hostid"], proto)

                # Calculate uptime per host per protocol
                host_uptime: dict[str, dict[str, dict]] = {}  # hostid -> proto -> {up, total}
                for t in trends:
                    info = item_info.get(t["itemid"])
                    if not info:
                        continue
                    hid, proto = info
                    entry = host_uptime.setdefault(hid, {}).setdefault(proto, {"up": 0, "total": 0})
                    entry["total"] += 1
                    try:
                        if float(t["value_avg"]) >= 0.5:
                            entry["up"] += 1
                    except (ValueError, TypeError):
                        pass

                host_map = {h["hostid"]: h["host"] for h in filtered}

                rows = []
                for hid in hostids:
                    hostname = host_map.get(hid, "?")
                    ctry = extract_country(hostname)
                    hu = host_uptime.get(hid, {})

                    service1_data = hu.get("service1", {"up": 0, "total": 0})
                    service2_data = hu.get("service2", {"up": 0, "total": 0})

                    service1_pct = (service1_data["up"] / service1_data["total"] * 100) if service1_data["total"] > 0 else None
                    service2_pct = (service2_data["up"] / service2_data["total"] * 100) if service2_data["total"] > 0 else None

                    overall = "HEALTHY"
                    if service1_pct is not None and service1_pct < 50:
                        overall = "DOWN"
                    elif service1_pct is not None and service1_pct < 90:
                        overall = "DEGRADED"

                    rows.append({
                        "host": hostname, "country": ctry,
                        "service1": service1_pct, "service2": service2_pct,
                        "overall": overall, "hours": service1_data["total"],
                    })

                rows.sort(key=lambda r: (r["service1"] or 100))

                # Filter and limit
                total_all = len(rows)
                healthy_count = sum(1 for r in rows if r["overall"] == "HEALTHY")
                if only_problems:
                    rows = [r for r in rows if r["overall"] != "HEALTHY"]
                shown = rows[:max_results]
                omitted = len(rows) - len(shown)

                parts = [
                    f"**Service Availability ({period}): {total_all} servers ({healthy_count} healthy)**\n",
                    "| Server | Country | service Primary | service Secondary | Status |",
                    "|--------|---------|-------------|-------------|--------|",
                ]
                for r in shown:
                    x = f"{r['service1']:.1f}%" if r["service1"] is not None else "N/A"
                    k = f"{r['service2']:.1f}%" if r["service2"] is not None else "N/A"
                    parts.append(f"| {r['host']} | {r['country']} | {x} | {k} | {r['overall']} |")

                if omitted:
                    parts.append(f"\n*{omitted} more servers omitted*")

                # Country summary
                country_stats: dict[str, list] = {}
                for r in rows:
                    if r["country"]:
                        country_stats.setdefault(r["country"], []).append(r)

                if country_stats:
                    parts.append("\n### Country Summary\n")
                    parts.append("| Country | Servers | Avg service Uptime | DOWN |")
                    parts.append("|---------|---------|-----------------|------|")
                    for ctry in sorted(country_stats):
                        cs = country_stats[ctry]
                        service1_vals = [r["service1"] for r in cs if r["service1"] is not None]
                        avg_x = f"{median(service1_vals):.1f}%" if service1_vals else "N/A"
                        down = sum(1 for r in cs if r["overall"] == "DOWN")
                        parts.append(f"| {ctry} | {len(cs)} | {avg_x} | {down} |")

                return "\n".join(parts)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

    if "get_service_health_matrix" not in skip:

        @mcp.tool()
        async def get_service_health_matrix(
            min_servers: int = 2,
            instance: str = "",
        ) -> str:
            """Service health status matrix by country.

            Args:
                min_servers: Minimum servers per country to include (default: 2)
                instance: Zabbix instance name (optional)
            """
            try:
                client = resolver.resolve(instance)

                hosts = await client.call("host.get", {
                    "output": ["hostid", "host"],
                    "filter": {"status": "0"},
                })

                countries: dict[str, list[dict]] = {}
                for h in hosts:
                    ctry = extract_country(h["host"])
                    if ctry:
                        countries.setdefault(ctry, []).append(h)
                countries = {c: hs for c, hs in countries.items() if len(hs) >= min_servers}

                if not countries:
                    return "No countries with enough servers."

                all_ids = [h["hostid"] for hs in countries.values() for h in hs]

                async def _empty():
                    return []

                service1_items, service2_items, service3_items, traffic_map = await asyncio.gather(
                    client.call("item.get", {
                        "hostids": all_ids, "output": ["hostid", "lastvalue"],
                        "filter": {"key_": KEY_service_PRIMARY, "status": "0"},
                    }) if KEY_service_PRIMARY else _empty(),
                    client.call("item.get", {
                        "hostids": all_ids, "output": ["hostid", "lastvalue"],
                        "filter": {"key_": KEY_service_SECONDARY, "status": "0"},
                    }) if KEY_service_SECONDARY else _empty(),
                    client.call("item.get", {
                        "hostids": all_ids, "output": ["hostid", "lastvalue"],
                        "filter": {"key_": KEY_service_TERTIARY, "status": "0"},
                    }) if KEY_service_TERTIARY else _empty(),
                    fetch_traffic_map(client, all_ids),
                )

                service1_map = build_value_map(service1_items, lambda v: int(float(v)))
                service2_map = build_value_map(service2_items, lambda v: int(float(v)))
                service3_map: dict[str, int] = {}
                for i in service3_items:
                    try:
                        val = int(float(i["lastvalue"]))
                        hid = i["hostid"]
                        if val > service3_map.get(hid, 0):
                            service3_map[hid] = val
                    except (ValueError, TypeError, KeyError):
                        pass

                # Traffic-validation: if server has real traffic, treat as up
                # regardless of check item state (fixes false positives from
                # deprecated check items returning 0)
                TRAFFIC_VALIDATION_MBPS = 5.0
                active_by_traffic = {hid for hid, mbps in traffic_map.items() if mbps >= TRAFFIC_VALIDATION_MBPS}

                parts = [
                    "**Service Health Matrix**\n",
                    "| Country | Servers | Proto 1 | Proto 2 | Proto 3 | Recommendation |",
                    "|---------|---------|------|-------|---------|----------------|",
                ]

                for ctry in sorted(countries):
                    hs = countries[ctry]
                    hids = [h["hostid"] for h in hs]
                    total = len(hids)

                    # "Up" = check returned 1 OR server has real traffic (traffic-validated)
                    service1_up = sum(1 for hid in hids if service1_map.get(hid) == 1 or hid in active_by_traffic)
                    service2_up = sum(1 for hid in hids if service2_map.get(hid) == 1 or hid in active_by_traffic)
                    service3_up = sum(1 for hid in hids if service3_map.get(hid, 0) >= 1 or hid in active_by_traffic)

                    service1_checked = sum(1 for hid in hids if hid in service1_map)
                    service2_checked = sum(1 for hid in hids if hid in service2_map)
                    service3_checked = sum(1 for hid in hids if hid in service3_map)

                    def _status(up: int, checked: int) -> str:
                        if checked == 0:
                            return "N/A"
                        pct = up / checked * 100
                        if pct >= 80:
                            return f"OK ({up}/{checked})"
                        if pct >= 30:
                            return f"PARTIAL ({up}/{checked})"
                        return f"DOWN ({up}/{checked})"

                    x_s = _status(service1_up, service1_checked)
                    k_s = _status(service2_up, service2_checked)
                    o_s = _status(service3_up, service3_checked)

                    # Recommendation
                    working = []
                    if "OK" in x_s or "PARTIAL" in x_s:
                        working.append("Proto 1")
                    if "OK" in k_s or "PARTIAL" in k_s:
                        working.append("Proto 2")
                    if "OK" in o_s or "PARTIAL" in o_s:
                        working.append("Proto 3")

                    if not working:
                        rec = "ALL BLOCKED"
                    elif len(working) == 3:
                        rec = "All protocols OK"
                    else:
                        rec = " / ".join(working) + " only"

                    parts.append(f"| {ctry} | {total} | {x_s} | {k_s} | {o_s} | {rec} |")

                return "\n".join(parts)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

    if "get_regional_density_map" not in skip:

        @mcp.tool()
        async def get_regional_density_map(
            region: str = "ALL",
            country: str = "",
            min_traffic_mbps: float = 0,
            max_results: int = 40,
            instance: str = "",
        ) -> str:
            """Server density by country with traffic, CPU, provider mix.

            Args:
                region: LATAM, APAC, EMEA, NA, CIS, ALL (default: ALL)
                country: Filter by specific country code (optional)
                min_traffic_mbps: Minimum total traffic to include (default: 0)
                max_results: Maximum rows (default: 40)
                instance: Zabbix instance name (optional)
            """
            try:
                client = resolver.resolve(instance)

                hosts = await fetch_enabled_hosts(client)
                by_country = group_by_country(hosts, country=country, region=region)

                if not by_country:
                    return "No servers match the filters."

                all_ids = [h["hostid"] for hs in by_country.values() for h in hs]
                traffic_map, cpu_map = await asyncio.gather(
                    fetch_traffic_map(client, all_ids),
                    fetch_cpu_map(client, all_ids),
                )

                rows = []
                for cc, cc_hosts in by_country.items():
                    total_mbps = sum(traffic_map.get(h["hostid"], 0) for h in cc_hosts)
                    if total_mbps < min_traffic_mbps and min_traffic_mbps > 0:
                        continue
                    cpus = [cpu_map[h["hostid"]] for h in cc_hosts if h["hostid"] in cpu_map]
                    avg_cpu = round(sum(cpus) / len(cpus), 1) if cpus else 0
                    dcs = set()
                    for h in cc_hosts:
                        ip = host_ip(h)
                        if ip:
                            prov, city = resolve_datacenter(ip)
                            dcs.add(city if city and city != "Various" else prov)
                    flag = " **!**" if len(cc_hosts) == 1 else ""
                    rows.append({
                        "cc": cc, "servers": len(cc_hosts), "flag": flag,
                        "traffic_gbps": round(total_mbps / 1000, 2),
                        "avg_cpu": avg_cpu,
                        "dcs": ", ".join(sorted(dcs - {""}))[:40] or "?",
                    })

                rows.sort(key=lambda x: -x["traffic_gbps"])
                shown = rows[:max_results]

                lines = [f"**Density Map** ({len(rows)} countries)\n"]
                lines.append("| Country | Servers | Traffic Gbps | Avg CPU% | Datacenters |")
                lines.append("|---------|---------|-------------|----------|-------------|")
                for r in shown:
                    lines.append(
                        f"| {r['cc']}{r['flag']} | {r['servers']} | {r['traffic_gbps']} | "
                        f"{r['avg_cpu']}% | {r['dcs']} |"
                    )

                no_redundancy = [r for r in rows if r["servers"] == 1]
                if no_redundancy:
                    lines.append(f"\n**No redundancy (1 server):** {', '.join(r['cc'] for r in no_redundancy)}")
                if len(rows) > max_results:
                    lines.append(f"\n*{len(rows) - max_results} more countries omitted*")

                return "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"


    if "get_latency_estimate" not in skip:

        @mcp.tool()
        async def get_latency_estimate(
            client_country: str = "",
            product: str = "",
            max_results: int = 10,
            instance: str = "",
        ) -> str:
            """Estimate nearest server by geographic distance from client country.

            Args:
                client_country: 2-letter country code (required)
                product: Product name filter (optional)
                max_results: Max results (default: 10)
                instance: Zabbix instance (optional)
            """
            def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
                R = 6371
                dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
                a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
                return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

            if not client_country:
                return "client_country is required (2-letter code, e.g. 'CO')."

            cc = client_country.upper()
            if cc not in CAPITAL_COORDS:
                return f"Unknown country '{cc}'. No coordinates available."

            try:
                client_inst = resolver.resolve(instance)
                hosts = await client_inst.call("host.get", {
                    "output": ["hostid", "host"],
                    "selectGroups": ["name"],
                    "filter": {"status": "0"},
                })

                server_countries: dict[str, int] = {}
                for h in hosts:
                    if product:
                        prod, _ = _classify_host(h.get("groups", []))
                        if not prod or product.lower() not in prod.lower():
                            continue
                    srv_cc = extract_country(h["host"])
                    if srv_cc:
                        server_countries[srv_cc] = server_countries.get(srv_cc, 0) + 1

                if not server_countries:
                    return "No servers found."

                client_lat, client_lon = CAPITAL_COORDS[cc]
                distances = []
                for srv_cc, count in server_countries.items():
                    if srv_cc in CAPITAL_COORDS:
                        srv_lat, srv_lon = CAPITAL_COORDS[srv_cc]
                        dist = _haversine(client_lat, client_lon, srv_lat, srv_lon)
                        distances.append({"cc": srv_cc, "servers": count, "km": round(dist)})

                distances.sort(key=lambda x: x["km"])
                shown = distances[:max_results]

                lines = [f"**Nearest servers for clients in {cc}**\n"]
                lines.append("| Server Country | Servers | Distance km |")
                lines.append("|---------------|---------|------------|")
                for d in shown:
                    marker = " *" if d["cc"] == cc else ""
                    lines.append(f"| {d['cc']}{marker} | {d['servers']} | {d['km']:,} |")

                if cc not in server_countries:
                    nearest = distances[0] if distances else None
                    if nearest:
                        lines.append(f"\n**No servers in {cc}.** Nearest: {nearest['cc']} ({nearest['km']:,} km)")

                return "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

    if "get_servers_by_ping" not in skip:

        @mcp.tool()
        async def get_servers_by_ping(
            client_country: str = "",
            product: str = "",
            max_results: int = 20,
            instance: str = "",
        ) -> str:
            """List servers sorted by estimated latency from a client country.

            Args:
                client_country: 2-letter country code (required)
                product: Product name filter (optional)
                max_results: Max servers (default: 20)
                instance: Zabbix instance (optional)
            """
            def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
                R = 6371
                dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
                a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
                return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

            if not client_country:
                return "client_country is required (2-letter code)."

            cc = client_country.upper()
            if cc not in CAPITAL_COORDS:
                return f"Unknown country '{cc}'."

            try:
                client_inst = resolver.resolve(instance)
                hosts = await fetch_enabled_hosts(client_inst)
                all_ids = [h["hostid"] for h in hosts]
                traffic_map = await fetch_traffic_map(client_inst, all_ids)

                client_lat, client_lon = CAPITAL_COORDS[cc]
                servers = []
                for h in hosts:
                    if product:
                        prod, _ = _classify_host(h.get("groups", []))
                        if not prod or product.lower() not in prod.lower():
                            continue
                    srv_cc = extract_country(h["host"])
                    if not srv_cc or srv_cc not in CAPITAL_COORDS:
                        continue
                    srv_lat, srv_lon = CAPITAL_COORDS[srv_cc]
                    dist = _haversine(client_lat, client_lon, srv_lat, srv_lon)
                    est_ms = round(dist * 6 / 1000)
                    traffic = traffic_map.get(h["hostid"], 0)
                    servers.append({
                        "host": h["host"], "cc": srv_cc,
                        "km": round(dist), "ms": est_ms,
                        "traffic": round(traffic, 1),
                    })

                servers.sort(key=lambda x: x["km"])
                shown = servers[:max_results]

                if not shown:
                    return "No servers found."

                lines = [f"**Nearest servers for {cc}** ({len(servers)} total)\n"]
                lines.append("| Server | Country | Distance | Est. Latency | Traffic Mbps |")
                lines.append("|--------|---------|----------|-------------|-------------|")
                for s in shown:
                    lines.append(
                        f"| {s['host']} | {s['cc']} | {s['km']:,} km | ~{s['ms']} ms | {s['traffic']} |"
                    )
                if len(servers) > max_results:
                    lines.append(f"\n*{len(servers) - max_results} more omitted*")

                return "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"
