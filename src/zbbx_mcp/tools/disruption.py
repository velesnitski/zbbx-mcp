"""Disruption-detection tools: service-port split, regional loss, drop wave.

Three independent traffic-side analyses that share a common shape — a
baseline-vs-recent comparison over a configurable window — but answer
different questions:

- `detect_service_port_split` — does the service-port traffic on a
  host diverge from its management-NIC traffic? (selective disruption)
- `detect_regional_traffic_loss` — does inbound traffic from one
  source region collapse while others stay flat? (geographic
  disruption; requires per-region item keys via env)
- `detect_disruption_wave` — do many hosts across many /24s drop
  inside the same hour? (wide-blast event)
"""

from __future__ import annotations

import time as _time

import httpx

from zbbx_mcp.data import (
    KEY_SERVICE_BPS,
    STATUS_ENABLED,
    TRAFFIC_IN_KEYS,
    _get_regional_traffic_keys,
    extract_country,
    host_ip,
)
from zbbx_mcp.resolver import InstanceResolver
from zbbx_mcp.tools.correlation import _subnet24

# --- shared pure helpers ------------------------------------------------

_RECENT_DEFAULT_DAYS = 1


def _classify_service_split(
    service_baseline: float | None,
    service_recent: float | None,
    mgmt_baseline: float | None,
    mgmt_recent: float | None,
    *,
    service_drop_pct: float = 50.0,
    mgmt_drop_pct: float = 10.0,
) -> tuple[str, dict]:
    """Compare service-port vs management-NIC traffic deltas on one host.

    Returns (label, details). Label vocabulary:
        split        — service dropped ≥ service_drop_pct, mgmt drop < mgmt_drop_pct
        full-outage  — both dropped (different concern: host-level disruption)
        ok           — neither dropped
        n/a          — insufficient data on either side
    """
    details = {
        "service_baseline": service_baseline,
        "service_recent": service_recent,
        "mgmt_baseline": mgmt_baseline,
        "mgmt_recent": mgmt_recent,
        "service_drop_pct": None,
        "mgmt_drop_pct": None,
    }
    if (service_baseline is None or service_recent is None
            or mgmt_baseline is None or mgmt_recent is None):
        return "n/a", details
    if service_baseline <= 0 or mgmt_baseline <= 0:
        return "n/a", details

    s_drop = (service_baseline - service_recent) / service_baseline * 100.0
    m_drop = (mgmt_baseline - mgmt_recent) / mgmt_baseline * 100.0
    details["service_drop_pct"] = s_drop
    details["mgmt_drop_pct"] = m_drop

    s_collapsed = s_drop >= service_drop_pct
    m_collapsed = m_drop >= mgmt_drop_pct
    if s_collapsed and m_collapsed:
        return "full-outage", details
    if s_collapsed and not m_collapsed:
        return "split", details
    return "ok", details


def _classify_regional_loss(
    per_region: dict[str, tuple[float | None, float | None]],
    *,
    drop_threshold: float = 30.0,
    flat_threshold: float = 10.0,
) -> list[dict]:
    """Flag regions whose recent traffic collapsed while other regions stayed flat.

    `per_region` maps region label → (baseline_avg, recent_avg). A region
    collapses when its drop ≥ drop_threshold *and* at least one peer region
    stayed inside ±flat_threshold of its baseline. Regions reported here
    are the suspect ones; ok/flat regions are left out.
    """
    deltas: dict[str, float | None] = {}
    for region, (baseline, recent) in per_region.items():
        if baseline is None or recent is None or baseline <= 0:
            deltas[region] = None
            continue
        deltas[region] = (baseline - recent) / baseline * 100.0

    has_flat_peer = any(
        d is not None and abs(d) <= flat_threshold for d in deltas.values()
    )

    out: list[dict] = []
    for region, drop_pct in deltas.items():
        if drop_pct is None:
            continue
        if drop_pct < drop_threshold:
            continue
        # A solo collapse (no flat peer to contrast) is still informative,
        # but we mark it differently.
        label = "collapsed" if has_flat_peer else "solo-drop"
        out.append({
            "region": region,
            "drop_pct": drop_pct,
            "baseline": per_region[region][0],
            "recent": per_region[region][1],
            "label": label,
        })
    out.sort(key=lambda r: -r["drop_pct"])
    return out


