"""External-IP rotation history with per-rotation recovery scoring.

For each enabled host, walk the Zabbix audit log for interface-IP updates,
then for each rotation compute traffic deltas across a 24h pre/post window
and label the rotation `recovered`, `partial`, or `still-down`.
"""

from __future__ import annotations

import json
import time as _time
from datetime import datetime, timezone

import httpx

from zbbx_mcp.data import STATUS_ENABLED, TRAFFIC_IN_KEYS, extract_country, host_ip
from zbbx_mcp.resolver import InstanceResolver

# Recovery-score thresholds expressed as post/baseline traffic ratio.
_RECOVERED = 0.7
_PARTIAL = 0.3


def _parse_ip_changes(details_raw: str) -> list[tuple[str, str]]:
    """Extract (old_ip, new_ip) tuples from a Zabbix auditlog `details` blob.

    The audit log details schema varies across Zabbix major versions; we
    accept the two shapes seen in 6.0+ (a JSON array of update tuples and
    a JSON object keyed by field path) and fall back to no-result for
    anything else. The parser only flags entries whose path contains
    `.ip` (so renames, status flips, etc. are ignored).
    """
    if not details_raw:
        return []
    try:
        parsed = json.loads(details_raw)
    except (json.JSONDecodeError, TypeError):
        return []

    out: list[tuple[str, str]] = []

    def _consume(action: str, path: str, old: object, new: object) -> None:
        if action and action.lower() not in {"update", "1"}:
            return
        if not isinstance(path, str) or ".ip" not in path:
            return
        if old is None or new is None:
            return
        old_s, new_s = str(old), str(new)
        if not old_s or not new_s or old_s == new_s:
            return
        out.append((old_s, new_s))

    if isinstance(parsed, list):
        # Shape: [["update", "interface.ip", "old", "new"], ...]
        for entry in parsed:
            if isinstance(entry, list) and len(entry) >= 4:
                _consume(str(entry[0]), str(entry[1]), entry[2], entry[3])
    elif isinstance(parsed, dict):
        # Shape: {"interfaces.123.ip": ["update", "old", "new"], ...}
        for path, entry in parsed.items():
            if not isinstance(entry, list) or len(entry) < 3:
                continue
            action = str(entry[0]) if entry else ""
            old, new = entry[-2], entry[-1]
            _consume(action, path, old, new)
    return out


def _aggregate_recovery_scores(rotations: list[dict]) -> dict:
    """Fleet-level KPI summary over a list of scored rotations.

    Each rotation must carry a `score` field set to one of the labels
    returned by `_score_recovery`. The aggregate exposes raw counts
    plus the recovery rate (recovered / total_with_outcome) so that
    rotations labelled `n/a` are excluded from the denominator. Returns
    `rate_pct = None` when no rotations had a determinable outcome.
    """
    counts = {"recovered": 0, "partial": 0, "still-down": 0, "n/a": 0}
    for r in rotations:
        label = r.get("score", "n/a")
        if label not in counts:
            label = "n/a"
        counts[label] += 1
    total = sum(counts.values())
    determined = total - counts["n/a"]
    rate_pct = (counts["recovered"] / determined * 100.0) if determined else None
    return {
        "total": total,
        "recovered": counts["recovered"],
        "partial": counts["partial"],
        "still_down": counts["still-down"],
        "na": counts["n/a"],
        "rate_pct": rate_pct,
    }


def _score_recovery(baseline_avg: float | None, post_avg: float | None) -> str:
    """Classify a rotation's traffic outcome.

    Returns one of: `recovered`, `partial`, `still-down`, `n/a`.

    `n/a` is used when either window is missing data — the IP change is
    real but we cannot judge its outcome without both windows.
    """
    if baseline_avg is None or post_avg is None:
        return "n/a"
    if baseline_avg <= 0:
        return "n/a"
    ratio = post_avg / baseline_avg
    if ratio >= _RECOVERED:
        return "recovered"
    if ratio >= _PARTIAL:
        return "partial"
    return "still-down"


