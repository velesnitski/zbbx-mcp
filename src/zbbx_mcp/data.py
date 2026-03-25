"""Shared data fetching and row building for report modules.

Centralizes the pattern of: fetch hosts + dashboards + metrics → build rows.
All report tools use this instead of duplicating API call logic.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any

from zbbx_mcp.client import ZabbixClient
from zbbx_mcp.classify import classify_host as _classify_host, detect_provider
from zbbx_mcp.excel import classify_bandwidth, BW_MAX

_COUNTRY_RE = re.compile(r"[-_]([a-z]{2})\d", re.IGNORECASE)

# All known physical network interface keys (discovered from infrastructure)
TRAFFIC_IN_KEYS = [
    "net.if.in[eno1]", "net.if.in[eno2]", "net.if.in[eno4]",
    "net.if.in[eth0]", "net.if.in[eth1]",
    "net.if.in[enp1s0f0]", "net.if.in[enp1s0f1]",
    "net.if.in[enp2s0]", "net.if.in[enp2s0f0]", "net.if.in[enp2s0f1]",
    "net.if.in[enp3s0]", "net.if.in[enp3s0f0]", "net.if.in[enp3s0f1]",
    "net.if.in[enp4s0]", "net.if.in[enp5s0f0]", "net.if.in[enp6s0]",
    "net.if.in[enp8s0f0]", "net.if.in[enp10s0]", "net.if.in[enp11s0]",
    "net.if.in[enp45s0f1]",
    "net.if.in[ens3]", "net.if.in[ens5]", "net.if.in[ens192]",
    "net.if.in[ens6f0]", "net.if.in[ens7f0]", "net.if.in[ens7f0np0]",
    "net.if.in[ppp0]", "net.if.in[ppp1]", "net.if.in[bond0]",
]
TRAFFIC_OUT_KEYS = [k.replace("net.if.in[", "net.if.out[") for k in TRAFFIC_IN_KEYS]


def extract_country(hostname: str) -> str:
    """Extract 2-letter country code from hostname (e.g., srv-free-nl0105 → NL)."""
    m = _COUNTRY_RE.search(hostname)
    return m.group(1).upper() if m else ""


def build_value_map(items: list[dict], transform=float) -> dict[str, Any]:
    """Build hostid → value map from Zabbix item results."""
    m: dict[str, Any] = {}
    for i in items:
        try:
            m[i["hostid"]] = transform(i["lastvalue"])
        except (ValueError, TypeError, KeyError):
            pass
    return m


def build_max_map(items: list[dict]) -> dict[str, float]:
    """Build hostid → max(value) map (for traffic with multiple interfaces)."""
    m: dict[str, float] = {}
    for i in items:
        try:
            val = float(i["lastvalue"])
            hid = i["hostid"]
            if val > m.get(hid, 0):
                m[hid] = val
        except (ValueError, TypeError, KeyError):
            pass
    return m


@dataclass
class ServerRow:
    """One row in a server report."""
    host: str = ""
    name: str = ""
    country: str = ""
    dashboard: str = ""
    tab: str = ""
    all_tabs: str = ""
    product: str = ""
    tier: str = ""
    provider: str = ""
    ip: str = ""
    cpu_pct: float | None = None
    load_avg5: float | None = None
    mem_avail_gb: float | None = None
    traffic_in_mbps: float | None = None
    traffic_out_mbps: float | None = None
    traffic_total_mbps: float | None = None
    bw_util_pct: float | None = None
    bw_tier: str = ""
    connections: float | None = None
    service1: str = ""
    agent: str = ""
    service2: str = ""
    service_tertiary: str = ""
    active_problems: int = 0
    templates: str = ""
    cost_month: float | None = None
    cost_year: float | None = None
    on_dashboard: bool = False
    groups: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict matching Excel column names."""
        return {
            "Host": self.host,
            "Name": self.name,
            "Country": self.country,
            "Dashboard": self.dashboard,
            "Tab": self.tab,
            "Product": self.product,
            "Tier": self.tier,
            "Provider": self.provider,
            "IP": self.ip,
            "CPU %": self.cpu_pct,
            "Load Avg5": self.load_avg5,
            "Mem Avail GB": self.mem_avail_gb,
            "Traffic In Mbps": self.traffic_in_mbps,
            "Traffic Out Mbps": self.traffic_out_mbps,
            "Traffic Total Mbps": self.traffic_total_mbps,
            "BW Util %": self.bw_util_pct,
            "BW Tier": self.bw_tier,
            "Connections": self.connections,
            "service Primary": self.service1,
            "service Secondary": self.service2,
            "service Tertiary": self.service_tertiary,
            "Agent": self.agent,
            "Problems": self.active_problems or "",
            "Templates": self.templates,
            "Cost/Month ($)": self.cost_month,
            "Cost/Year ($)": self.cost_year,
            "On Dashboard": "Yes" if self.on_dashboard else "No",
            "All Tabs": self.all_tabs,
            "Groups": self.groups,
        }