def _compute_waves(
    hourly_drops: list[dict],
    *,
    window_sec: int = 3600,
    min_hosts: int = 5,
    min_subnets: int = 3,
) -> list[dict]:
    """Greedy time-window grouping of dropped hosts into wave events.

    Each input record carries `clock`, `hostid`, `host`, `subnet`,
    `hostgroup`, and `drop_pct`. A wave fires when a maximal-run group
    inside `window_sec` covers at least `min_hosts` distinct hostids
    spanning at least `min_subnets` distinct /24s.
    """
    if not hourly_drops:
        return []
    sorted_drops = sorted(hourly_drops, key=lambda r: r["clock"])
    waves: list[dict] = []
    i = 0
    n = len(sorted_drops)
    while i < n:
        j = i
        while j + 1 < n and sorted_drops[j + 1]["clock"] - sorted_drops[i]["clock"] <= window_sec:
            j += 1
        bucket = sorted_drops[i:j + 1]
        hosts = {r["hostid"] for r in bucket}
        subnets = {r["subnet"] for r in bucket if r.get("subnet")}
        if len(hosts) >= min_hosts and len(subnets) >= min_subnets:
            avg_drop = sum(r["drop_pct"] for r in bucket) / len(bucket)
            severity = "critical" if avg_drop >= 75 else "high" if avg_drop >= 50 else "medium"
            waves.append({
                "start": bucket[0]["clock"],
                "end": bucket[-1]["clock"],
                "host_count": len(hosts),
                "subnet_count": len(subnets),
                "hosts": sorted({r["host"] for r in bucket}),
                "subnets": sorted(subnets),
                "hostgroups": sorted({r["hostgroup"] for r in bucket if r.get("hostgroup")}),
                "avg_drop_pct": avg_drop,
                "severity": severity,
            })
            i = j + 1
        else:
            i += 1
    waves.sort(key=lambda w: (-w["host_count"], -w["avg_drop_pct"]))
    return waves


# --- async fetch helpers -----------------------------------------------

