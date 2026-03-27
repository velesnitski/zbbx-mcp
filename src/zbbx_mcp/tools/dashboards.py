import httpx

from zbbx_mcp.resolver import InstanceResolver

WIDGET_TYPES = {
    "graph": "Graph",
    "clock": "Clock",
    "problems": "Problems",
    "problemsbysv": "Problems by severity",
    "hostavail": "Host availability",
    "systeminfo": "System info",
    "favmaps": "Favorite maps",
    "favgraphs": "Favorite graphs",
    "map": "Map",
    "svggraph": "SVG Graph",
    "plaintext": "Plain text",
    "url": "URL",
    "dataover": "Data overview",
    "trigover": "Trigger overview",
    "item": "Item value",
    "gauge": "Gauge",
    "tophosts": "Top hosts",
    "piechart": "Pie chart",
    "geomap": "Geo map",
    "honeycomb": "Honeycomb",
    "itemhistory": "Item history",
    "slareport": "SLA report",
}

# Widget field types (Zabbix API)
FIELD_TYPES = {
    "2": "host_group",
    "3": "host",
    "4": "item",
    "5": "graph_prototype",
    "6": "graph",
}


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()) -> None:

    if "get_dashboards" not in skip:

        @mcp.tool()
        async def get_dashboards(instance: str = "") -> str:
            """List all Zabbix dashboards with page and widget counts.

            Args:
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)
                data = await client.call("dashboard.get", {
                    "output": ["dashboardid", "name"],
                    "selectPages": "extend",
                    "sortfield": "name",
                })

                if not data:
                    return "No dashboards found."

                lines = []
                for d in data:
                    pages = d.get("pages", [])
                    widget_count = sum(len(p.get("widgets", [])) for p in pages)
                    lines.append(
                        f"- **{d.get('name', '?')}** "
                        f"(id: {d.get('dashboardid', '?')}, "
                        f"{len(pages)} pages, {widget_count} widgets)"
                    )

                return f"**Found: {len(data)} dashboards**\n\n" + "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error querying Zabbix: {e}"

    if "get_dashboard_detail" not in skip:

        @mcp.tool()
        async def get_dashboard_detail(dashboard_id: str, instance: str = "") -> str:
            """Get full details of a Zabbix dashboard including pages, widgets, and referenced hosts.

            Args:
                dashboard_id: Zabbix dashboard ID
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)
                data = await client.call("dashboard.get", {
                    "dashboardids": [dashboard_id],
                    "output": "extend",
                    "selectPages": "extend",
                })

                if not data:
                    return f"Dashboard '{dashboard_id}' not found."

                d = data[0]
                host_ids = set()
                group_ids = set()
                item_ids = set()

                page_parts = []
                for pi, page in enumerate(d.get("pages", [])):
                    widgets = page.get("widgets", [])
                    widget_lines = []
                    for w in widgets:
                        wtype = WIDGET_TYPES.get(w.get("type", ""), w.get("type", "?"))
                        wname = w.get("name", "")
                        label = f"[{wtype}]"
                        if wname:
                            label += f" {wname}"

                        for f in w.get("fields", []):
                            ftype = f.get("type")
                            if ftype == "2":
                                group_ids.add(f["value"])
                            elif ftype == "3":
                                host_ids.add(f["value"])
                            elif ftype == "4":
                                item_ids.add(f["value"])

                        widget_lines.append(f"  - {label}")

                    page_name = page.get("name", f"Page {pi + 1}")
                    page_parts.append(f"### {page_name} ({len(widgets)} widgets)")
                    page_parts.extend(widget_lines)

                # Resolve host and group names
                parts = [
                    f"# Dashboard: {d.get('name', '?')}",
                    "",
                    f"**ID:** {d.get('dashboardid', '?')}",
                    f"**Pages:** {len(d.get('pages', []))}",
                ]

                if host_ids:
                    hosts = await client.call("host.get", {
                        "hostids": list(host_ids),
                        "output": ["hostid", "host", "name", "status"],
                        "selectGroups": ["name"],
                        "sortfield": "host",
                    })
                    parts.append("")
                    parts.append(f"## Referenced Hosts ({len(hosts)})")
                    for h in hosts:
                        status = "Enabled" if h.get("status") == "0" else "Disabled"
                        groups = ", ".join(g["name"] for g in h.get("groups", []))
                        parts.append(f"- **{h.get('host', '?')}** [{status}] ({groups})")

                if group_ids:
                    groups = await client.call("hostgroup.get", {
                        "groupids": list(group_ids),
                        "output": ["groupid", "name"],
                    })
                    parts.append("")
                    parts.append(f"## Referenced Host Groups ({len(groups)})")
                    for g in groups:
                        parts.append(f"- **{g.get('name', '?')}** (id: {g.get('groupid', '?')})")

                parts.append("")
                parts.append("## Pages")
                parts.extend(page_parts)

                if item_ids:
                    parts.append("")
                    parts.append(f"*{len(item_ids)} item references in graph widgets*")

                return "\n".join(parts)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error querying Zabbix: {e}"
