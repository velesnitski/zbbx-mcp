"""Async data fetching functions for Zabbix API.

Separated from data.py to isolate async I/O from pure helpers/constants/types.
All functions take a ZabbixClient and return structured data.
"""

from __future__ import annotations

import asyncio
import time as _time
from datetime import datetime, timezone
from typing import Any

from zbbx_mcp.classify import classify_host as _classify_host
from zbbx_mcp.classify import detect_provider
from zbbx_mcp.client import ZabbixClient
from zbbx_mcp.data import (
    GB_BYTES,
    KEY_AGENT_VERSION,
    KEY_CONNECTIONS,
    KEY_CPU_IDLE,
    KEY_CPU_LOAD,
    KEY_MEM_AVAIL,
    KEY_service_PRIMARY,
    KEY_service_SECONDARY,
    KEY_service_TERTIARY,
    METRIC_KEYS,
    STATUS_ENABLED,
    TRAFFIC_IN_KEYS,
    TRAFFIC_OUT_KEYS,
    FetchResult,
    ServerRow,
    TrendRow,
    _parse_period,
    _resolve,
    build_max_map,
    build_parent_map,
    build_value_map,
    extract_country,
    is_hidden_product,
)
from zbbx_mcp.excel import BW_MAX, classify_bandwidth


async def fetch_enabled_hosts(
    client: ZabbixClient,
    *,
    groups: bool = True,
    interfaces: bool = True,
    extra_output: list[str] | None = None,
) -> list[dict]:
    """Fetch all enabled hosts with optional groups/interfaces."""
    # Use client-side cache for the common case (no extra_output)
    cache_key = f"enabled_hosts:g={groups}:i={interfaces}"
    if not extra_output:
        cached = client._get_cached(cache_key, ttl=60.0)
        if cached is not None:
            return cached

    output = ["hostid", "host"]
    if extra_output:
        output.extend(extra_output)
    params: dict[str, Any] = {
        "output": output,
        "filter": {"status": STATUS_ENABLED},
        "sortfield": "host",
    }
    if groups:
        params["selectGroups"] = ["name"]
    if interfaces:
        params["selectInterfaces"] = ["ip"]
    result = await client.call("host.get", params)

    if not extra_output:
        client._set_cache(cache_key, result)
    return result


async def fetch_traffic_map(client: ZabbixClient, hostids: list[str]) -> dict[str, float]:
    """Fetch max inbound traffic (Mbps) per host. Returns {hostid: mbps}.

    Uses tag-based discovery (Application: Network interfaces) to find ALL
    physical NIC items, with fallback to TRAFFIC_IN_KEYS for older Zabbix.
    """
    if not hostids:
        return {}

    # Try tag-based discovery first (Zabbix 6.4+) — catches all NIC types
    try:
        items = await client.call("item.get", {
            "hostids": hostids,
            "output": ["hostid", "lastvalue", "key_"],
            "tags": [{"tag": "Application", "value": "Network interfaces", "operator": "1"}],
            "filter": {"status": STATUS_ENABLED},
            "search": {"key_": "net.if.in["},
            "searchWildcardsEnabled": True,
        })
    except Exception:
        items = []

    # Fallback: hardcoded NIC keys (for Zabbix < 6.4 or if tags not configured)
    if not items:
        items = await client.call("item.get", {
            "hostids": hostids,
            "output": ["hostid", "lastvalue", "key_"],
            "filter": {"key_": TRAFFIC_IN_KEYS},
        })

    # Filter out virtual/tunnel interfaces — keep only physical NICs (eth, eno, enp, ens, bond, ppp)
    _PHYSICAL = ("eth", "eno", "enp", "ens", "bond", "ppp")

    result: dict[str, float] = {}
    for it in items:
        try:
            key = it.get("key_", "")
            # Extract interface name from net.if.in[iface]
            iface = key.split("[")[1].rstrip("]") if "[" in key else ""
            if not any(iface.startswith(p) for p in _PHYSICAL):
                continue
            mbps = float(it.get("lastvalue", 0)) / 1_000_000
            hid = it["hostid"]
            if hid not in result or mbps > result[hid]:
                result[hid] = mbps
        except (ValueError, TypeError, IndexError):
            pass
    return result


async def fetch_host_dashboards(client: ZabbixClient) -> dict[str, str]:
    """Build host_id → 'Dashboard - Page' label lookup. One API call."""
    dashboards = await client.call("dashboard.get", {
        "output": ["dashboardid", "name"],
        "selectPages": "extend",
    })
    result: dict[str, str] = {}
    for d in dashboards:
        dname = d.get("name", "?")
        # Strip common prefix (product name + separator) for compact display
        short = dname.split(" - ", 1)[-1] if " - " in dname else dname
        for _pi, page in enumerate(d.get("pages", [])):
            pname = page.get("name", "").strip()
            label = f"{short} - {pname}" if pname else short
            for w in page.get("widgets", []):
                for f in w.get("fields", []):
                    if f.get("type") == "3":  # host reference
                        hid = f["value"]
                        if hid not in result:
                            result[hid] = label
    return result