async def _fetch_traffic_avg(
    client,
    hostid: str,
    time_from: int,
    time_till: int,
) -> float | None:
    """Average inbound traffic over the [time_from, time_till) window.

    Pulls all known physical-NIC items for the host and trend-aggregates
    them. Returns None when no trend data lands in the window.
    """
    items = await client.call("item.get", {
        "hostids": [hostid],
        "output": ["itemid"],
        "filter": {"key_": TRAFFIC_IN_KEYS, "status": "0"},
    })
    if not items:
        return None
    item_ids = [i["itemid"] for i in items]
    trends = await client.call("trend.get", {
        "itemids": item_ids,
        "time_from": time_from,
        "time_till": time_till,
        "output": ["clock", "value_avg"],
        "limit": 24 * len(item_ids),
    })
    if not trends:
        return None
    vals = []
    for t in trends:
        try:
            vals.append(float(t.get("value_avg", "0") or 0))
        except (ValueError, TypeError):
            continue
    if not vals:
        return None
    return sum(vals) / len(vals)


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()) -> None:

    if "get_external_ip_history" not in skip:

        @mcp.tool()
        async def get_external_ip_history(
            host: str = "",
            country: str = "",
            window_days: int = 30,
            max_hosts: int = 30,
            instance: str = "",
        ) -> str:
            """Per-host timeline of interface-IP rotations with recovery scoring.

            For each rotation found in the Zabbix audit log, traffic averages
            are compared across a 24h pre-window and a 24h post-window:

            - **recovered**: post/baseline ratio ≥ 0.7
            - **partial**: ratio in [0.3, 0.7)
            - **still-down**: ratio < 0.3
            - **n/a**: insufficient trend data in either window

            Args:
                host: Exact hostname (optional — if set, ignores country/max_hosts)
                country: 2-letter country filter (optional)
                window_days: Audit-log lookback (default: 30)
                max_hosts: Cap on hosts inspected when no host= specified (default: 30)
                instance: Zabbix instance name (optional)
            """
            try:
                client = resolver.resolve(instance)

                hosts = await client.call("host.get", {
                    "output": ["hostid", "host"],
                    "selectInterfaces": ["ip"],
                    "filter": {"status": STATUS_ENABLED},
                })
                if host:
                    hosts = [h for h in hosts if h.get("host") == host]
                else:
                    if country:
                        cc = country.upper()
                        hosts = [h for h in hosts if extract_country(h.get("host", "")) == cc]
                    hosts = hosts[:max_hosts]

                if not hosts:
                    return "No matching hosts."

                now = int(_time.time())
                time_from = now - window_days * 86400

                rows: list[dict] = []
                for h in hosts:
                    hid = h["hostid"]
                    records = await client.call("auditlog.get", {
                        "output": ["clock", "details"],
                        "filter": {"resourcetype": 2, "action": 1, "resourceid": hid},
                        "time_from": time_from,
                        "sortfield": "clock",
                        "sortorder": "ASC",
                        "limit": 200,
                    })
                    for r in records:
                        clock = int(r.get("clock", 0))
                        if clock <= 0:
                            continue
                        for old_ip, new_ip in _parse_ip_changes(r.get("details", "")):
                            baseline = await _fetch_traffic_avg(
                                client, hid, clock - 86400, clock,
                            )
                            post = await _fetch_traffic_avg(
                                client, hid, clock, clock + 86400,
                            )
                            score = _score_recovery(baseline, post)
                            rows.append({
                                "host": h["host"],
                                "ip": host_ip(h),
                                "clock": clock,
                                "old_ip": old_ip,
                                "new_ip": new_ip,
                                "baseline": baseline,
                                "post": post,
                                "score": score,
                            })

                if not rows:
                    return (
                        f"No external-IP rotations found in the last {window_days}d "
                        f"({len(hosts)} hosts inspected)."
                    )

                rows.sort(key=lambda r: (r["host"], r["clock"]))
                lines = [
                    f"**{len(rows)} external-IP rotations** "
                    f"(last {window_days}d, {len(hosts)} hosts inspected)\n",
                    "| Host | When | Old IP | New IP | Pre Mbps | Post Mbps | Outcome |",
                    "|------|------|--------|--------|---------:|----------:|---------|",
                ]
                for r in rows:
                    when = datetime.fromtimestamp(r["clock"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                    pre = f"{r['baseline'] / 1e6:.1f}" if r["baseline"] is not None else "—"
                    post = f"{r['post'] / 1e6:.1f}" if r["post"] is not None else "—"
                    lines.append(
                        f"| {r['host']} | {when} | {r['old_ip']} | {r['new_ip']} | "
                        f"{pre} | {post} | {r['score']} |"
                    )

                # Footer summary
                agg = _aggregate_recovery_scores(rows)
                rate = f"{agg['rate_pct']:.0f}%" if agg["rate_pct"] is not None else "—"
                lines.append(
                    f"\n*Outcomes: {agg['recovered']} recovered, {agg['partial']} partial, "
                    f"{agg['still_down']} still-down, {agg['na']} n/a "
                    f"(rate: {rate}).*"
                )
                return "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

    if "get_recovery_score" not in skip:

        @mcp.tool()
        async def get_recovery_score(
            country: str = "",
            window_days: int = 7,
            instance: str = "",
        ) -> str:
            """Fleet-wide recovery KPI aggregated over recent IP rotations.

            Walks every enabled host's audit log for IP rotations in the
            window, scores each rotation with the same 24h pre/post traffic
            comparison as get_external_ip_history, and returns a single KPI
            row: total rotations, count by outcome, and recovery rate.

            Args:
                country: 2-letter country filter (optional)
                window_days: Audit-log window (default: 7)
                instance: Zabbix instance name (optional)
            """
            try:
                client = resolver.resolve(instance)
                hosts = await client.call("host.get", {
                    "output": ["hostid", "host"],
                    "filter": {"status": STATUS_ENABLED},
                })
                if country:
                    cc = country.upper()
                    hosts = [h for h in hosts if extract_country(h.get("host", "")) == cc]
                if not hosts:
                    return "No matching hosts."

                hostids = [h["hostid"] for h in hosts]
                now = int(_time.time())
                time_from = now - window_days * 86400

                # Single audit-log query batched across all hosts.
                records = await client.call("auditlog.get", {
                    "output": ["clock", "details", "resourceid"],
                    "filter": {"resourcetype": 2, "action": 1, "resourceid": hostids},
                    "time_from": time_from,
                    "sortfield": "clock",
                    "sortorder": "ASC",
                    "limit": 5000,
                })

                rotations: list[dict] = []
                for r in records:
                    hid = str(r.get("resourceid", ""))
                    try:
                        clock = int(r.get("clock", 0))
                    except (ValueError, TypeError):
                        continue
                    for old_ip, new_ip in _parse_ip_changes(r.get("details", "")):
                        baseline = await _fetch_traffic_avg(
                            client, hid, clock - 86400, clock,
                        )
                        post = await _fetch_traffic_avg(
                            client, hid, clock, clock + 86400,
                        )
                        rotations.append({
                            "hostid": hid,
                            "clock": clock,
                            "old_ip": old_ip,
                            "new_ip": new_ip,
                            "score": _score_recovery(baseline, post),
                        })

                if not rotations:
                    return (
                        f"No IP rotations found in the last {window_days}d "
                        f"({len(hostids)} hosts inspected)."
                    )

                agg = _aggregate_recovery_scores(rotations)
                rate = f"{agg['rate_pct']:.1f}%" if agg["rate_pct"] is not None else "—"
                return (
                    f"**Recovery KPI** (last {window_days}d, {len(hostids)} hosts)\n\n"
                    f"- **Total rotations:** {agg['total']}\n"
                    f"- **Recovered:** {agg['recovered']}\n"
                    f"- **Partial:** {agg['partial']}\n"
                    f"- **Still-down:** {agg['still_down']}\n"
                    f"- **N/A (insufficient trend):** {agg['na']}\n"
                    f"- **Recovery rate:** {rate} "
                    f"(recovered / determined-outcome)"
                )
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"
