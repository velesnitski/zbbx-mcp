"""Shared data fetching and row building for report modules.

Centralizes the pattern of: fetch hosts + dashboards + metrics → build rows.
All report tools use this instead of duplicating API call logic.
"""

from __future__ import annotations

import asyncio
import re
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from zbbx_mcp.classify import classify_host as _classify_host
from zbbx_mcp.classify import detect_provider
from zbbx_mcp.client import ZabbixClient
from zbbx_mcp.excel import BW_MAX, classify_bandwidth

__all__ = [
    "ServerRow", "extract_country", "fetch_all_data", "fetch_trends_batch",
    "build_value_map", "build_max_map", "countries_for_region",
    "TRAFFIC_IN_KEYS", "TRAFFIC_OUT_KEYS", "METRIC_KEYS", "GB_BYTES",
    "REGION_MAP", "CAPITAL_COORDS",
]

_COUNTRY_RE = re.compile(
    r"(?:[-_]([a-z]{2})\d)"       # nl0105, de0267
    r"|(?:[-_]([a-z]{2})[-_])",   # -in-lite, -us-lite
    re.IGNORECASE,
)
GB_BYTES = 1_073_741_824  # 1 GB in bytes

# Region → country code mapping for geo filtering
REGION_MAP: dict[str, list[str]] = {
    "LATAM": ["AR", "BR", "MX", "CL", "CO", "PE", "VE", "EC", "UY", "PY", "BO", "CR", "PA"],
    "APAC": ["JP", "IN", "ID", "TH", "KZ", "AZ", "SG", "KR", "AU", "NZ", "PH", "VN", "MY", "TW", "HK"],
    "EMEA": ["NL", "DE", "FR", "GB", "ES", "IT", "SE", "FI", "NO", "DK", "PL", "CZ", "AT", "CH", "BE",
             "PT", "IE", "RO", "BG", "HR", "UA", "TR", "IL", "AE", "ZA", "NG", "EG", "KE"],
    "NA": ["US", "CA"],
    "CIS": ["RU", "BY", "KZ", "UZ", "GE", "AM", "MD"],
}

# Capital coordinates for distance estimation (lat, lon)
CAPITAL_COORDS: dict[str, tuple[float, float]] = {
    "AR": (-34.6, -58.4), "BR": (-15.8, -47.9), "MX": (19.4, -99.1),
    "CL": (-33.4, -70.6), "CO": (4.7, -74.1), "PE": (-12.0, -77.0),
    "VE": (10.5, -66.9), "EC": (-0.2, -78.5), "UY": (-34.9, -56.2),
    "US": (38.9, -77.0), "CA": (45.4, -75.7),
    "NL": (52.4, 4.9), "DE": (52.5, 13.4), "FR": (48.9, 2.3),
    "GB": (51.5, -0.1), "ES": (40.4, -3.7), "IT": (41.9, 12.5),
    "SE": (59.3, 18.1), "FI": (60.2, 24.9), "NO": (59.9, 10.8),
    "PL": (52.2, 21.0), "CZ": (50.1, 14.4), "AT": (48.2, 16.4),
    "CH": (46.9, 7.4), "BE": (50.8, 4.4), "PT": (38.7, -9.1),
    "IE": (53.3, -6.3), "RO": (44.4, 26.1), "BG": (42.7, 23.3),
    "HR": (45.8, 16.0), "UA": (50.4, 30.5), "TR": (39.9, 32.9),
    "IL": (31.8, 34.8), "AE": (24.5, 54.7), "ZA": (-33.9, 18.4),
    "JP": (35.7, 139.7), "IN": (28.6, 77.2), "ID": (-6.2, 106.8),
    "TH": (13.8, 100.5), "KZ": (51.2, 71.4), "AZ": (40.4, 49.9),
    "SG": (1.3, 103.8), "KR": (37.6, 127.0), "AU": (-33.9, 151.2),
    "RU": (55.8, 37.6), "BY": (53.9, 27.6),
    "DK": (55.7, 12.6), "HK": (22.3, 114.2), "TW": (25.0, 121.5),
}


def countries_for_region(region: str) -> set[str]:
    """Return set of country codes for a region name. ALL returns everything."""
    r = region.upper()
    if r == "ALL":
        return {cc for codes in REGION_MAP.values() for cc in codes}
    return set(REGION_MAP.get(r, []))

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

# Metric key groups for batch trend fetching
METRIC_KEYS: dict[str, list[str]] = {
    "cpu": ["system.cpu.util[,idle]"],
    "load": ["system.cpu.load[percpu,avg5]"],
    "memory": ["vm.memory.size[available]"],
    "traffic": TRAFFIC_IN_KEYS,
    "traffic_out": TRAFFIC_OUT_KEYS,
    "iowait": ["system.cpu.util[,iowait]"],
    "softirq": ["system.cpu.util[,softirq]"],
    "disk_read": ["vfs.dev.read.rate[sda]", "vfs.dev.read.rate[vda]", "vfs.dev.read.rate[nvme0n1]"],
    "disk_write": ["vfs.dev.write.rate[sda]", "vfs.dev.write.rate[vda]", "vfs.dev.write.rate[nvme0n1]"],
}