async def fetch_service_status(client: ZabbixClient, hostids: list[str]) -> dict[str, int]:
    """Fetch combined service status per host. Checks all configured service keys.

    Returns {hostid: status} where:
      1 = all protocols OK
     -1 = PARTIAL (some OK, some DOWN)
      0 = all DOWN
      (missing) = no service checks configured for this host
    """
    if not hostids:
        return {}
    keys = [k for k in (KEY_service_PRIMARY, KEY_service_SECONDARY, KEY_service_TERTIARY) if k]
    if not keys:
        return {}

    items = await client.call("item.get", {
        "hostids": hostids,
        "output": ["hostid", "lastvalue", "key_"],
        "filter": {"key_": keys, "status": STATUS_ENABLED},
    })

    # Per host: count OK and total checks
    host_ok: dict[str, int] = {}
    host_total: dict[str, int] = {}
    for it in items:
        hid = it["hostid"]
        host_total[hid] = host_total.get(hid, 0) + 1
        try:
            if int(float(it.get("lastvalue", 0))) == 1:
                host_ok[hid] = host_ok.get(hid, 0) + 1
        except (ValueError, TypeError):
            pass

    result: dict[str, int] = {}
    for hid in host_total:
        ok = host_ok.get(hid, 0)
        total = host_total[hid]
        if ok == total:
            result[hid] = 1       # all OK
        elif ok > 0:
            result[hid] = -1      # PARTIAL
        else:
            result[hid] = 0       # all DOWN
    return result


async def fetch_cpu_map(client: ZabbixClient, hostids: list[str]) -> dict[str, float]:
    """Fetch CPU usage % per host (idle → used). Returns {hostid: cpu_pct}."""
    if not hostids:
        return {}
    items = await client.call("item.get", {
        "hostids": hostids,
        "output": ["hostid", "lastvalue"],
        "filter": {"key_": "system.cpu.util[,idle]"},
    })
    result: dict[str, float] = {}
    for it in items:
        try:
            result[it["hostid"]] = round(100 - float(it["lastvalue"]), 1)
        except (ValueError, TypeError):
            pass
    return result


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
                "filter": {"status": STATUS_ENABLED},
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
    parent_map = build_parent_map(hosts)
    # Include parent hostids so their metrics are fetched too
    parent_ids = set(parent_map.values()) - set(host_map.keys())
    all_ids = list(host_map.keys()) + list(parent_ids)

    # Phase 2: all metrics in parallel
    graph_task = (
        client.call("graph.get", {
            "graphids": list(all_graph_ids),
            "output": ["graphid"],
            "selectHosts": ["hostid"],
        }) if all_graph_ids else asyncio.sleep(0)
    )

    async def _noop():
        return []

    def _item_call(key):
        if not key:
            return _noop()
        return client.call("item.get", {"hostids": all_ids, "output": ["hostid", "lastvalue"],
                                         "filter": {"key_": key, "status": STATUS_ENABLED}})

    results = await asyncio.gather(
        _item_call(KEY_CPU_IDLE),
        _item_call(KEY_CPU_LOAD),
        _item_call(KEY_MEM_AVAIL),
        _item_call(KEY_CONNECTIONS),
        # Traffic: filter by known physical interface keys
        client.call("item.get", {"hostids": all_ids, "output": ["hostid", "lastvalue"],
                                  "filter": {"key_": TRAFFIC_IN_KEYS, "status": STATUS_ENABLED}}),
        client.call("item.get", {"hostids": all_ids, "output": ["hostid", "lastvalue"],
                                  "filter": {"key_": TRAFFIC_OUT_KEYS, "status": STATUS_ENABLED}}),
        _item_call(KEY_AGENT_VERSION),
        _item_call(KEY_service_PRIMARY),
        _item_call(KEY_service_SECONDARY),
        _item_call(KEY_service_TERTIARY),
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
                    "filter": {"status": STATUS_ENABLED},
                }),
                client.call("item.get", {
                    "hostids": missing_hosts,
                    "output": ["hostid", "lastvalue"],
                    "search": {"name": "Outgoing network traffic"},
                    "filter": {"status": STATUS_ENABLED},
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
        in_traffic = _resolve(in_traffic_map, hid, parent_map)
        out_traffic = _resolve(out_traffic_map, hid, parent_map)
        in_mbps = round(in_traffic / 1e6, 1) if in_traffic else None
        out_mbps = round(out_traffic / 1e6, 1) if out_traffic else None
        total_mbps = round((in_mbps or 0) + (out_mbps or 0), 1) if (in_mbps or out_mbps) else None
        cost = cost_map.get(hid)
        service1_val = service1_map.get(hid)
        service2_val = service2_map.get(hid)
        service3_val = service3_map.get(hid)
        version = _resolve(version_map, hid, parent_map)
        templates = template_map.get(hid) or (template_map.get(parent_map.get(hid, ""), "") if hid in parent_map else "")

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
            cpu_pct=_resolve(cpu_map, hid, parent_map),
            load_avg5=_resolve(load_map, hid, parent_map),
            mem_avail_gb=_resolve(mem_map, hid, parent_map),
            traffic_in_mbps=in_mbps,
            traffic_out_mbps=out_mbps,
            traffic_total_mbps=total_mbps,
            bw_util_pct=round(in_mbps / (bw_limit_map.get(hid, BW_MAX)) * 100, 1) if in_mbps else None,
            bw_tier=classify_bandwidth(in_mbps),
            connections=_resolve(conn_map, hid, parent_map),
            service_primary="OK" if service1_val == 1 else ("DOWN" if service1_val is not None else ""),
            service_secondary="OK" if service2_val == 1 else ("DOWN" if service2_val is not None else ""),
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
            "filter": {"key_": all_keys, "status": STATUS_ENABLED},
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
