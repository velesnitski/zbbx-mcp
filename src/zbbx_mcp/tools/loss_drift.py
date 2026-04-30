"""Sustained ping-loss drift detection.

Early-signal tool: a host that is fully reachable today but whose ping loss
or round-trip time has stepped upward against its 14-day baseline is often
days away from a full traffic disruption. The traffic-drop tools see the
disruption *after* it lands; this tool fires before.

Item keys are configured via env (`ZABBIX_PING_LOSS_KEY`,
`ZABBIX_PING_RTT_KEY`) — there are no hardcoded keys in this module.
"""

from __future__ import annotations

import time as _time

import httpx

from zbbx_mcp.data import KEY_PING_LOSS, KEY_PING_RTT, STATUS_ENABLED, extract_country, host_ip
from zbbx_mcp.resolver import InstanceResolver

# Last `_RECENT_DAYS` of the window are compared against the rest as baseline.
_RECENT_DAYS = 2


def _split_baseline_recent(
    trends: list[dict],
    cutoff_clock: int,
) -> tuple[float | None, float | None]:
    """Average a trend list into (baseline_avg, recent_avg) around cutoff_clock.

    `trends` items must carry `clock` (epoch seconds) and `value_avg` (numeric
    string). Records at or after `cutoff_clock` form the recent window;
    earlier records form the baseline. Either side can be None.
    """
    base_vals: list[float] = []
    recent_vals: list[float] = []
    for t in trends:
        try:
            clock = int(t.get("clock", 0))
            v = float(t.get("value_avg", "0") or 0)
        except (ValueError, TypeError):
            continue
        if clock >= cutoff_clock:
            recent_vals.append(v)
        else:
            base_vals.append(v)
    base = sum(base_vals) / len(base_vals) if base_vals else None
    recent = sum(recent_vals) / len(recent_vals) if recent_vals else None
    return base, recent