def extract_country(hostname: str) -> str:
    """Extract 2-letter country code from hostname.

    Handles: srv-nl0105 → NL, srv-nl01-lite → IN, srv-us01-lite → IN
    """
    m = _COUNTRY_RE.search(hostname)
    if not m:
        return ""
    return (m.group(1) or m.group(2) or "").upper()


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


@dataclass(slots=True)
class ServerRow:
    """One row in a server report."""
    hostid: str = ""
    host: str = ""
    name: str = ""
    country: str = ""
    dashboard: str = ""
    dashboardid: str = ""
    tab: str = ""
    page_index: int = 0
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
            "Host ID": self.hostid,
            "Host": self.host,
            "Name": self.name,
            "Country": self.country,
            "Dashboard": self.dashboard,
            "Dashboard ID": self.dashboardid,
            "Page Index": self.page_index,
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
                        graph_context[gid] = {
                            "dashboard": dname,
                            "dashboardid": d.get("dashboardid", ""),
                            "tab": tab,
                            "page_index": pi,
                        }

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
        client.call("usermacro.get", {"hostids": all_ids, "output": ["hostid", "macro", "value"],
                                       "filter": {"macro": ["{$COST_MONTH}", "{$BW_LIMIT}"]}}),
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
    mem_map = build_value_map(mem_items, lambda v: round(float(v) / GB_BYTES, 1))
    conn_map = build_value_map(conn_items)
    # Split cost and BW limit macros
    cost_map: dict[str, float] = {}
    bw_limit_map: dict[str, float] = {}
    for m in cost_macros:
        try:
            if m.get("macro") == "{$COST_MONTH}":
                cost_map[m["hostid"]] = float(m["value"])
            elif m.get("macro") == "{$BW_LIMIT}":
                bw_limit_map[m["hostid"]] = float(m["value"])
        except (ValueError, TypeError, KeyError):
            pass
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
        except (ValueError, KeyError, OSError):
            pass  # Fallback is best-effort

    # service protocol check: any item with value 1 = OK
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
            hostid=hid,
            host=hostname,
            name=h.get("name", ""),
            country=extract_country(hostname),
            dashboard=tabs[0]["dashboard"] if tabs else "",
            dashboardid=tabs[0].get("dashboardid", "") if tabs else "",
            tab=tabs[0]["tab"] if tabs else "",
            page_index=tabs[0].get("page_index", 0) if tabs else 0,
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
            bw_util_pct=round(in_mbps / (bw_limit_map.get(hid, BW_MAX)) * 100, 1) if in_mbps else None,
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



@dataclass
class TrendRow:
    """One metric trend for one host."""
    hostid: str
    hostname: str
    metric: str
    avg: float
    peak: float
    min_val: float
    current: float
    trend_dir: str = ""
    daily: dict = field(default_factory=dict)  # date_str -> avg value

    def to_dict(self) -> dict[str, Any]:
        return {
            "Host": self.hostname,
            "Metric": self.metric,
            "Avg": self.avg,
            "Peak": self.peak,
            "Min": self.min_val,
            "Current": self.current,
            "Trend": self.trend_dir,
        }


def _parse_period(period: str) -> int:
    """Parse '1d', '7d', '30d' to seconds."""
    period = period.strip().lower()
    if period.endswith("d"):
        return int(period[:-1]) * 86400
    if period.endswith("h"):
        return int(period[:-1]) * 3600
    return int(period) * 86400  # default to days


