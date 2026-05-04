"""Composite risk and impact tools.

`get_at_risk_hosts` ranks hosts by likelihood of disruption in the next 24h
using three independent signals: (a) IP-rotation churn in the same /24,
(b) ping-loss / RTT drift, (c) age of the current external IP. Each signal
is normalised then combined with a weighted sum.

`get_disruption_blast_radius` reuses the cohort model from #43 (peer
headroom) and answers the operator question after a candidate drop:
"do peers absorb the load, stay flat, or also drain?" Connection-count
items (KEY_CONNECTIONS) are compared on a 1h pre/post window.
"""

from __future__ import annotations

import math
import time as _time

import httpx

from zbbx_mcp.classify import classify_host as _classify_host
from zbbx_mcp.data import (
    KEY_CONNECTIONS,
    KEY_PING_LOSS,
    KEY_PING_RTT,
    STATUS_ENABLED,
    extract_country,
    host_ip,
)
from zbbx_mcp.resolver import InstanceResolver
from zbbx_mcp.tools.correlation import _subnet24
from zbbx_mcp.tools.ip_history import _parse_ip_changes
from zbbx_mcp.tools.loss_drift import _compute_loss_drift, _split_baseline_recent

# --- pure helpers -------------------------------------------------------

# Per-signal weights for the composite at-risk score.
_W_PEER = 1.5
_W_DRIFT = 2.0
_W_AGE = 0.5

_DRIFT_WEIGHT = {
    "loss-and-rtt": 2.0,
    "new-loss": 1.5,
    "loss-up": 1.0,
    "rtt-up": 0.5,
    "ok": 0.0,
    "n/a": 0.0,
}


def _compute_risk_score(
    peer_rotations_7d: int,
    drift_label: str,
    days_since_rotation: float | None,
) -> tuple[float, dict]:
    """Composite at-risk score with component breakdown.

    Components:
      peers_score = log1p(peer_rotations_7d)
      drift_score = lookup table on the drift label
      age_score   = log1p(days_since_rotation), capped at log1p(90)

    A higher score means more at-risk. None for `days_since_rotation` is
    treated as "no recent rotation observed" — caps at the 90d ceiling
    rather than zero so long-stable IPs are not artificially safe.
    """
    peers = math.log1p(max(0, peer_rotations_7d))
    drift = _DRIFT_WEIGHT.get(drift_label, 0.0)
    age_days_capped = 90 if days_since_rotation is None else min(max(0.0, days_since_rotation), 90.0)
    age = math.log1p(age_days_capped)
    total = _W_PEER * peers + _W_DRIFT * drift + _W_AGE * age
    return total, {
        "peers": peers,
        "drift": drift,
        "age": age,
        "peer_rotations_7d": peer_rotations_7d,
        "drift_label": drift_label,
        "days_since_rotation": days_since_rotation,
    }


def _compute_blast_radius(
    pre_count: float | None,
    post_count: float | None,
) -> tuple[str, float | None]:
    """Classify a peer's connection-count delta after a host drop.

    Returns (label, delta_pct). Labels:
        absorbing  — recent count up ≥ 10% (peer took the candidate's load)
        stable     — recent count within ±10% of pre
        draining   — recent count down > 10% (the disruption spread)
        n/a        — pre or post missing, or pre is non-positive
    """
    if pre_count is None or post_count is None or pre_count <= 0:
        return "n/a", None
    delta_pct = (post_count - pre_count) / pre_count * 100.0
    if delta_pct >= 10.0:
        return "absorbing", delta_pct
    if delta_pct <= -10.0:
        return "draining", delta_pct
    return "stable", delta_pct


# --- async fetch helpers ------------------------------------------------

