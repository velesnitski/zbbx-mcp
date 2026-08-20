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


# Zabbix widget-field types (API: dashboard widget field `type`). A widget
# names what it shows through these, and a reader that decodes only some of
# them reports the rest as nothing at all.
FIELD_GROUP = "2"
FIELD_HOST = "3"
FIELD_ITEM = "4"
FIELD_ITEM_PROTOTYPE = "5"
FIELD_GRAPH = "6"
FIELD_GRAPH_PROTOTYPE = "7"


def collect_widget_refs(pages: list[dict]) -> dict:
    """Pull every object reference out of a dashboard's widgets.

    Returns ``{"groups", "hosts", "items", "graphs", "graph_prototypes",
    "item_prototypes", "undecoded"}``.

    Only types 2/3/4 used to be read. A classic **Graph** widget names a
    *graphid*, not a host, so a dashboard built entirely from graph widgets
    resolved to zero hosts — and both callers reported that as fact: the detail
    view printed no host section, and `find_host_dashboard` answered "not found
    on any dashboard" for a host whose graphs are on one. The host was there;
    the decoder was not.

    ``undecoded`` counts widget types carrying fields none of the above
    matched, so "we could not read this" stays distinguishable from "this
    references nothing" (ADR 131). Pure.
    """
    out = {
        "groups": set(), "hosts": set(), "items": set(),
        "graphs": set(), "graph_prototypes": set(), "item_prototypes": set(),
        "undecoded": {},
    }
    bucket = {
        FIELD_GROUP: "groups", FIELD_HOST: "hosts", FIELD_ITEM: "items",
        FIELD_ITEM_PROTOTYPE: "item_prototypes", FIELD_GRAPH: "graphs",
        FIELD_GRAPH_PROTOTYPE: "graph_prototypes",
    }
    for page in pages or []:
        for w in page.get("widgets", []) or []:
            fields = w.get("fields", []) or []
            matched = False
            for f in fields:
                key = bucket.get(str(f.get("type")))
                if key and f.get("value"):
                    out[key].add(str(f["value"]))
                    matched = True
            if fields and not matched:
                wt = w.get("type", "?")
                out["undecoded"][wt] = out["undecoded"].get(wt, 0) + 1
    return out