async def fetch_trends_batch(
    client: ZabbixClient,
    hostids: list[str],
    metrics: list[str] | None = None,
    period: str = "7d",
) -> tuple[list[TrendRow], dict[str, dict]]:
    """Fetch trend data for multiple hosts and metrics in 3 API calls.

    Args:
        client: ZabbixClient instance
        hostids: List of host IDs
        metrics: List of metric names ('cpu', 'load', 'memory', 'traffic')
        period: Time period ('1d', '7d', '30d')

    Returns:
        Tuple of (trend_rows, host_map) where host_map is {hostid: host_dict}
    """

    if metrics is None:
        metrics = ["cpu", "traffic", "load"]

    # Build key list from metric names
    all_keys: list[str] = []
    metric_key_map: dict[str, str] = {}  # item_key -> metric_name
    for m in metrics:
        keys = METRIC_KEYS.get(m, [])
        all_keys.extend(keys)
        for k in keys:
            metric_key_map[k] = m

    if not all_keys:
        return [], {}

    # Get host details + items in parallel
    host_data, items = await asyncio.gather(
        client.call("host.get", {
            "hostids": hostids,
            "output": ["hostid", "host"],
        }),
        client.call("item.get", {
            "hostids": hostids,
            "output": ["itemid", "hostid", "key_", "lastvalue", "value_type"],
            "filter": {"key_": all_keys, "status": "0"},
        }),
    )

    host_map = {h["hostid"]: h for h in host_data}

    # Pick best item per host per metric (max lastvalue for traffic, first for others)
    host_metric_item: dict[str, dict[str, dict]] = {}  # hostid -> metric -> item
    for item in items:
        hid = item["hostid"]
        metric_name = metric_key_map.get(item["key_"])
        if not metric_name:
            continue
        existing = host_metric_item.setdefault(hid, {}).get(metric_name)
        if existing is None:
            host_metric_item[hid][metric_name] = item
        elif metric_name == "traffic":
            try:
                if float(item.get("lastvalue", "0")) > float(existing.get("lastvalue", "0")):
                    host_metric_item[hid][metric_name] = item
            except (ValueError, TypeError):
                pass

    # Collect all item IDs for trend fetch
    item_ids = []
    item_to_host_metric: dict[str, tuple[str, str]] = {}  # itemid -> (hostid, metric)
    for hid, metric_items in host_metric_item.items():
        for metric_name, item in metric_items.items():
            item_ids.append(item["itemid"])
            item_to_host_metric[item["itemid"]] = (hid, metric_name)

    if not item_ids:
        return [], host_map

    # Fetch trends
    now = int(_time.time())
    time_from = now - _parse_period(period)

    trends = await client.call("trend.get", {
        "itemids": item_ids,
        "time_from": time_from,
        "output": ["itemid", "clock", "value_avg", "value_max", "value_min"],
        "limit": len(item_ids) * 24 * 30,  # max hourly records
    })

    # Group trends by item
    item_trends: dict[str, list] = {}
    for t in trends:
        item_trends.setdefault(t["itemid"], []).append(t)

    # Build TrendRows
    rows: list[TrendRow] = []
    for itemid, (hid, metric_name) in item_to_host_metric.items():
        t_data = item_trends.get(itemid, [])
        if not t_data:
            continue

        hostname = host_map.get(hid, {}).get("host", "?")
        item = host_metric_item.get(hid, {}).get(metric_name, {})
        try:
            current = float(item.get("lastvalue", "0"))
        except (ValueError, TypeError):
            current = 0

        avgs = [float(t["value_avg"]) for t in t_data]
        peaks = [float(t["value_max"]) for t in t_data]
        mins = [float(t["value_min"]) for t in t_data]

        avg_val = sum(avgs) / len(avgs) if avgs else 0
        peak_val = max(peaks) if peaks else 0
        min_val = min(mins) if mins else 0

        # For CPU: invert (idle → used)
        if metric_name == "cpu":
            current = round(100 - current, 1)
            avg_val = round(100 - avg_val, 1)
            peak_val = round(100 - min_val, 1)  # min idle = max used
            min_val = round(100 - max(avgs), 1) if avgs else 0

        # For traffic: convert to Mbps
        if metric_name == "traffic":
            current = round(current / 1e6, 1)
            avg_val = round(avg_val / 1e6, 1)
            peak_val = round(peak_val / 1e6, 1)
            min_val = round(min_val / 1e6, 1)

        # For memory: convert to GB
        if metric_name == "memory":
            current = round(current / GB_BYTES, 1)
            avg_val = round(avg_val / GB_BYTES, 1)
            peak_val = round(peak_val / GB_BYTES, 1)
            min_val = round(min_val / GB_BYTES, 1)

        # Determine trend direction
        if len(avgs) >= 2:
            recent = sum(avgs[-len(avgs)//4:]) / max(1, len(avgs)//4) if avgs else 0
            older = sum(avgs[:len(avgs)//4]) / max(1, len(avgs)//4) if avgs else 0
            if metric_name == "cpu":
                recent, older = 100 - recent, 100 - older
            if metric_name in ("traffic", "memory"):
                recent /= 1e6 if metric_name == "traffic" else GB_BYTES
                older /= 1e6 if metric_name == "traffic" else GB_BYTES
            pct_change = ((recent - older) / older * 100) if older > 0 else 0
            if pct_change > 15:
                trend_dir = "rising"
            elif pct_change < -15:
                trend_dir = "dropping"
            else:
                trend_dir = "stable"
        else:
            trend_dir = ""

        # Build daily breakdown
        daily: dict[str, float] = {}
        for t in t_data:
            dt = datetime.fromtimestamp(int(t["clock"]), tz=timezone.utc)
            day_key = dt.strftime("%b %d")
            day_val = float(t["value_avg"])
            if metric_name == "cpu":
                day_val = round(100 - day_val, 1)
            elif metric_name == "traffic":
                day_val = round(day_val / 1e6, 1)
            elif metric_name == "memory":
                day_val = round(day_val / GB_BYTES, 1)
            else:
                day_val = round(day_val, 2)
            # Average per day (multiple hourly records)
            if day_key in daily:
                daily[day_key] = round((daily[day_key] + day_val) / 2, 1)
            else:
                daily[day_key] = day_val

        rows.append(TrendRow(
            hostid=hid,
            hostname=hostname,
            metric=metric_name,
            avg=round(avg_val, 1),
            peak=round(peak_val, 1),
            min_val=round(min_val, 1),
            current=round(current, 1),
            trend_dir=trend_dir,
            daily=daily,
        ))

    rows.sort(key=lambda r: (r.hostname, r.metric))
    return rows, host_map