def _compute_loss_drift(
    loss_baseline: float | None,
    loss_recent: float | None,
    rtt_baseline: float | None,
    rtt_recent: float | None,
    *,
    loss_step: float = 5.0,
    rtt_step_pct: float = 50.0,
) -> tuple[str, dict]:
    """Classify a host's network-quality drift.

    Returns (label, details). Label is one of:
        new-loss      — loss baseline < 1%, recent ≥ loss_step
        loss-up       — recent loss exceeds baseline by ≥ loss_step
        rtt-up        — recent RTT exceeds baseline by ≥ rtt_step_pct
        loss-and-rtt  — both flags fire
        ok            — neither flag fires
        n/a           — insufficient data on both signals
    """
    flags = set()
    details = {
        "loss_baseline": loss_baseline,
        "loss_recent": loss_recent,
        "rtt_baseline": rtt_baseline,
        "rtt_recent": rtt_recent,
        "loss_delta": None,
        "rtt_delta_pct": None,
    }

    if loss_baseline is not None and loss_recent is not None:
        delta = loss_recent - loss_baseline
        details["loss_delta"] = delta
        if delta >= loss_step:
            flags.add("loss-up")
            if loss_baseline < 1.0:
                flags.add("new-loss")

    if rtt_baseline is not None and rtt_recent is not None and rtt_baseline > 0:
        delta_pct = (rtt_recent - rtt_baseline) / rtt_baseline * 100.0
        details["rtt_delta_pct"] = delta_pct
        if delta_pct >= rtt_step_pct:
            flags.add("rtt-up")

    if not flags:
        if loss_baseline is None and loss_recent is None and rtt_baseline is None and rtt_recent is None:
            return "n/a", details
        return "ok", details

    if "new-loss" in flags:
        return "new-loss", details  # most actionable — surface first
    if "loss-up" in flags and "rtt-up" in flags:
        return "loss-and-rtt", details
    if "rtt-up" in flags:
        return "rtt-up", details
    return "loss-up", details


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()) -> None:

    if "detect_loss_drift" not in skip:

        @mcp.tool()
        async def detect_loss_drift(
            country: str = "",
            window_days: int = 14,
            loss_step: float = 5.0,
            rtt_step_pct: float = 50.0,
            max_results: int = 50,
            instance: str = "",
        ) -> str:
            """Find hosts whose ping-loss or RTT has drifted upward vs 14d baseline.

            Compares the last 2 days against the prior `window_days - 2` baseline
            using the env-configured ping items (`ZABBIX_PING_LOSS_KEY` and
            `ZABBIX_PING_RTT_KEY`).

            Args:
                country: 2-letter country filter (optional)
                window_days: Total window for baseline + recent (default: 14)
                loss_step: Min absolute loss % increase to flag (default: 5.0)
                rtt_step_pct: Min RTT % increase to flag (default: 50.0)
                max_results: Maximum hosts to render (default: 50)
                instance: Zabbix instance name (optional)
            """
            if not KEY_PING_LOSS and not KEY_PING_RTT:
                return (
                    "No ping items configured. Set ZABBIX_PING_LOSS_KEY and/or "
                    "ZABBIX_PING_RTT_KEY in the environment."
                )
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

                key_filter: list[str] = []
                if KEY_PING_LOSS:
                    key_filter.append(KEY_PING_LOSS)
                if KEY_PING_RTT:
                    key_filter.append(KEY_PING_RTT)

                items = await client.call("item.get", {
                    "hostids": hostids,
                    "output": ["itemid", "hostid", "key_"],
                    "filter": {"key_": key_filter, "status": "0"},
                })
                if not items:
                    return "No ping items found on the selected hosts."

                # Group items by (hostid, kind)
                loss_items: dict[str, str] = {}
                rtt_items: dict[str, str] = {}
                for it in items:
                    if it.get("key_") == KEY_PING_LOSS:
                        loss_items[it["hostid"]] = it["itemid"]
                    elif it.get("key_") == KEY_PING_RTT:
                        rtt_items[it["hostid"]] = it["itemid"]

                all_item_ids = list(loss_items.values()) + list(rtt_items.values())
                if not all_item_ids:
                    return "No ping items found on the selected hosts."

                now = int(_time.time())
                time_from = now - window_days * 86400
                cutoff = now - _RECENT_DAYS * 86400

                trends = await client.call("trend.get", {
                    "itemids": all_item_ids,
                    "time_from": time_from,
                    "output": ["itemid", "clock", "value_avg"],
                    "limit": len(all_item_ids) * window_days * 24,
                })
                by_item: dict[str, list[dict]] = {}
                for t in trends:
                    by_item.setdefault(t["itemid"], []).append(t)

                results: list[dict] = []
                for hid in hostids:
                    loss_iid = loss_items.get(hid)
                    rtt_iid = rtt_items.get(hid)
                    loss_b, loss_r = _split_baseline_recent(by_item.get(loss_iid, []), cutoff) if loss_iid else (None, None)
                    rtt_b, rtt_r = _split_baseline_recent(by_item.get(rtt_iid, []), cutoff) if rtt_iid else (None, None)
                    label, details = _compute_loss_drift(
                        loss_b, loss_r, rtt_b, rtt_r,
                        loss_step=loss_step, rtt_step_pct=rtt_step_pct,
                    )
                    if label in {"ok", "n/a"}:
                        continue
                    h = host_map[hid]
                    results.append({
                        "host": h.get("host", ""),
                        "ip": host_ip(h),
                        "label": label,
                        "loss_baseline": details["loss_baseline"],
                        "loss_recent": details["loss_recent"],
                        "loss_delta": details["loss_delta"],
                        "rtt_baseline": details["rtt_baseline"],
                        "rtt_recent": details["rtt_recent"],
                        "rtt_delta_pct": details["rtt_delta_pct"],
                    })

                if not results:
                    return (
                        f"No drift detected (loss step ≥ {loss_step}%, RTT step ≥ "
                        f"{rtt_step_pct}%) in {len(hostids)} hosts."
                    )

                # new-loss > loss-and-rtt > loss-up > rtt-up
                priority = {"new-loss": 0, "loss-and-rtt": 1, "loss-up": 2, "rtt-up": 3}
                results.sort(key=lambda r: (
                    priority.get(r["label"], 9),
                    -(r["loss_delta"] or 0),
                ))
                shown = results[:max_results]

                lines = [
                    f"**{len(results)} hosts with network-quality drift** "
                    f"(window {window_days}d, recent {_RECENT_DAYS}d)\n",
                    "| Host | IP | Label | Loss base→recent | RTT base→recent |",
                    "|------|----|-------|------------------|-----------------|",
                ]
                for r in shown:
                    if r["loss_baseline"] is not None and r["loss_recent"] is not None:
                        loss_cell = f"{r['loss_baseline']:.1f}% → {r['loss_recent']:.1f}% (Δ +{r['loss_delta']:.1f})"
                    else:
                        loss_cell = "—"
                    if r["rtt_baseline"] is not None and r["rtt_recent"] is not None:
                        delta = r["rtt_delta_pct"] or 0
                        rtt_cell = f"{r['rtt_baseline']:.0f}ms → {r['rtt_recent']:.0f}ms (Δ +{delta:.0f}%)"
                    else:
                        rtt_cell = "—"
                    lines.append(
                        f"| {r['host']} | {r['ip']} | {r['label']} | {loss_cell} | {rtt_cell} |"
                    )
                if len(results) > max_results:
                    lines.append(f"\n*{len(results) - max_results} more omitted*")
                return "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"