async def hosts_behind_graphs(client, graph_ids) -> dict:
    """Map graphid -> {"name", "hosts": [(hostid, host)]} via ``graph.get``.

    A graph belongs to its items' hosts, which is the link that makes a
    graph-widget dashboard resolvable to hosts at all.
    """
    if not graph_ids:
        return {}
    graphs = await client.call("graph.get", {
        "graphids": list(graph_ids),
        "output": ["graphid", "name"],
        "selectHosts": ["hostid", "host"],
    })
    return {
        g["graphid"]: {
            "name": g.get("name", "?"),
            "hosts": [(h["hostid"], h.get("host", "?"))
                      for h in g.get("hosts", []) or []],
        }
        for g in graphs
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
                refs = collect_widget_refs(d.get("pages", []))
                # A classic Graph widget names a graphid, not a host. Resolving
                # it is what makes a graph-built dashboard show its hosts at all
                # (ADR 131).
                graph_map = await hosts_behind_graphs(client, refs["graphs"])
                host_ids = set(refs["hosts"])
                for g in graph_map.values():
                    host_ids.update(hid for hid, _ in g["hosts"])
                group_ids = refs["groups"]
                item_ids = refs["items"]

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
                        # Name the graph a Graph widget draws, so the page list
                        # says what is on it rather than repeating "[Graph]".
                        for f in w.get("fields", []):
                            if str(f.get("type")) == FIELD_GRAPH:
                                g = graph_map.get(str(f.get("value")))
                                if g:
                                    hs = ", ".join(h for _, h in g["hosts"])
                                    label += f" {g['name']}" + (f" — {hs}" if hs else "")
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
                    parts.append(f"*{len(item_ids)} direct item reference(s)*")
                if refs["graphs"]:
                    parts.append(
                        f"*{len(refs['graphs'])} graph(s) referenced; "
                        f"{len(graph_map)} resolved to "
                        f"{len(host_ids)} host(s)*")
                if refs["undecoded"]:
                    # "Could not read this widget" must not look like "this
                    # widget references nothing" (ADR 131).
                    detail = ", ".join(
                        f"{WIDGET_TYPES.get(k, k)}×{n}"
                        for k, n in sorted(refs["undecoded"].items()))
                    parts.append("")
                    parts.append(
                        f"⚠ {sum(refs['undecoded'].values())} widget(s) carry "
                        f"references this tool cannot decode ({detail}) — most "
                        "likely host/item *patterns* rather than ids. Their "
                        "hosts are NOT included above; absent here does not "
                        "mean absent from the dashboard.")

                return "\n".join(parts)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error querying Zabbix: {e}"

    if "find_host_dashboard" not in skip:

        @mcp.tool()
        async def find_host_dashboard(
            host_id: str,
            instance: str = "",
        ) -> str:
            """Find which dashboard(s) contain a host.

            Args:
                host_id: Host ID or hostname
                instance: Zabbix instance name (optional)
            """
            try:
                client = resolver.resolve(instance)

                # Resolve hostname to ID if needed
                if not host_id.isdigit():
                    lookup = await client.call("host.get", {
                        "output": ["hostid", "host"],
                        "filter": {"host": [host_id]},
                    })
                    if not lookup:
                        lookup = await client.call("host.get", {
                            "output": ["hostid", "host"],
                            "search": {"host": host_id},
                            "searchWildcardsEnabled": True,
                            "limit": 1,
                        })
                    if not lookup:
                        return f"Host '{host_id}' not found."
                    resolved_id = lookup[0]["hostid"]
                    hostname = lookup[0]["host"]
                else:
                    resolved_id = host_id
                    hosts = await client.call("host.get", {
                        "hostids": [host_id],
                        "output": ["host"],
                    })
                    hostname = hosts[0]["host"] if hosts else host_id

                # Get all dashboards with pages+widgets
                dashboards = await client.call("dashboard.get", {
                    "output": ["dashboardid", "name"],
                    "selectPages": "extend",
                })

                # Resolve every referenced graph ONCE, across all dashboards,
                # then match the host through them as well as directly. Matching
                # only field type 3 made a host whose graphs are on a dashboard
                # answer "not found on any dashboard" — the decoder's blind spot
                # reported as the fleet's state (ADR 131).
                all_graphs: set = set()
                for d in dashboards:
                    all_graphs |= collect_widget_refs(d.get("pages", []))["graphs"]
                graph_map = await hosts_behind_graphs(client, all_graphs)
                graphs_of_host = {
                    gid for gid, g in graph_map.items()
                    if any(hid == resolved_id for hid, _ in g["hosts"])
                }

                found = []
                undecoded_total = 0
                for d in dashboards:
                    undecoded_total += sum(
                        collect_widget_refs(d.get("pages", []))["undecoded"].values())
                    for pi, page in enumerate(d.get("pages", [])):
                        for w in page.get("widgets", []):
                            hit = ""
                            for f in w.get("fields", []):
                                ftype, val = str(f.get("type")), str(f.get("value"))
                                if ftype == FIELD_HOST and val == resolved_id:
                                    hit = "host widget"
                                elif ftype == FIELD_GRAPH and val in graphs_of_host:
                                    hit = f"graph '{graph_map[val]['name']}'"
                                if hit:
                                    break
                            if hit:
                                page_name = page.get("name", f"Page {pi + 1}")
                                found.append(
                                    f"**{d['name']}** — {page_name} "
                                    f"(id: {d['dashboardid']}, page: {pi}) via {hit}")
                                break

                if not found:
                    msg = f"Host **{hostname}** (ID: {resolved_id}) not found on any dashboard."
                    if undecoded_total:
                        msg += (f"\n\n⚠ {undecoded_total} widget(s) across all "
                                "dashboards carry references this tool cannot "
                                "decode (host/item patterns rather than ids). "
                                "This host could be on one of those and not be "
                                "found here.")
                    return msg

                lines = [f"Host **{hostname}** (ID: {resolved_id}) found on:\n"]
                for f in found:
                    lines.append(f"- {f}")
                return "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"