async def _history_avg(
    client,
    item_ids: list[str],
    time_from: int,
    time_till: int,
) -> dict[str, float]:
    """itemid → avg of `history.get` numeric values in a window.

    Uses history rather than trend because connection-count windows are
    typically tighter than the 1h trend granularity.
    """
    if not item_ids:
        return {}
    records = await client.call("history.get", {
        "itemids": item_ids,
        "history": 0,  # numeric float
        "time_from": time_from,
        "time_till": time_till,
        "output": ["itemid", "value"],
        "limit": len(item_ids) * 600,
    })
    bucket: dict[str, list[float]] = {}
    for r in records:
        try:
            bucket.setdefault(r["itemid"], []).append(float(r.get("value", "0") or 0))
        except (ValueError, TypeError):
            continue
    return {iid: sum(vals) / len(vals) for iid, vals in bucket.items() if vals}


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()) -> None:

    if "get_at_risk_hosts" not in skip:

        @mcp.tool()
        async def get_at_risk_hosts(
            country: str = "",
            window_days: int = 7,
            ping_window_days: int = 14,
            top: int = 30,
            instance: str = "",
        ) -> str:
            """Rank hosts by likelihood of disruption in the next 24h.

            Composite of peer-rotation churn, loss/RTT drift, and IP age.
            See ADR 013, 014.

            Args:
                country: 2-letter country filter (optional)
                window_days: Audit-log window for peer/self rotations (default: 7)
                ping_window_days: Window for loss/RTT drift (default: 14)
                top: Top-N hosts to render (default: 30)
                instance: Zabbix instance name (optional)
            """
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

                # 1) Audit-log walk for IP changes within the window.
                now = int(_time.time())
                window_from = now - window_days * 86400
                audit = await client.call("auditlog.get", {
                    "output": ["clock", "details", "resourceid"],
                    "filter": {"resourcetype": 2, "action": 1, "resourceid": hostids},
                    "time_from": window_from,
                    "sortfield": "clock",
                    "sortorder": "DESC",
                    "limit": 5000,
                })
                # Per-host: sorted clocks of IP changes (most recent first).
                rotations_per_host: dict[str, list[int]] = {}
                for r in audit:
                    if not _parse_ip_changes(r.get("details", "")):
                        continue
                    hid = str(r.get("resourceid", ""))
                    try:
                        clock = int(r.get("clock", 0))
                    except (ValueError, TypeError):
                        continue
                    rotations_per_host.setdefault(hid, []).append(clock)

                # 2) Group rotations by /24 to compute peer churn.
                subnet_rotations: dict[str, int] = {}
                host_subnet: dict[str, str] = {}
                for hid in hostids:
                    h = host_map[hid]
                    subnet = _subnet24(host_ip(h))
                    host_subnet[hid] = subnet
                    if subnet:
                        subnet_rotations[subnet] = subnet_rotations.get(subnet, 0) + len(
                            rotations_per_host.get(hid, [])
                        )

                # 3) Loss / RTT drift via #115 helpers.
                drift_per_host: dict[str, str] = {}
                if KEY_PING_LOSS or KEY_PING_RTT:
                    keys = [k for k in (KEY_PING_LOSS, KEY_PING_RTT) if k]
                    items = await client.call("item.get", {
                        "hostids": hostids,
                        "output": ["itemid", "hostid", "key_"],
                        "filter": {"key_": keys, "status": "0"},
                    })
                    loss_items: dict[str, str] = {}
                    rtt_items: dict[str, str] = {}
                    for it in items:
                        if it.get("key_") == KEY_PING_LOSS:
                            loss_items[it["hostid"]] = it["itemid"]
                        elif it.get("key_") == KEY_PING_RTT:
                            rtt_items[it["hostid"]] = it["itemid"]
                    all_iids = list(loss_items.values()) + list(rtt_items.values())
                    cutoff = now - 2 * 86400
                    ping_from = now - ping_window_days * 86400
                    trends = await client.call("trend.get", {
                        "itemids": all_iids,
                        "time_from": ping_from,
                        "output": ["itemid", "clock", "value_avg"],
                        "limit": len(all_iids) * ping_window_days * 24,
                    }) if all_iids else []
                    by_item: dict[str, list[dict]] = {}
                    for t in trends:
                        by_item.setdefault(t["itemid"], []).append(t)
                    for hid in hostids:
                        loss_iid = loss_items.get(hid)
                        rtt_iid = rtt_items.get(hid)
                        loss_b, loss_r = (
                            _split_baseline_recent(by_item.get(loss_iid, []), cutoff)
                            if loss_iid else (None, None)
                        )
                        rtt_b, rtt_r = (
                            _split_baseline_recent(by_item.get(rtt_iid, []), cutoff)
                            if rtt_iid else (None, None)
                        )
                        label, _ = _compute_loss_drift(loss_b, loss_r, rtt_b, rtt_r)
                        drift_per_host[hid] = label

                # 4) Compose per-host score.
                rows: list[dict] = []
                for hid in hostids:
                    self_rot = rotations_per_host.get(hid, [])
                    subnet = host_subnet.get(hid, "")
                    peer_count = subnet_rotations.get(subnet, 0) - len(self_rot)
                    if peer_count < 0:
                        peer_count = 0
                    drift_label = drift_per_host.get(hid, "n/a")
                    days_since = (
                        (now - max(self_rot)) / 86400 if self_rot else None
                    )
                    # IP age alone is not a disruption predictor — skip hosts
                    # that have no peer churn AND no drift signal. Otherwise
                    # every never-rotated host scores W_AGE * log1p(90) ≈ 2.26
                    # and the ranking degenerates to "everyone is at risk".
                    if peer_count == 0 and drift_label in {"ok", "n/a"}:
                        continue
                    score, components = _compute_risk_score(peer_count, drift_label, days_since)
                    if score <= 0:
                        continue
                    h = host_map[hid]
                    rows.append({
                        "host": h.get("host", ""),
                        "ip": host_ip(h),
                        "subnet": subnet,
                        "score": score,
                        **components,
                    })

                if not rows:
                    return f"No at-risk hosts ranked > 0 ({len(hostids)} hosts inspected)."
                rows.sort(key=lambda r: -r["score"])
                shown = rows[:top]
                lines = [
                    f"**Top {len(shown)} at-risk hosts** ({len(rows)} ranked, "
                    f"window {window_days}d, ping {ping_window_days}d)\n",
                    "| # | Host | Score | Peer rot. | Drift | IP age (d) |",
                    "|--:|------|------:|----------:|-------|-----------:|",
                ]
                for idx, r in enumerate(shown, 1):
                    age = f"{r['days_since_rotation']:.0f}" if r["days_since_rotation"] is not None else "—"
                    lines.append(
                        f"| {idx} | {r['host']} | {r['score']:.2f} | "
                        f"{r['peer_rotations_7d']} | {r['drift_label']} | {age} |"
                    )
                if len(rows) > top:
                    lines.append(f"\n*{len(rows) - top} more omitted*")
                return "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

    if "get_disruption_blast_radius" not in skip:

        @mcp.tool()
        async def get_disruption_blast_radius(
            host: str,
            drop_clock: int = 0,
            window_min: int = 60,
            instance: str = "",
        ) -> str:
            """Measure cohort connection-count delta after a host drop.

            Cohort = (product, tier, country) peers. See ADR 013.

            Args:
                host: Hostname of the dropped server (required)
                drop_clock: Epoch seconds of the drop event (default: now − 1h)
                window_min: Pre/post window minutes (default: 60)
                instance: Zabbix instance name (optional)
            """
            if not host:
                return "Argument `host` is required."
            if not KEY_CONNECTIONS:
                return "Not configured. Set ZABBIX_CONNECTIONS_KEY in the environment."
            try:
                client = resolver.resolve(instance)
                # Resolve candidate host + cohort peers.
                hosts = await client.call("host.get", {
                    "output": ["hostid", "host"],
                    "selectGroups": ["name"],
                    "selectInterfaces": ["ip"],
                    "filter": {"status": STATUS_ENABLED},
                })
                candidate = next((h for h in hosts if h.get("host") == host), None)
                if not candidate:
                    return f"Host not found: {host}"

                cand_country = extract_country(candidate.get("host", ""))
                cand_prod, cand_tier = _classify_host(candidate.get("groups", []))
                cohort = []
                for h in hosts:
                    if h["hostid"] == candidate["hostid"]:
                        continue
                    if extract_country(h.get("host", "")) != cand_country:
                        continue
                    p, t = _classify_host(h.get("groups", []))
                    if p != cand_prod or t != cand_tier:
                        continue
                    cohort.append(h)

                if not cohort:
                    return (
                        f"No cohort peers for {host} "
                        f"(product={cand_prod}, tier={cand_tier}, country={cand_country})."
                    )

                cohort_ids = [h["hostid"] for h in cohort]
                items = await client.call("item.get", {
                    "hostids": cohort_ids,
                    "output": ["itemid", "hostid"],
                    "filter": {"key_": KEY_CONNECTIONS, "status": "0"},
                })
                if not items:
                    return f"No connection-count items found on {len(cohort)} cohort peers."
                iid_to_hid = {it["itemid"]: it["hostid"] for it in items}

                now = int(_time.time())
                anchor = drop_clock if drop_clock > 0 else now - 3600
                pre_from = anchor - window_min * 60
                pre_till = anchor
                post_from = anchor
                post_till = anchor + window_min * 60
                if post_till > now:
                    post_till = now

                pre_avg = await _history_avg(client, list(iid_to_hid.keys()), pre_from, pre_till)
                post_avg = await _history_avg(client, list(iid_to_hid.keys()), post_from, post_till)

                rows: list[dict] = []
                summary = {"absorbing": 0, "stable": 0, "draining": 0, "n/a": 0}
                for iid, hid in iid_to_hid.items():
                    label, delta_pct = _compute_blast_radius(pre_avg.get(iid), post_avg.get(iid))
                    summary[label] += 1
                    h = next((x for x in cohort if x["hostid"] == hid), None)
                    if h is None:
                        continue
                    rows.append({
                        "host": h.get("host", ""),
                        "label": label,
                        "delta_pct": delta_pct,
                        "pre": pre_avg.get(iid),
                        "post": post_avg.get(iid),
                    })

                # Show the most informative rows: draining first, then absorbing.
                priority = {"draining": 0, "absorbing": 1, "stable": 2, "n/a": 3}
                rows.sort(key=lambda r: (priority[r["label"]], -(r["delta_pct"] or 0)))

                from datetime import datetime, timezone
                ts = datetime.fromtimestamp(anchor, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                lines = [
                    f"**Blast radius for {host}** (drop ~{ts}, "
                    f"cohort {cand_prod}/{cand_tier}/{cand_country}, {len(cohort)} peers)\n",
                    f"Summary: {summary['draining']} draining, "
                    f"{summary['absorbing']} absorbing, {summary['stable']} stable, "
                    f"{summary['n/a']} n/a.\n",
                    "| Peer | Label | Δ% | Pre | Post |",
                    "|------|-------|---:|----:|-----:|",
                ]
                for r in rows[:20]:
                    pre = f"{r['pre']:.0f}" if r["pre"] is not None else "—"
                    post = f"{r['post']:.0f}" if r["post"] is not None else "—"
                    delta = f"{r['delta_pct']:+.0f}%" if r["delta_pct"] is not None else "—"
                    lines.append(f"| {r['host']} | {r['label']} | {delta} | {pre} | {post} |")
                if len(rows) > 20:
                    lines.append(f"\n*{len(rows) - 20} more peers omitted*")
                return "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"
