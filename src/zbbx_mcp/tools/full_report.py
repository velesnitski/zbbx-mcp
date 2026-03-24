"""Comprehensive server report: all dashboards, distinct servers, cost correlation."""

import asyncio
import os
from datetime import datetime
from statistics import median

import httpx

from zbbx_mcp.resolver import InstanceResolver
from zbbx_mcp.tools.inventory import _classify_host, detect_provider

# Bandwidth thresholds (1 Gbps NIC)
BW_MAX = 800  # Mbps - practical NIC limit
BW_RED = 650  # Mbps - near saturation
BW_ORANGE = 500  # Mbps - high utilization
BW_GREEN = 200  # Mbps - normal


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()):

    if "generate_full_report" not in skip:

        @mcp.tool()
        async def generate_full_report(
            include_off_dashboard: bool = True,
            output_dir: str = "",
            instance: str = "",
        ) -> str:
            """Generate comprehensive report of ALL distinct servers across ALL dashboards.

            For each server: dashboard tab, product, provider, IP, CPU, load,
            memory, traffic (color-coded: red >= 650 Mbps, orange >= 500 Mbps),
            connections, cost, bandwidth utilization %.

            Also includes servers NOT on any dashboard (if include_off_dashboard=True).

            Sheets:
            1. All Servers — distinct, deduplicated, with all metrics
            2. Per-dashboard tabs summary
            3. Provider × Product matrix with costs
            4. Bandwidth analysis (utilization tiers)
            5. Off-Dashboard servers (not monitored on any board)

            Args:
                include_off_dashboard: Include servers not on any dashboard (default: True)
                output_dir: Directory for the Excel file (default: ~/Downloads)
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)

                # Phase 1: dashboards + hosts (parallel)
                dashboards, hosts = await asyncio.gather(
                    client.call("dashboard.get", {
                        "output": ["dashboardid", "name"],
                        "selectPages": "extend",
                    }),
                    client.call("host.get", {
                        "output": ["hostid", "host", "name", "status"],
                        "selectGroups": ["name"],
                        "selectInterfaces": ["ip"],
                        "filter": {"status": "0"},
                    }),
                )

                # Build graph → (dashboard, tab) mapping
                all_graph_ids = set()
                graph_context: dict[str, dict] = {}
                for d in dashboards:
                    dname = d["name"]
                    for pi, page in enumerate(d.get("pages", [])):
                        tab = page.get("name", "") or f"Page {pi + 1}"
                        for w in page.get("widgets", []):
                            for f in w.get("fields", []):
                                if f.get("type") == "6":
                                    gid = f["value"]
                                    all_graph_ids.add(gid)
                                    graph_context[gid] = {"dashboard": dname, "tab": tab}

                host_map = {h["hostid"]: h for h in hosts}
                all_ids = list(host_map.keys())

                # Phase 2: all metrics in parallel (10 calls)
                graph_task = (
                    client.call("graph.get", {
                        "graphids": list(all_graph_ids),
                        "output": ["graphid"],
                        "selectHosts": ["hostid"],
                    }) if all_graph_ids else asyncio.sleep(0)
                )

                results = await asyncio.gather(
                    client.call("item.get", {
                        "hostids": all_ids,
                        "output": ["hostid", "lastvalue"],
                        "filter": {"key_": "system.cpu.util[,idle]", "status": "0"},
                    }),
                    client.call("item.get", {
                        "hostids": all_ids,
                        "output": ["hostid", "lastvalue"],
                        "filter": {"key_": "system.cpu.load[percpu,avg5]", "status": "0"},
                    }),
                    client.call("item.get", {
                        "hostids": all_ids,
                        "output": ["hostid", "lastvalue"],
                        "filter": {"key_": "vm.memory.size[available]", "status": "0"},
                    }),
                    client.call("item.get", {
                        "hostids": all_ids,
                        "output": ["hostid", "lastvalue"],
                        "filter": {"key_": "service_connections", "status": "0"},
                    }),
                    client.call("item.get", {
                        "hostids": all_ids,
                        "output": ["hostid", "lastvalue"],
                        "search": {"name": "Incoming network traffic"},
                        "filter": {"status": "0"},
                    }),
                    client.call("item.get", {
                        "hostids": all_ids,
                        "output": ["hostid", "lastvalue"],
                        "search": {"name": "Outgoing network traffic"},
                        "filter": {"status": "0"},
                    }),
                    client.call("item.get", {
                        "hostids": all_ids,
                        "output": ["hostid", "lastvalue"],
                        "filter": {"key_": "agent.version", "status": "0"},
                    }),
                    client.call("item.get", {
                        "hostids": all_ids,
                        "output": ["hostid", "lastvalue"],
                        "filter": {"key_": "service_primary_check[{HOST.IP}]", "status": "0"},
                    }),
                    client.call("usermacro.get", {
                        "hostids": all_ids,
                        "output": ["hostid", "value"],
                        "filter": {"macro": "{$COST_MONTH}"},
                    }),
                    graph_task,
                )

                (cpu_items, load_items, mem_items, conn_items,
                 in_traffic_items, out_traffic_items,
                 version_items, service1_items, cost_macros, graphs_raw) = results

                graphs = graphs_raw if isinstance(graphs_raw, list) else []

                # Build metrics maps
                def _build_map(items, transform=float):
                    m = {}
                    for i in items:
                        try:
                            m[i["hostid"]] = transform(i["lastvalue"])
                        except (ValueError, TypeError):
                            pass
                    return m

                def _build_max_map(items):
                    m: dict[str, float] = {}
                    for i in items:
                        try:
                            val = float(i["lastvalue"])
                            hid = i["hostid"]
                            if val > m.get(hid, 0):
                                m[hid] = val
                        except (ValueError, TypeError):
                            pass
                    return m

                cpu_map = _build_map(cpu_items, lambda v: round(100 - float(v), 1))
                load_map = _build_map(load_items, lambda v: round(float(v), 2))
                mem_map = _build_map(mem_items, lambda v: round(float(v) / 1_073_741_824, 1))
                conn_map = _build_map(conn_items)
                cost_map = _build_map(cost_macros, lambda v: float(v))
                version_map = _build_map(version_items, lambda v: str(v))
                service1_map = _build_map(service1_items, lambda v: int(float(v)))
                in_traffic_map = _build_max_map(in_traffic_items)
                out_traffic_map = _build_max_map(out_traffic_items)

                # Extract country code from hostname (e.g., srv-free-nl0105 → NL)
                import re
                _COUNTRY_RE = re.compile(r'[-_]([a-z]{2})\d', re.IGNORECASE)

                def _extract_country(hostname: str) -> str:
                    m = _COUNTRY_RE.search(hostname)
                    return m.group(1).upper() if m else ""

                # Build host → dashboard/tab mapping (may have multiple)
                graph_to_hostid = {}
                dashboard_hosts = set()
                for g in graphs:
                    for h in g.get("hosts", []):
                        graph_to_hostid[g["graphid"]] = h["hostid"]
                        dashboard_hosts.add(h["hostid"])

                host_dash_tabs: dict[str, list[dict]] = {}
                for gid, ctx in graph_context.items():
                    hid = graph_to_hostid.get(gid)
                    if hid:
                        host_dash_tabs.setdefault(hid, []).append(ctx)

                # Build rows (one per distinct server)
                rows = []
                for hid, h in host_map.items():
                    prod, tier = _classify_host(h.get("groups", []))
                    if not prod or prod == "Unknown":
                        continue

                    on_dashboard = hid in dashboard_hosts
                    if not on_dashboard and not include_off_dashboard:
                        continue

                    hostname = h.get("host", "")
                    ip = next((i["ip"] for i in h.get("interfaces", []) if i.get("ip") != "127.0.0.1"), "")
                    provider = detect_provider(ip) if ip else ""
                    country = _extract_country(hostname)
                    in_traffic = in_traffic_map.get(hid)
                    out_traffic = out_traffic_map.get(hid)
                    in_mbps = round(in_traffic / 1e6, 1) if in_traffic else None
                    out_mbps = round(out_traffic / 1e6, 1) if out_traffic else None
                    total_mbps = round((in_mbps or 0) + (out_mbps or 0), 1) if (in_mbps or out_mbps) else None
                    cost = cost_map.get(hid)
                    version = version_map.get(hid)
                    service1 = service1_map.get(hid)
                    service1_status = ""
                    if service1 is not None:
                        service1_status = "OK" if service1 == 1 else "DOWN"

                    # Dashboard info
                    tabs = host_dash_tabs.get(hid, [])
                    primary_dash = tabs[0]["dashboard"] if tabs else ""
                    primary_tab = tabs[0]["tab"] if tabs else ""
                    all_tabs = ", ".join(f"{t['dashboard']} / {t['tab']}" for t in tabs) if tabs else ""

                    # Bandwidth utilization (based on incoming, the dominant direction)
                    bw_util = round(in_mbps / BW_MAX * 100, 1) if in_mbps else None
                    bw_tier = ""
                    if in_mbps is not None:
                        if in_mbps >= BW_RED:
                            bw_tier = "CRITICAL"
                        elif in_mbps >= BW_ORANGE:
                            bw_tier = "HIGH"
                        elif in_mbps >= BW_GREEN:
                            bw_tier = "NORMAL"
                        else:
                            bw_tier = "LOW"

                    groups = ", ".join(g["name"] for g in h.get("groups", []))

                    rows.append({
                        "Host": hostname,
                        "Name": h.get("name", ""),
                        "Country": country,
                        "Dashboard": primary_dash,
                        "Tab": primary_tab,
                        "Product": prod,
                        "Tier": tier,
                        "Provider": provider,
                        "IP": ip,
                        "CPU %": cpu_map.get(hid),
                        "Load Avg5": load_map.get(hid),
                        "Mem Avail GB": mem_map.get(hid),
                        "Traffic In Mbps": in_mbps,
                        "Traffic Out Mbps": out_mbps,
                        "Traffic Total Mbps": total_mbps,
                        "BW Util %": bw_util,
                        "BW Tier": bw_tier,
                        "Connections": conn_map.get(hid),
                        "service Primary": service1_status,
                        "Agent": version if isinstance(version, str) else "",
                        "Cost/Month ($)": cost,
                        "Cost/Year ($)": round(cost * 12, 2) if cost else None,
                        "On Dashboard": "Yes" if on_dashboard else "No",
                        "All Tabs": all_tabs,
                        "Groups": groups,
                    })

                # Sort: dashboard servers first, then by product/tier/host
                rows.sort(key=lambda r: (
                    0 if r["On Dashboard"] == "Yes" else 1,
                    r["Product"], r["Tier"], r["Host"],
                ))

                # Generate Excel
                from openpyxl import Workbook
                from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

                wb = Workbook()
                hdr_font = Font(bold=True, color="FFFFFF", size=11)
                hdr_fill = PatternFill(start_color="2F5496", end_color="2F5496", fill_type="solid")
                red_fill = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
                dark_red_fill = PatternFill(start_color="C00000", end_color="C00000", fill_type="solid")
                dark_red_font = Font(color="FFFFFF", bold=True)
                orange_fill = PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")
                green_fill = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
                light_green_fill = PatternFill(start_color="E2EFDA", end_color="E2EFDA", fill_type="solid")
                thin_border = Border(bottom=Side(style="thin", color="D9D9D9"))

                headers = ["#", "Host", "Name", "Country", "Dashboard", "Tab",
                           "Product", "Tier", "Provider", "IP",
                           "CPU %", "Load Avg5", "Mem Avail GB",
                           "Traffic In Mbps", "Traffic Out Mbps", "Traffic Total Mbps",
                           "BW Util %", "BW Tier", "Connections", "service Primary", "Agent",
                           "Cost/Month ($)", "Cost/Year ($)",
                           "On Dashboard", "All Tabs", "Groups"]

                def _write_headers(ws, hdrs):
                    for col, h in enumerate(hdrs, 1):
                        cell = ws.cell(row=1, column=col, value=h)
                        cell.font = hdr_font
                        cell.fill = hdr_fill
                        cell.alignment = Alignment(horizontal="center")

                def _auto_width(ws, hdrs):
                    for col in range(1, len(hdrs) + 1):
                        max_len = len(str(ws.cell(1, col).value or ""))
                        for row in range(2, min(ws.max_row + 1, 50)):
                            val = ws.cell(row, col).value
                            if val:
                                max_len = max(max_len, len(str(val)))
                        ws.column_dimensions[ws.cell(1, col).column_letter].width = min(max_len + 3, 45)

                                ws1 = wb.active
                ws1.title = f"All Servers ({len(rows)})"
                _write_headers(ws1, headers)

                cpu_col = headers.index("CPU %") + 1
                traffic_in_col = headers.index("Traffic In Mbps") + 1
                traffic_total_col = headers.index("Traffic Total Mbps") + 1
                bw_col = headers.index("BW Util %") + 1
                tier_col = headers.index("BW Tier") + 1
                service1_col = headers.index("service Primary") + 1

                for idx, r in enumerate(rows, 2):
                    ws1.cell(row=idx, column=1, value=idx - 1)
                    for col, key in enumerate(headers[1:], 2):
                        cell = ws1.cell(row=idx, column=col, value=r.get(key, ""))
                        cell.border = thin_border

                    # CPU coloring
                    cpu_val = r.get("CPU %")
                    if cpu_val is not None:
                        cpu_cell = ws1.cell(row=idx, column=cpu_col)
                        if cpu_val >= 80:
                            cpu_cell.fill = red_fill
                        elif cpu_val >= 50:
                            cpu_cell.fill = orange_fill
                        elif cpu_val < 10:
                            cpu_cell.fill = green_fill

                    # Traffic coloring (based on incoming)
                    traffic_val = r.get("Traffic In Mbps")
                    if traffic_val is not None:
                        bw_cells = [
                            ws1.cell(row=idx, column=traffic_in_col),
                            ws1.cell(row=idx, column=traffic_total_col),
                            ws1.cell(row=idx, column=bw_col),
                            ws1.cell(row=idx, column=tier_col),
                        ]
                        if traffic_val >= BW_MAX:
                            for c in bw_cells:
                                c.fill = dark_red_fill
                                c.font = dark_red_font
                        elif traffic_val >= BW_RED:
                            for c in bw_cells:
                                c.fill = red_fill
                        elif traffic_val >= BW_ORANGE:
                            for c in bw_cells:
                                c.fill = orange_fill
                        elif traffic_val >= BW_GREEN:
                            for c in bw_cells:
                                c.fill = green_fill
                        else:
                            for c in bw_cells:
                                c.fill = light_green_fill

                    # service Primary status coloring
                    service1_val = r.get("service Primary")
                    if service1_val:
                        service1_cell = ws1.cell(row=idx, column=service1_col)
                        if service1_val == "DOWN":
                            service1_cell.fill = red_fill
                        elif service1_val == "OK":
                            service1_cell.fill = green_fill

                from openpyxl.utils import get_column_letter
                last_col = get_column_letter(len(headers))
                ws1.auto_filter.ref = f"A1:{last_col}{len(rows) + 1}"
                ws1.freeze_panes = "A2"
                _auto_width(ws1, headers)

                                ws2 = wb.create_sheet("Dashboard Tabs")
                tab_headers = ["Dashboard", "Tab", "Servers", "Median CPU %",
                               "Median Traffic Mbps", "Servers >= 650 Mbps",
                               "Total Connections", "Total Cost/Month ($)"]
                _write_headers(ws2, tab_headers)

                # Aggregate by dashboard/tab
                tab_data: dict[str, list[dict]] = {}
                for r in rows:
                    if r["Dashboard"]:
                        key = f"{r['Dashboard']}||{r['Tab']}"
                        tab_data.setdefault(key, []).append(r)

                for idx, (key, tab_rows) in enumerate(sorted(tab_data.items()), 2):
                    dash, tab = key.split("||", 1)
                    cpu_vals = [r["CPU %"] for r in tab_rows if r["CPU %"] is not None]
                    t_vals = [r["Traffic In Mbps"] for r in tab_rows if r["Traffic In Mbps"] is not None]
                    high_bw = sum(1 for r in tab_rows if (r["Traffic In Mbps"] or 0) >= BW_RED)
                    total_conns = sum(r["Connections"] or 0 for r in tab_rows)
                    total_cost = sum(r["Cost/Month ($)"] or 0 for r in tab_rows)

                    ws2.cell(row=idx, column=1, value=dash)
                    ws2.cell(row=idx, column=2, value=tab)
                    ws2.cell(row=idx, column=3, value=len(tab_rows))
                    ws2.cell(row=idx, column=4, value=round(median(cpu_vals), 1) if cpu_vals else "")
                    ws2.cell(row=idx, column=5, value=round(median(t_vals), 1) if t_vals else "")
                    cell_high = ws2.cell(row=idx, column=6, value=high_bw)
                    if high_bw > 0:
                        cell_high.fill = red_fill
                    ws2.cell(row=idx, column=7, value=total_conns)
                    ws2.cell(row=idx, column=8, value=round(total_cost, 2) if total_cost else "")

                ws2.freeze_panes = "A2"
                _auto_width(ws2, tab_headers)

                                ws3 = wb.create_sheet("Provider × Product")
                matrix: dict[str, dict[str, dict]] = {}
                for r in rows:
                    prov = r["Provider"] or "No IP"
                    prod = f"{r['Product']}/{r['Tier']}"
                    m = matrix.setdefault(prov, {}).setdefault(prod, {
                        "count": 0, "cost": 0.0, "traffic": [],
                    })
                    m["count"] += 1
                    m["cost"] += r["Cost/Month ($)"] or 0
                    if r["Traffic In Mbps"]:
                        m["traffic"].append(r["Traffic In Mbps"])

                products = sorted(set(
                    prod for prov_data in matrix.values() for prod in prov_data
                ))
                m_headers = ["Provider"] + products + ["Total Servers", "Total Cost ($)"]
                _write_headers(ws3, m_headers)

                for idx, prov in enumerate(sorted(matrix, key=lambda p: -sum(
                    d["count"] for d in matrix[p].values()
                )), 2):
                    ws3.cell(row=idx, column=1, value=prov)
                    total_svrs = 0
                    total_cost = 0.0
                    for col, prod in enumerate(products, 2):
                        d = matrix[prov].get(prod, {"count": 0, "cost": 0})
                        if d["count"] > 0:
                            ws3.cell(row=idx, column=col, value=d["count"])
                        total_svrs += d["count"]
                        total_cost += d.get("cost", 0)
                    ws3.cell(row=idx, column=len(products) + 2, value=total_svrs)
                    ws3.cell(row=idx, column=len(products) + 3, value=round(total_cost, 2) if total_cost else "")

                ws3.freeze_panes = "A2"
                _auto_width(ws3, m_headers)

                                ws4 = wb.create_sheet("Bandwidth Analysis")
                bw_headers = ["Tier", "Range", "Servers", "% of Total",
                              "Median CPU %", "Total Cost ($)"]
                _write_headers(ws4, bw_headers)

                tiers = [
                    ("CRITICAL", f">= {BW_RED} Mbps", lambda v: v >= BW_RED),
                    ("HIGH", f"{BW_ORANGE}–{BW_RED} Mbps", lambda v: BW_ORANGE <= v < BW_RED),
                    ("NORMAL", f"{BW_GREEN}–{BW_ORANGE} Mbps", lambda v: BW_GREEN <= v < BW_ORANGE),
                    ("LOW", f"< {BW_GREEN} Mbps", lambda v: v < BW_GREEN),
                    ("NO DATA", "No traffic data", lambda v: False),
                ]
                servers_with_traffic = [r for r in rows if r["Traffic In Mbps"] is not None]
                servers_no_traffic = [r for r in rows if r["Traffic In Mbps"] is None]

                for idx, (tier_name, label, pred) in enumerate(tiers, 2):
                    if tier_name == "NO DATA":
                        tier_rows = servers_no_traffic
                    else:
                        tier_rows = [r for r in servers_with_traffic if pred(r["Traffic In Mbps"])]
                    cpu_vals = [r["CPU %"] for r in tier_rows if r["CPU %"] is not None]
                    total_cost = sum(r["Cost/Month ($)"] or 0 for r in tier_rows)
                    pct = len(tier_rows) / len(rows) * 100 if rows else 0

                    ws4.cell(row=idx, column=1, value=tier_name)
                    ws4.cell(row=idx, column=2, value=label)
                    ws4.cell(row=idx, column=3, value=len(tier_rows))
                    ws4.cell(row=idx, column=4, value=f"{pct:.1f}%")
                    ws4.cell(row=idx, column=5, value=round(median(cpu_vals), 1) if cpu_vals else "")
                    ws4.cell(row=idx, column=6, value=round(total_cost, 2) if total_cost else "")

                    # Color the tier column
                    fill = {"CRITICAL": red_fill, "HIGH": orange_fill,
                            "NORMAL": green_fill, "LOW": light_green_fill}.get(tier_name)
                    if fill:
                        ws4.cell(row=idx, column=1).fill = fill

                ws4.freeze_panes = "A2"
                _auto_width(ws4, bw_headers)

                                off_dash = [r for r in rows if r["On Dashboard"] == "No"]
                if off_dash:
                    ws5 = wb.create_sheet(f"Off-Dashboard ({len(off_dash)})")
                    off_headers = ["#", "Host", "Country", "Product", "Tier", "Provider", "IP",
                                   "CPU %", "Traffic In Mbps", "service Primary", "Agent", "Groups"]
                    _write_headers(ws5, off_headers)
                    for idx, r in enumerate(off_dash, 2):
                        ws5.cell(row=idx, column=1, value=idx - 1)
                        for col, key in enumerate(off_headers[1:], 2):
                            ws5.cell(row=idx, column=col, value=r.get(key, ""))
                    ws5.freeze_panes = "A2"
                    _auto_width(ws5, off_headers)

                # Save
                if not output_dir:
                    output_dir = os.path.expanduser("~/Downloads")
                ts = datetime.now().strftime("%Y%m%d_%H%M")
                filepath = os.path.join(output_dir, f"zabbix_full_report_{ts}.xlsx")
                wb.save(filepath)

                # Summary
                on_dash = sum(1 for r in rows if r["On Dashboard"] == "Yes")
                critical = sum(1 for r in rows if r["BW Tier"] == "CRITICAL")
                high = sum(1 for r in rows if r["BW Tier"] == "HIGH")
                total_cost = sum(r["Cost/Month ($)"] or 0 for r in rows)

                parts = [
                    f"**Full Server Report**",
                    f"",
                    f"**File:** `{filepath}`",
                    f"**Servers:** {len(rows)} distinct ({on_dash} on dashboards, {len(off_dash)} off)",
                    f"**Bandwidth:** {critical} critical (>={BW_RED} Mbps), {high} high (>={BW_ORANGE} Mbps)",
                ]
                if total_cost:
                    parts.append(f"**Total cost:** ${total_cost:,.2f}/month")
                parts.extend([
                    f"",
                    f"### Sheets",
                    f"1. **All Servers** — {len(rows)} × {len(headers)} columns (traffic color-coded)",
                    f"2. **Dashboard Tabs** — {len(tab_data)} tabs with aggregates",
                    f"3. **Provider × Product** — matrix with server counts and costs",
                    f"4. **Bandwidth Analysis** — utilization tiers",
                    f"5. **Off-Dashboard** — {len(off_dash)} unmonitored servers",
                ])

                return "\n".join(parts)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"