async def _trends_window_avg(
    client,
    item_ids: list[str],
    time_from: int,
    time_till: int,
) -> dict[str, float]:
    """itemid → mean(value_avg) over [time_from, time_till)."""
    if not item_ids:
        return {}
    trends = await client.call("trend.get", {
        "itemids": item_ids,
        "time_from": time_from,
        "time_till": time_till,
        "output": ["itemid", "value_avg"],
        "limit": len(item_ids) * 24 * 14,
    })
    bucket: dict[str, list[float]] = {}
    for t in trends:
        try:
            bucket.setdefault(t["itemid"], []).append(float(t.get("value_avg", "0") or 0))
        except (ValueError, TypeError):
            continue
    return {iid: sum(vals) / len(vals) for iid, vals in bucket.items() if vals}


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()) -> None:

    if "detect_service_port_split" not in skip:

        @mcp.tool()
        async def detect_service_port_split(
            country: str = "",
            window_days: int = 7,
            recent_days: int = _RECENT_DEFAULT_DAYS,
            service_drop_pct: float = 50.0,
            mgmt_drop_pct: float = 10.0,
            max_results: int = 50,
            instance: str = "",
        ) -> str:
            """Find hosts where service-port traffic dropped but management is healthy.

            Item key from ZABBIX_SERVICE_BPS_KEY. See ADR 013.

            Args:
                country: 2-letter country filter (optional)
                window_days: Total window (default: 7)
                recent_days: Recent slice for drop comparison (default: 1)
                service_drop_pct: Min service drop to flag (default: 50)
                mgmt_drop_pct: Max mgmt drop to keep label as 'split' (default: 10)
                max_results: Maximum hosts (default: 50)
                instance: Zabbix instance name (optional)
            """
            if not KEY_SERVICE_BPS:
                return "Not configured. Set ZABBIX_SERVICE_BPS_KEY in the environment."
            try:
                client = resolver.resolve(instance)
                hosts = await client.call("host.get", {
                    "output": ["hostid", "host"],
                    "selectInterfaces": ["ip"],
                    "filter": {"status": STATUS_ENABLED},
                })
                if country:
                    cc = country.upper()
                    hosts = [h for h in hosts if extract_country(h.get("host", "")) == cc]
                if not hosts:
                    return "No matching hosts."

                hostids = [h["hostid"] for h in hosts]
                host_map = {h["hostid"]: h for h in hosts}

                key_filter = [KEY_SERVICE_BPS, *TRAFFIC_IN_KEYS]
                items = await client.call("item.get", {
                    "hostids": hostids,
                    "output": ["itemid", "hostid", "key_"],
                    "filter": {"key_": key_filter, "status": "0"},
                })
                service_iid: dict[str, str] = {}
                mgmt_iids: dict[str, list[str]] = {}
                for it in items:
                    hid = it["hostid"]
                    key = it.get("key_", "")
                    if key == KEY_SERVICE_BPS:
                        service_iid[hid] = it["itemid"]
                    else:
                        mgmt_iids.setdefault(hid, []).append(it["itemid"])

                if not service_iid:
                    return "No service-port items found on the selected hosts."

                now = int(_time.time())
                cutoff = now - recent_days * 86400
                baseline_from = now - window_days * 86400

                all_iids = list(service_iid.values()) + [
                    iid for ids in mgmt_iids.values() for iid in ids
                ]
                baseline_avg = await _trends_window_avg(client, all_iids, baseline_from, cutoff)
                recent_avg = await _trends_window_avg(client, all_iids, cutoff, now)

                def _agg(iids: list[str], src: dict[str, float]) -> float | None:
                    vals = [src[iid] for iid in iids if iid in src]
                    if not vals:
                        return None
                    return sum(vals)

                rows: list[dict] = []
                for hid, s_iid in service_iid.items():
                    s_b = baseline_avg.get(s_iid)
                    s_r = recent_avg.get(s_iid)
                    m_iids = mgmt_iids.get(hid, [])
                    m_b = _agg(m_iids, baseline_avg)
                    m_r = _agg(m_iids, recent_avg)
                    label, details = _classify_service_split(
                        s_b, s_r, m_b, m_r,
                        service_drop_pct=service_drop_pct,
                        mgmt_drop_pct=mgmt_drop_pct,
                    )
                    if label != "split":
                        continue
                    h = host_map[hid]
                    rows.append({
                        "host": h.get("host", ""),
                        "ip": host_ip(h),
                        "service_drop": details["service_drop_pct"],
                        "mgmt_drop": details["mgmt_drop_pct"],
                        "service_b": details["service_baseline"],
                        "service_r": details["service_recent"],
                    })

                if not rows:
                    return (
                        f"No service-vs-mgmt split detected ({len(hostids)} hosts, "
                        f"window {window_days}d, recent {recent_days}d)."
                    )
                rows.sort(key=lambda r: -r["service_drop"])
                shown = rows[:max_results]
                lines = [
                    f"**{len(rows)} hosts with service/mgmt traffic split** "
                    f"(window {window_days}d, recent {recent_days}d)\n",
                    "| Host | IP | Service drop | Mgmt drop | Service base→recent |",
                    "|------|----|-------------:|----------:|---------------------|",
                ]
                for r in shown:
                    sb = r["service_b"] / 1e6 if r["service_b"] is not None else 0
                    sr = r["service_r"] / 1e6 if r["service_r"] is not None else 0
                    lines.append(
                        f"| {r['host']} | {r['ip']} | "
                        f"{r['service_drop']:.0f}% | {r['mgmt_drop']:.0f}% | "
                        f"{sb:.1f} → {sr:.1f} Mbps |"
                    )
                if len(rows) > max_results:
                    lines.append(f"\n*{len(rows) - max_results} more omitted*")
                return "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

    if "detect_regional_traffic_loss" not in skip:

        @mcp.tool()
        async def detect_regional_traffic_loss(
            window_days: int = 7,
            recent_days: int = _RECENT_DEFAULT_DAYS,
            drop_threshold: float = 30.0,
            flat_threshold: float = 10.0,
            instance: str = "",
        ) -> str:
            """Find regions whose inbound traffic collapsed while peers stayed flat.

            Region → item-key map from ZABBIX_REGIONAL_TRAFFIC_KEYS. See ADR 013.

            Args:
                window_days: Total window (default: 7)
                recent_days: Recent slice (default: 1)
                drop_threshold: Min drop % to flag a region (default: 30)
                flat_threshold: Tolerance for 'flat' peer (default: 10 = ±10%)
                instance: Zabbix instance name (optional)
            """
            region_keys = _get_regional_traffic_keys()
            if not region_keys:
                return (
                    "Not configured. Set ZABBIX_REGIONAL_TRAFFIC_KEYS as a JSON "
                    "object mapping region label to item key."
                )
            try:
                client = resolver.resolve(instance)
                # Fetch all matching items in one call.
                items = await client.call("item.get", {
                    "output": ["itemid", "key_"],
                    "filter": {"key_": list(region_keys.values()), "status": "0"},
                })
                if not items:
                    return "No items match the configured region keys."

                key_to_iids: dict[str, list[str]] = {}
                for it in items:
                    key_to_iids.setdefault(it.get("key_", ""), []).append(it["itemid"])

                now = int(_time.time())
                cutoff = now - recent_days * 86400
                baseline_from = now - window_days * 86400
                all_iids = [iid for iids in key_to_iids.values() for iid in iids]
                baseline_avg = await _trends_window_avg(client, all_iids, baseline_from, cutoff)
                recent_avg = await _trends_window_avg(client, all_iids, cutoff, now)

                per_region: dict[str, tuple[float | None, float | None]] = {}
                for region, key in region_keys.items():
                    iids = key_to_iids.get(key, [])
                    b_vals = [baseline_avg[i] for i in iids if i in baseline_avg]
                    r_vals = [recent_avg[i] for i in iids if i in recent_avg]
                    per_region[region] = (
                        sum(b_vals) if b_vals else None,
                        sum(r_vals) if r_vals else None,
                    )

                flagged = _classify_regional_loss(
                    per_region,
                    drop_threshold=drop_threshold,
                    flat_threshold=flat_threshold,
                )
                if not flagged:
                    return (
                        f"No regional traffic loss (drop ≥ {drop_threshold:.0f}%) "
                        f"across {len(region_keys)} configured regions."
                    )
                lines = [
                    f"**{len(flagged)} regions with traffic loss** "
                    f"(window {window_days}d, recent {recent_days}d)\n",
                    "| Region | Label | Drop | Baseline | Recent |",
                    "|--------|-------|-----:|---------:|-------:|",
                ]
                for r in flagged:
                    b = (r["baseline"] or 0) / 1e6
                    rc = (r["recent"] or 0) / 1e6
                    lines.append(
                        f"| {r['region']} | {r['label']} | {r['drop_pct']:.0f}% | "
                        f"{b:.1f} Mbps | {rc:.1f} Mbps |"
                    )
                return "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

    if "detect_disruption_wave" not in skip:

        @mcp.tool()
        async def detect_disruption_wave(
            country: str = "",
            window_hours: int = 12,
            recent_hours: int = 2,
            drop_pct: float = 50.0,
            min_hosts: int = 5,
            min_subnets: int = 3,
            min_baseline_mbps: float = 5.0,
            instance: str = "",
        ) -> str:
            """Find waves where many hosts across many /24s drop in the same hour.

            Defaults are diurnal-safe. See ADR 013, 014.

            Args:
                country: 2-letter country filter (optional)
                window_hours: Total comparison window (default: 12)
                recent_hours: Recent slice (default: 2)
                drop_pct: Min drop to count a host (default: 50)
                min_hosts: Min hosts per wave (default: 5)
                min_subnets: Min distinct /24s per wave (default: 3)
                min_baseline_mbps: Skip hosts with baseline below this (default: 5.0)
                instance: Zabbix instance name (optional)
            """
            try:
                client = resolver.resolve(instance)
                hosts = await client.call("host.get", {
                    "output": ["hostid", "host"],
                    "selectGroups": ["name"],
                    "selectInterfaces": ["ip"],
                    "filter": {"status": STATUS_ENABLED},
                })
                if country:
                    cc = country.upper()
                    hosts = [h for h in hosts if extract_country(h.get("host", "")) == cc]
                if not hosts:
                    return "No matching hosts."

                hostids = [h["hostid"] for h in hosts]
                host_map = {h["hostid"]: h for h in hosts}

                items = await client.call("item.get", {
                    "hostids": hostids,
                    "output": ["itemid", "hostid"],
                    "filter": {"key_": TRAFFIC_IN_KEYS, "status": "0"},
                })
                iid_to_hid: dict[str, str] = {it["itemid"]: it["hostid"] for it in items}
                if not iid_to_hid:
                    return "No traffic items found on the selected hosts."

                now = int(_time.time())
                cutoff = now - recent_hours * 3600
                baseline_from = now - window_hours * 3600
                all_iids = list(iid_to_hid.keys())
                baseline = await _trends_window_avg(client, all_iids, baseline_from, cutoff)
                recent = await _trends_window_avg(client, all_iids, cutoff, now)

                # Aggregate per host (sum across multiple physical NICs).
                host_baseline: dict[str, float] = {}
                host_recent: dict[str, float] = {}
                for iid, hid in iid_to_hid.items():
                    if iid in baseline:
                        host_baseline[hid] = host_baseline.get(hid, 0) + baseline[iid]
                    if iid in recent:
                        host_recent[hid] = host_recent.get(hid, 0) + recent[iid]

                drops: list[dict] = []
                min_baseline_bps = min_baseline_mbps * 1e6
                for hid, b in host_baseline.items():
                    if b <= 0 or b < min_baseline_bps:
                        continue
                    r = host_recent.get(hid, 0)
                    drop = (b - r) / b * 100.0
                    if drop < drop_pct:
                        continue
                    h = host_map[hid]
                    ip = host_ip(h)
                    groups = [g.get("name", "") for g in h.get("groups", []) if g.get("name")]
                    drops.append({
                        "clock": cutoff,  # approximate the drop boundary
                        "hostid": hid,
                        "host": h.get("host", ""),
                        "subnet": _subnet24(ip),
                        "hostgroup": groups[0] if groups else "",
                        "drop_pct": drop,
                    })

                waves = _compute_waves(
                    drops,
                    window_sec=3600,
                    min_hosts=min_hosts,
                    min_subnets=min_subnets,
                )
                if not waves:
                    return (
                        f"No disruption waves ({len(drops)} hosts dropped, but no "
                        f"cluster met ≥{min_hosts} hosts × ≥{min_subnets} /24s)."
                    )
                lines = [
                    f"**{len(waves)} disruption waves** "
                    f"(≥{min_hosts} hosts × ≥{min_subnets} /24s, drop ≥ {drop_pct:.0f}%)\n",
                ]
                for idx, w in enumerate(waves, 1):
                    sample_hosts = ", ".join(w["hosts"][:5])
                    more = f" +{len(w['hosts']) - 5}" if len(w["hosts"]) > 5 else ""
                    lines.append(
                        f"### Wave {idx} — {w['severity']}\n"
                        f"- **Hosts:** {w['host_count']} ({sample_hosts}{more})\n"
                        f"- **Subnets:** {w['subnet_count']} ({', '.join(w['subnets'][:5])})\n"
                        f"- **Avg drop:** {w['avg_drop_pct']:.0f}%\n"
                    )
                return "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"