@dataclass
class FetchResult:
    """Result of fetch_all_data()."""
    rows: list[dict] = field(default_factory=list)
    tab_data: dict[str, list[dict]] = field(default_factory=dict)
    total_on_dashboard: int = 0
    total_off_dashboard: int = 0


async def fetch_all_data(
    client: ZabbixClient,
    include_off_dashboard: bool = True,
    dashboard_id: str | None = None,
) -> FetchResult:
    """Fetch hosts, dashboards, and all metrics. Build rows.

    Args:
        client: ZabbixClient instance
        include_off_dashboard: Include servers not on any dashboard
        dashboard_id: If set, only fetch this dashboard (otherwise all)
    """
    # Phase 1: dashboards + hosts
    dash_params: dict = {
        "output": ["dashboardid", "name"],
        "selectPages": "extend",
    }
    if dashboard_id:
        dash_params["dashboardids"] = [dashboard_id]

    # Use cached hosts if available (hosts rarely change)
    cached_hosts = client._get_cached("all_enabled_hosts", ttl=60.0)
    if cached_hosts is not None:
        dashboards = await client.call("dashboard.get", dash_params)
        hosts = cached_hosts
    else:
        dashboards, hosts = await asyncio.gather(
            client.call("dashboard.get", dash_params),
            client.call("host.get", {
                "output": ["hostid", "host", "name", "status"],
                "selectGroups": ["name"],
                "selectInterfaces": ["ip"],
                "filter": {"status": "0"},
            }),
        )
        client._set_cache("all_enabled_hosts", hosts)

    # Build graph → (dashboard, tab) mapping
    all_graph_ids: set[str] = set()
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

    # Phase 2: all metrics in parallel
    graph_task = (
        client.call("graph.get", {
            "graphids": list(all_graph_ids),
            "output": ["graphid"],
            "selectHosts": ["hostid"],
        }) if all_graph_ids else asyncio.sleep(0)
    )

    results = await asyncio.gather(
        client.call("item.get", {"hostids": all_ids, "output": ["hostid", "lastvalue"],
                                  "filter": {"key_": "system.cpu.util[,idle]", "status": "0"}}),
        client.call("item.get", {"hostids": all_ids, "output": ["hostid", "lastvalue"],
                                  "filter": {"key_": "system.cpu.load[percpu,avg5]", "status": "0"}}),
        client.call("item.get", {"hostids": all_ids, "output": ["hostid", "lastvalue"],
                                  "filter": {"key_": "vm.memory.size[available]", "status": "0"}}),
        client.call("item.get", {"hostids": all_ids, "output": ["hostid", "lastvalue"],
                                  "filter": {"key_": "service_connections", "status": "0"}}),
        # Traffic: filter by known physical interface keys
        client.call("item.get", {"hostids": all_ids, "output": ["hostid", "lastvalue"],
                                  "filter": {"key_": TRAFFIC_IN_KEYS, "status": "0"}}),
        client.call("item.get", {"hostids": all_ids, "output": ["hostid", "lastvalue"],
                                  "filter": {"key_": TRAFFIC_OUT_KEYS, "status": "0"}}),
        client.call("item.get", {"hostids": all_ids, "output": ["hostid", "lastvalue"],
                                  "filter": {"key_": "agent.version", "status": "0"}}),
        client.call("item.get", {"hostids": all_ids, "output": ["hostid", "lastvalue"],
                                  "filter": {"key_": "service_primary_check[{HOST.IP}]", "status": "0"}}),
        client.call("item.get", {"hostids": all_ids, "output": ["hostid", "lastvalue"],
                                  "filter": {"key_": "service_secondary_check[{HOST.IP}]", "status": "0"}}),
        client.call("item.get", {"hostids": all_ids, "output": ["hostid", "lastvalue"],
                                  "search": {"key_": "service3"}, "searchWildcardsEnabled": True,
                                  "filter": {"status": "0"}}),
        client.call("usermacro.get", {"hostids": all_ids, "output": ["hostid", "value"],
                                       "filter": {"macro": "{$COST_MONTH}"}}),
        client.call("host.get", {"hostids": all_ids, "output": ["hostid"],
                                  "selectParentTemplates": ["name"]}),
        graph_task,
        return_exceptions=True,
    )

    # Gracefully handle failed API calls (return empty list instead of crashing)
    def _safe(r, idx: int) -> list:
        if isinstance(r, BaseException):
            return []
        return r if isinstance(r, list) else []

    (cpu_items, load_items, mem_items, conn_items,
     in_traffic_items, out_traffic_items,
     version_items, service1_items, service2_items, service3_items,
     cost_macros, template_hosts, graphs_raw) = (
        _safe(results[i], i) for i in range(13)
    )

    graphs = graphs_raw if isinstance(graphs_raw, list) else []

    # Build metric maps
    cpu_map = build_value_map(cpu_items, lambda v: round(100 - float(v), 1))
    load_map = build_value_map(load_items, lambda v: round(float(v), 2))
    mem_map = build_value_map(mem_items, lambda v: round(float(v) / 1_073_741_824, 1))
    conn_map = build_value_map(conn_items)
    cost_map = build_value_map(cost_macros, lambda v: float(v))
    version_map = build_value_map(version_items, lambda v: str(v))
    service1_map = build_value_map(service1_items, lambda v: int(float(v)))
    service2_map = build_value_map(service2_items, lambda v: int(float(v)))
    in_traffic_map = build_max_map(in_traffic_items)
    out_traffic_map = build_max_map(out_traffic_items)

    # Phase 3: fallback — fetch traffic for hosts missed by fast filter
    covered_hosts = set(in_traffic_map.keys())
    missing_hosts = [hid for hid in all_ids if hid not in covered_hosts]
    if missing_hosts:
        try:
            fallback_in, fallback_out = await asyncio.gather(
                client.call("item.get", {
                    "hostids": missing_hosts,
                    "output": ["hostid", "lastvalue"],
                    "search": {"name": "Incoming network traffic"},
                    "filter": {"status": "0"},
                }),
                client.call("item.get", {
                    "hostids": missing_hosts,
                    "output": ["hostid", "lastvalue"],
                    "search": {"name": "Outgoing network traffic"},
                    "filter": {"status": "0"},
                }),
                return_exceptions=True,
            )
            if isinstance(fallback_in, list):
                for i in fallback_in:
                    try:
                        val = float(i["lastvalue"])
                        hid = i["hostid"]
                        if val > in_traffic_map.get(hid, 0):
                            in_traffic_map[hid] = val
                    except (ValueError, TypeError, KeyError):
                        pass
            if isinstance(fallback_out, list):
                for i in fallback_out:
                    try:
                        val = float(i["lastvalue"])
                        hid = i["hostid"]
                        if val > out_traffic_map.get(hid, 0):
                            out_traffic_map[hid] = val
                    except (ValueError, TypeError, KeyError):
                        pass
        except Exception:
            pass  # Fallback is best-effort

    # service Tertiary: any check item with value 1 = OK
    service3_map: dict[str, int] = {}
    for i in service3_items:
        try:
            val = int(float(i["lastvalue"]))
            hid = i["hostid"]
            if val > service3_map.get(hid, 0):
                service3_map[hid] = val
        except (ValueError, TypeError, KeyError):
            pass

    # Templates per host
    template_map: dict[str, str] = {}
    if isinstance(template_hosts, list):
        for h in template_hosts:
            tnames = [t["name"] for t in h.get("parentTemplates", [])]
            if tnames:
                template_map[h["hostid"]] = ", ".join(tnames[:3])
                if len(tnames) > 3:
                    template_map[h["hostid"]] += f" (+{len(tnames)-3})"

    # Build graph → host and dashboard/tab mappings
    graph_to_hostid: dict[str, str] = {}
    dashboard_hosts: set[str] = set()
    for g in graphs:
        for h in g.get("hosts", []):
            graph_to_hostid[g["graphid"]] = h["hostid"]
            dashboard_hosts.add(h["hostid"])

    host_dash_tabs: dict[str, list[dict]] = {}
    for gid, ctx in graph_context.items():
        hid = graph_to_hostid.get(gid)
        if hid:
            host_dash_tabs.setdefault(hid, []).append(ctx)

    # Build rows
    rows: list[dict] = []
    tab_data: dict[str, list[dict]] = {}
    on_count = 0
    off_count = 0

    for hid, h in host_map.items():
        prod, tier = _classify_host(h.get("groups", []))
        if not prod or prod == "Unknown":
            continue

        on_dashboard = hid in dashboard_hosts
        if on_dashboard:
            on_count += 1
        else:
            off_count += 1
            if not include_off_dashboard:
                continue

        hostname = h.get("host", "")
        ip = next((i["ip"] for i in h.get("interfaces", []) if i.get("ip") != "127.0.0.1"), "")
        in_traffic = in_traffic_map.get(hid)
        out_traffic = out_traffic_map.get(hid)
        in_mbps = round(in_traffic / 1e6, 1) if in_traffic else None
        out_mbps = round(out_traffic / 1e6, 1) if out_traffic else None
        total_mbps = round((in_mbps or 0) + (out_mbps or 0), 1) if (in_mbps or out_mbps) else None
        cost = cost_map.get(hid)
        service1_val = service1_map.get(hid)
        service2_val = service2_map.get(hid)
        service3_val = service3_map.get(hid)
        version = version_map.get(hid)
        templates = template_map.get(hid, "")

        tabs = host_dash_tabs.get(hid, [])

        row = ServerRow(
            host=hostname,
            name=h.get("name", ""),
            country=extract_country(hostname),
            dashboard=tabs[0]["dashboard"] if tabs else "",
            tab=tabs[0]["tab"] if tabs else "",
            all_tabs=", ".join(f"{t['dashboard']} / {t['tab']}" for t in tabs) if tabs else "",
            product=prod,
            tier=tier,
            provider=detect_provider(ip) if ip else "",
            ip=ip,
            cpu_pct=cpu_map.get(hid),
            load_avg5=load_map.get(hid),
            mem_avail_gb=mem_map.get(hid),
            traffic_in_mbps=in_mbps,
            traffic_out_mbps=out_mbps,
            traffic_total_mbps=total_mbps,
            bw_util_pct=round(in_mbps / BW_MAX * 100, 1) if in_mbps else None,
            bw_tier=classify_bandwidth(in_mbps),
            connections=conn_map.get(hid),
            service1="OK" if service1_val == 1 else ("DOWN" if service1_val is not None else ""),
            service2="OK" if service2_val == 1 else ("DOWN" if service2_val is not None else ""),
            service_tertiary="OK" if service3_val and service3_val >= 1 else ("DOWN" if service3_val is not None else ""),
            agent=str(version) if isinstance(version, str) else "",
            templates=templates,
            cost_month=cost,
            cost_year=round(cost * 12, 2) if cost else None,
            on_dashboard=on_dashboard,
            groups=", ".join(g["name"] for g in h.get("groups", [])),
        ).to_dict()

        rows.append(row)

        # Track per dashboard/tab
        if row["Dashboard"]:
            key = f"{row['Dashboard']}||{row['Tab']}"
            tab_data.setdefault(key, []).append(row)

    # Sort: dashboard servers first
    rows.sort(key=lambda r: (
        0 if r["On Dashboard"] == "Yes" else 1,
        r["Product"], r["Tier"], r["Host"],
    ))

    return FetchResult(
        rows=rows,
        tab_data=tab_data,
        total_on_dashboard=on_count,
        total_off_dashboard=off_count,
    )
