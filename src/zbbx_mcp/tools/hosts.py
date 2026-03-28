import re
from collections import defaultdict

import httpx

from zbbx_mcp.classify import classify_host as _classify_host
from zbbx_mcp.data import (
    extract_country,
    fetch_enabled_hosts,
    fetch_traffic_map,
    host_ip,
)
from zbbx_mcp.formatters import format_host_detail, format_host_list
from zbbx_mcp.resolver import InstanceResolver


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()) -> None:

    if "search_hosts" not in skip:

        @mcp.tool()
        async def search_hosts(
            query: str = "",
            group: str = "",
            country: str = "",
            max_results: int = 50,
            format: str = "table",
            instance: str = "",
        ) -> str:
            """Search Zabbix hosts by name pattern, host group, or country.

            Args:
                query: Host name substring search
                group: Host group name filter
                country: 2-letter country code filter
                max_results: Max results (default: 50)
                format: 'table' (default) or 'list'
                instance: Zabbix instance (optional)
            """
            try:
                client = resolver.resolve(instance)
                params = {
                    "output": ["hostid", "host", "name", "status", "available"],
                    "selectInterfaces": ["ip"],
                    "selectGroups": ["name"],
                    "sortfield": "host",
                }
                if query:
                    q = query if "*" in query else f"*{query}*"
                    params["search"] = {"host": q, "name": q}
                    params["searchWildcardsEnabled"] = True
                    params["searchByAny"] = True
                if group:
                    groups = await client.call("hostgroup.get", {
                        "output": ["groupid"],
                        "filter": {"name": [group]},
                    })
                    if groups:
                        params["groupids"] = [g["groupid"] for g in groups]
                    else:
                        return f"Host group '{group}' not found."

                if not country:
                    params["limit"] = max_results

                data = await client.call("host.get", params)

                if country:
                    data = [h for h in data if extract_country(h["host"]).lower() == country.lower()]

                if not data:
                    return "No hosts found."

                total = len(data)
                data = data[:max_results]
                header = f"**Found: {total} hosts**"
                if total > max_results:
                    header += f" (showing first {max_results})"

                if format == "table":
                    lines = ["| Host | Name | Host ID | Status | IP |",
                             "|------|------|---------|--------|----|"]
                    for h in data:
                        status = "Enabled" if h.get("status") == "0" else "Disabled"
                        ip = next((i["ip"] for i in h.get("interfaces", []) if i.get("ip") != "127.0.0.1"), "")
                        lines.append(f"| {h.get('host', '?')} | {h.get('name', '')} | {h.get('hostid', '?')} | {status} | {ip} |")
                    return f"{header}\n\n" + "\n".join(lines)
                else:
                    result = format_host_list(data)
                    return f"{header}\n\n{result}"
            except (httpx.HTTPError, ValueError) as e:
                return f"Error querying Zabbix: {e}"

    if "create_host" not in skip:

        @mcp.tool()
        async def create_host(
            host: str,
            group_ids: str,
            name: str = "",
            ip: str = "",
            dns: str = "",
            port: str = "10050",
            template_ids: str = "",
            description: str = "",
            instance: str = "",
        ) -> str:
            """Create a new Zabbix host.

            Args:
                host: Technical host name (must be unique)
                group_ids: Comma-separated host group IDs (required)
                name: Visible name (optional, defaults to host)
                ip: IP address for agent interface (optional)
                dns: DNS name for agent interface (optional)
                port: Agent port (default: 10050)
                template_ids: Comma-separated template IDs to link (optional)
                description: Host description (optional)
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)
                params = {
                    "host": host,
                    "groups": [{"groupid": g.strip()} for g in group_ids.split(",")],
                }
                if name:
                    params["name"] = name
                if description:
                    params["description"] = description
                if template_ids:
                    params["templates"] = [{"templateid": t.strip()} for t in template_ids.split(",")]

                if ip or dns:
                    params["interfaces"] = [{
                        "type": 1,
                        "main": 1,
                        "useip": 1 if ip else 0,
                        "ip": ip or "",
                        "dns": dns or "",
                        "port": port,
                    }]

                result = await client.call("host.create", params)
                hid = result.get("hostids", ["?"])[0]
                client.record_create("host", hid, f"Created host '{host}'")
                return f"Host created. ID: {hid}"
            except (httpx.HTTPError, ValueError) as e:
                return f"Error creating host: {e}"

    if "update_host" not in skip:

        @mcp.tool()
        async def update_host(
            host_id: str,
            name: str = "",
            status: int = -1,
            description: str = "",
            instance: str = "",
        ) -> str:
            """Update an existing Zabbix host.

            Args:
                host_id: Host ID to update
                name: New visible name (optional)
                status: 0=enabled, 1=disabled (optional, -1 to skip)
                description: New description (optional)
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)
                await client.snapshot_and_record("update", "host", host_id, f"Updated host {host_id}")

                params = {"hostid": host_id}
                if name:
                    params["name"] = name
                if status >= 0:
                    params["status"] = status
                if description:
                    params["description"] = description

                await client.call("host.update", params)
                return f"Host {host_id} updated."
            except (httpx.HTTPError, ValueError) as e:
                return f"Error updating host: {e}"

    if "delete_host" not in skip:

        @mcp.tool()
        async def delete_host(host_id: str, instance: str = "") -> str:
            """Delete a Zabbix host.

            Args:
                host_id: Host ID to delete
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)
                await client.snapshot_and_record("delete", "host", host_id, f"Deleted host {host_id}")
                await client.call("host.delete", [host_id])
                return f"Host {host_id} deleted."
            except (httpx.HTTPError, ValueError) as e:
                return f"Error deleting host: {e}"

    if "get_host" not in skip:

        @mcp.tool()
        async def get_host(host_id: str, instance: str = "") -> str:
            """Get full details of a specific Zabbix host.

            Args:
                host_id: Zabbix host ID or hostname (auto-resolved)
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)

                # Resolve hostname to ID if not numeric
                if not host_id.isdigit():
                    lookup = await client.call("host.get", {
                        "output": ["hostid"],
                        "filter": {"host": [host_id]},
                    })
                    if not lookup:
                        lookup = await client.call("host.get", {
                            "output": ["hostid"],
                            "search": {"host": host_id, "name": host_id},
                            "searchByAny": True, "searchWildcardsEnabled": True,
                            "limit": 1,
                        })
                    if not lookup:
                        return f"Host '{host_id}' not found."
                    host_id = lookup[0]["hostid"]

                data = await client.call("host.get", {
                    "hostids": [host_id],
                    "output": "extend",
                    "selectGroups": ["name"],
                    "selectInterfaces": ["type", "ip", "port"],
                })

                if not data:
                    return f"Host '{host_id}' not found."
                return format_host_detail(data[0])
            except (httpx.HTTPError, ValueError) as e:
                return f"Error querying Zabbix: {e}"

    
    _CLUSTER_RE = re.compile(r"^(.+?\d+)(?:\s+\w+\d+)+$")

    def _parse_cluster_base(hostname: str) -> str:
        """Extract base host from cluster name.

        'srv-us175 us177' → 'srv-us175'
        'srv-us175' → 'srv-us175'
        """
        return hostname.split()[0]

    def _infer_cluster_role(hostname: str) -> str:
        """Infer role from hostname pattern.

        Base host (no space-separated suffix) → primary.
        Host with suffix in visible name → secondary.
        """
        parts = hostname.strip().split()
        return "primary" if len(parts) == 1 else "secondary"

    if "get_server_clusters" not in skip:

        @mcp.tool()
        async def get_server_clusters(
            group: str = "",
            country: str = "",
            max_results: int = 30,
            instance: str = "",
        ) -> str:
            """Detect server clusters from naming patterns.

            Hosts sharing a base name form a cluster (e.g. 'srv-us1', 'srv-us1 us2').
            Groups them and infers primary/secondary roles.

            Args:
                group: Filter by host group name (optional)
                country: Filter by 2-letter country code (optional)
                max_results: Maximum clusters to show (default: 30)
                instance: Zabbix instance name (optional)
            """
            try:
                client = resolver.resolve(instance)
                params = {
                    "output": ["hostid", "host", "name", "status"],
                    "selectGroups": ["name"],
                    "selectInterfaces": ["ip"],
                    "filter": {"status": "0"},
                    "sortfield": "host",
                }
                if group:
                    groups = await client.call("hostgroup.get", {
                        "output": ["groupid"],
                        "filter": {"name": [group]},
                    })
                    if groups:
                        params["groupids"] = [g["groupid"] for g in groups]
                    else:
                        return f"Host group '{group}' not found."

                hosts = await client.call("host.get", params)

                if country:
                    hosts = [h for h in hosts if extract_country(h["host"]).lower() == country.lower()]

                # Group by base hostname
                clusters: dict[str, list[dict]] = defaultdict(list)
                for h in hosts:
                    base = _parse_cluster_base(h.get("name", h["host"]))
                    clusters[base].append(h)

                # Only keep actual clusters (2+ members)
                multi = {k: v for k, v in clusters.items() if len(v) > 1}

                if not multi:
                    return f"No clusters found ({len(hosts)} standalone hosts)."

                sorted_clusters = sorted(multi.items(), key=lambda x: -len(x[1]))[:max_results]

                lines = [f"**{len(multi)} clusters** ({sum(len(v) for v in multi.values())} hosts)\n"]
                for base, members in sorted_clusters:
                    cc = extract_country(base)
                    prod, _ = _classify_host(members[0].get("groups", []))
                    lines.append(f"**{base}** ({cc}, {prod or '?'}) — {len(members)} members")
                    for m in sorted(members, key=lambda x: x["host"]):
                        role = _infer_cluster_role(m.get("name", m["host"]))
                        ip = next((i["ip"] for i in m.get("interfaces", []) if i.get("ip") != "127.0.0.1"), "")
                        lines.append(f"  {m['host']} — {role} {ip}")
                    lines.append("")

                omitted = len(multi) - len(sorted_clusters)
                if omitted > 0:
                    lines.append(f"*{omitted} more clusters omitted*")

                return "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

    
    if "search_hosts_by_location" not in skip:

        @mcp.tool()
        async def search_hosts_by_location(
            country: str = "",
            group: str = "",
            product: str = "",
            min_traffic_mbps: float = 0,
            show_cluster_role: bool = False,
            max_results: int = 50,
            instance: str = "",
        ) -> str:
            """Search hosts by country, group, product with optional traffic filter.

            Args:
                country: 2-letter country code
                group: Host group name filter
                product: Product name filter (substring)
                min_traffic_mbps: Min traffic threshold in Mbps
                show_cluster_role: Show primary/secondary role
                max_results: Max results (default: 50)
                instance: Zabbix instance (optional)
            """
            try:
                client = resolver.resolve(instance)
                hosts = await fetch_enabled_hosts(client, extra_output=["name", "status"])

                if group:
                    grps = await client.call("hostgroup.get", {
                        "output": ["groupid"],
                        "filter": {"name": [group]},
                    })
                    if not grps:
                        return f"Host group '{group}' not found."
                    gids = {g["groupid"] for g in grps}
                    hosts = [h for h in hosts if any(g.get("groupid") in gids for g in h.get("groups", []))]

                if country:
                    hosts = [h for h in hosts if extract_country(h["host"]).lower() == country.lower()]
                if product:
                    hosts = [h for h in hosts if product.lower() in (_classify_host(h.get("groups", []))[0] or "").lower()]

                if not hosts:
                    return "No hosts match the filters."

                hids = [h["hostid"] for h in hosts]
                traffic_map = await fetch_traffic_map(client, hids)

                if min_traffic_mbps > 0:
                    hosts = [h for h in hosts if traffic_map.get(h["hostid"], 0) >= min_traffic_mbps]

                if not hosts:
                    return f"No hosts with traffic >= {min_traffic_mbps} Mbps."

                total = len(hosts)
                hosts = hosts[:max_results]

                lines = [f"**{total} hosts**" + (f" (showing {max_results})" if total > max_results else "")]
                lines.append("| Host | Country | Product | IP | Traffic Mbps |" + (" Role |" if show_cluster_role else ""))
                lines.append("|------|---------|---------|----|-----------:|" + ("------|" if show_cluster_role else ""))

                for h in hosts:
                    hostname = h["host"]
                    cc = extract_country(hostname)
                    prod, _ = _classify_host(h.get("groups", []))
                    ip = host_ip(h)
                    mbps = traffic_map.get(h["hostid"], 0)
                    row = f"| {hostname} | {cc} | {prod or '?'} | {ip} | {mbps:.1f} |"
                    if show_cluster_role:
                        role = _infer_cluster_role(h.get("name", hostname))
                        row += f" {role} |"
                    lines.append(row)

                if total > max_results:
                    lines.append(f"\n*{total - max_results} more hosts omitted*")
                return "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"
