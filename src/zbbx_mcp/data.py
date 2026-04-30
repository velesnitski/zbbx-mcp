"""Shared constants, types, and pure helpers for report modules.

Async fetch functions live in fetch.py. This module holds constants, dataclasses,
and synchronous utility functions used across the codebase.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

from zbbx_mcp.classify import classify_host as _classify_host
from zbbx_mcp.classify import resolve_datacenter  # noqa: F401 — re-export

__all__ = [
    "ServerRow", "FetchResult", "TrendRow",
    "extract_country", "build_value_map", "build_max_map", "build_parent_map",
    "countries_for_region", "group_by_country", "host_ip", "is_hidden_product",
    "resolve_datacenter",
    "_parse_period", "_resolve",
    "HIDE_PRODUCTS", "TRAFFIC_IN_KEYS", "TRAFFIC_OUT_KEYS", "METRIC_KEYS", "GB_BYTES",
    "REGION_MAP", "CAPITAL_COORDS", "STATUS_ENABLED",
    "KEY_service_PRIMARY", "KEY_service_SECONDARY", "KEY_service_TERTIARY",
    "KEY_CPU_IDLE", "KEY_CPU_LOAD", "KEY_MEM_AVAIL",
    "KEY_CONNECTIONS", "KEY_AGENT_VERSION",
    "KEY_PING_LOSS", "KEY_PING_RTT", "KEY_SERVICE_BPS",
    "_get_regional_traffic_keys",
    # Re-exports from fetch.py for backward compatibility
    "fetch_all_data", "fetch_trends_batch", "fetch_enabled_hosts",
    "fetch_traffic_map", "fetch_cpu_map", "fetch_service_status", "fetch_host_dashboards",
]

_COUNTRY_RE = re.compile(
    r"(?:[-_]([a-z]{2})\d)"       # nl0105, de0267
    r"|(?:[-_]([a-z]{2})[-_])",   # -in-lite, -us-lite
    re.IGNORECASE,
)
GB_BYTES = 1_073_741_824  # 1 GB in bytes

# Zabbix API filter values used across many calls
STATUS_ENABLED = "0"
# service health check item keys — configurable per deployment
KEY_service_PRIMARY = os.environ.get("ZABBIX_SERVICE_CHECK_KEY", "")
KEY_service_SECONDARY = os.environ.get("ZABBIX_SERVICE2_CHECK_KEY", "")
KEY_service_TERTIARY = os.environ.get("ZABBIX_SERVICE3_CHECK_KEY", "")
KEY_CONNECTIONS = os.environ.get("ZABBIX_CONNECTIONS_KEY", "")
# Network-quality item keys — configurable per deployment.
KEY_PING_LOSS = os.environ.get("ZABBIX_PING_LOSS_KEY", "")
KEY_PING_RTT = os.environ.get("ZABBIX_PING_RTT_KEY", "")
# Optional service-port BPS key for service-vs-mgmt traffic split detection.
KEY_SERVICE_BPS = os.environ.get("ZABBIX_SERVICE_BPS_KEY", "")


def _get_regional_traffic_keys() -> dict[str, str]:
    """Read ZABBIX_REGIONAL_TRAFFIC_KEYS as a JSON {region: item_key} map.

    Returns {} when the env var is missing or unparseable.
    """
    raw = os.environ.get("ZABBIX_REGIONAL_TRAFFIC_KEYS", "")
    if not raw:
        return {}
    import json as _json
    try:
        data = _json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if k and v}
# Products to hide from all reports (comma-separated)
# Read fresh from env on every call — no caching, avoids import-time race
def _get_hide_products() -> frozenset[str]:
    raw = os.environ.get("ZABBIX_HIDE_PRODUCTS", "")
    if not raw:
        return frozenset()
    return frozenset(p.strip() for p in raw.split(",") if p.strip())


HIDE_PRODUCTS = frozenset()  # backward compat — use is_hidden_product() instead
# Standard Zabbix agent keys
KEY_CPU_IDLE = "system.cpu.util[,idle]"
KEY_CPU_LOAD = "system.cpu.load[percpu,avg5]"
KEY_MEM_AVAIL = "vm.memory.size[available]"
KEY_AGENT_VERSION = "agent.version"

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


_COUNTRY_ALIASES = {"UK": "GB"}  # normalize non-ISO codes


def extract_country(hostname: str) -> str:
    """Extract 2-letter country code from hostname.

    Handles: srv-nl0105 → NL, srv-nl01-lite → NL, srv-us01-lite → US
    Normalizes UK → GB.
    """
    m = _COUNTRY_RE.search(hostname)
    if not m:
        return ""
    cc = (m.group(1) or m.group(2) or "").upper()
    return _COUNTRY_ALIASES.get(cc, cc)


def build_parent_map(hosts: list[dict]) -> dict[str, str]:
    """Build child→parent hostid map for endpoint sub-hosts.

    Pattern: parent ``relay-xx01``, child ``relay-xx01 xx02``.
    Child hostname = parent hostname + space + suffix.
    """
    name_to_id: dict[str, str] = {}
    for h in hosts:
        name_to_id[h.get("host", "")] = h["hostid"]

    parent_map: dict[str, str] = {}
    for h in hosts:
        hostname = h.get("host", "")
        if " " in hostname:
            parent_name = hostname.split(" ", 1)[0]
            pid = name_to_id.get(parent_name)
            if pid and pid != h["hostid"]:
                parent_map[h["hostid"]] = pid
    return parent_map


def _resolve(metric_map: dict, hid: str, parent_map: dict[str, str]):
    """Get metric for host, falling back to parent if missing."""
    val = metric_map.get(hid)
    if val is not None:
        return val
    pid = parent_map.get(hid)
    return metric_map.get(pid) if pid else None


def is_hidden_product(product: str) -> bool:
    """Check if a product should be hidden from reports."""
    hide = _get_hide_products()
    if not hide:
        return False
    return product.lower() in {p.lower() for p in hide}


def group_by_country(
    hosts: list[dict],
    *,
    country: str = "",
    region: str = "",
    product: str = "",
) -> dict[str, list[dict]]:
    """Group hosts by country code, with optional filters."""
    region_codes = countries_for_region(region) if region else set()
    result: dict[str, list[dict]] = {}
    for h in hosts:
        cc = extract_country(h.get("host", ""))
        if not cc:
            continue
        if country and cc.lower() != country.lower():
            continue
        if region_codes and cc not in region_codes:
            continue
        prod, _ = _classify_host(h.get("groups", []))
        if is_hidden_product(prod):
            continue
        if product and (not prod or product.lower() not in prod.lower()):
            continue
        result.setdefault(cc, []).append(h)
    return result


def host_ip(h: dict) -> str:
    """Extract first non-loopback IP from host interfaces."""
    return next((i["ip"] for i in h.get("interfaces", []) if i.get("ip") != "127.0.0.1"), "")


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
    service_primary: str = ""
    agent: str = ""
    service_secondary: str = ""
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
            "service Primary": self.service_primary,
            "service Secondary": self.service_secondary,
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


# Re-exports from fetch.py — allows existing `from zbbx_mcp.data import fetch_*` to keep working
from zbbx_mcp.fetch import (  # noqa: E402, F401
    fetch_all_data,
    fetch_cpu_map,
    fetch_enabled_hosts,
    fetch_host_dashboards,
    fetch_service_status,
    fetch_traffic_map,
    fetch_trends_batch,
)
