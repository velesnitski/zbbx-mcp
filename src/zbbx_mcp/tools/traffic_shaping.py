"""Rate-limit / traffic-shaping detection — the flat-ceiling signature (ADR 104).

``detect_traffic_drops`` (ADR 040) sees a step DOWN. ``detect_traffic_erosion``
(ADR 091) sees a slow slide. Neither can separate a host that was **rate
limited** from one that simply lost demand, because in an average the two look
identical: less traffic than before. Acting on that ambiguity costs real time —
"shaped" means open a provider ticket, "demand" means do nothing.

Shaping has its own fingerprint, and it is not in the average — it is in the
**shape of the peaks**. A policer truncates the top of the distribution: every
hour that wants more than the cap reports exactly the cap, so the busy hours
pile up on one value. Ordinary demand touches its maximum once or twice and
spreads out below it, and keeps swinging with the diurnal curve even while it
falls. So the question to ask an hourly series is not "is it lower" but "did
the peaks pile up against a wall".

That is why every measurement here reads ``value_max`` and never ``value_avg``.
A steady load has a stable average and still has moving peaks; only a cap
flattens the peaks themselves, and averaging erases the one signal that
separates the two.

What it cannot do, both stated rather than hidden:

- Throughput alone cannot distinguish a hard cap from genuinely constant
  demand. Both report as ``capped``, which is why that verdict is worded as an
  observation rather than a diagnosis.
- A **ratcheting** cap — one stepped down more than once inside the recent
  window — reads ``normal`` until it settles, because no single value holds the
  point mass yet. That is roughly one run of latency and it resolves itself;
  splitting the recent window in half and comparing halves would close it, and
  is worth doing only if it bites in practice (ADR 108).

The pure core (``percentile``, ``ceiling_hit_rate``, ``classify_shaping``) is
unit tested; the async tool wires real trend data into it.
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass

import httpx

from zbbx_mcp.classify import classify_host as _classify_host
from zbbx_mcp.data import (
    AUDIT_ACTION_ADD,
    AUDIT_RESOURCE_HOST,
    countries_for_region,
    excluded_test_note,
    extract_country,
    partition_test_hosts,
)
from zbbx_mcp.fetch import TRAFFIC_DIVISOR, physical_traffic_items
from zbbx_mcp.resolver import InstanceResolver

_IFACE_CANDIDATES = 3      # top-N interfaces per host — bound the trend fetch
_MAX_HOURS = 24 * 21       # window cap: hourly trends fleet-wide are heavy
_MIN_RECENT_H = 12         # fewer recent hours than this can't show a ceiling
_MIN_ACTIVE_H = 6          # a ceiling seen in <6 active hours is not evidence
_ACTIVE_FRAC = 0.5         # an hour counts as "pushing" at >=50% of the ceiling
_HIT_TOLERANCE = 0.02      # within 2% of the ceiling counts as touching it

# Verdict vocabulary.
SHAPED = "shaped"            # dropped AND the peaks piled up at a ceiling
CAPPED = "capped"            # peaks at a ceiling, but no drop — pre-existing
DROPPED = "dropped"          # lower peaks that still spread — demand or a block
NORMAL = "normal"            # peaks spread, no material drop
NO_BASELINE = "no_baseline"  # nothing to compare against — cannot say
IDLE = "idle"                # ceiling below the floor — spare/out of rotation
INSUFFICIENT = "insufficient"  # too few hours to judge


@dataclass
class ShapingVerdict:
    verdict: str
    ceiling_mbps: float
    hit_rate: float
    hits: int
    active_hours: int
    base_ceiling_mbps: float | None
    drop_pct: float
    note: str


def percentile(values: list[float], q: float) -> float:
    """Nearest-rank percentile of ``values``. Pure; 0.0 for an empty list.

    Used instead of ``max`` so a single freak minute cannot define the ceiling.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    k = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[k]


def ceiling_hit_rate(
    peaks: list[float],
    *,
    tolerance: float = _HIT_TOLERANCE,
    active_frac: float = _ACTIVE_FRAC,
) -> tuple[float, float, int, int]:
    """``(hit_rate, ceiling, hits, active_hours)`` for one hourly peak series.

    The ceiling is the **modal point mass** — the value where the most active
    hours cluster — and the hit rate is that cluster's share. Clipping puts
    many hours on one value; ordinary demand reaches its top once and spreads
    below it.

    The ceiling is NOT a band around p95, which is the obvious construction and
    fails on the policer shape that matters most: a token bucket permits an
    occasional burst above its cap, the burst becomes p95, and the real cap then
    sits outside the band — hit rate collapses to ~7% and a hard cap reports as
    "demand or reachability, not a cap". Found by adversarial validation, not by
    the original tests (ADR 108). p95 still sets the *active* threshold, where
    an outlier is harmless.

    Scanning every observed value as a candidate is exact and costs nothing at
    these sizes (a few hundred hours), so there is no binning artefact to tune.
    Ties prefer the higher value, so a cap reads as the cap rather than as some
    trough beneath it.

    Rejected earlier, recorded because it looks right and is not: measuring the
    SPREAD of the top quartile. Selecting the largest values compresses any
    distribution, so a perfectly healthy series scores as flat as a shaped one.
    The test has to be how MANY hours reach the ceiling, not how tightly the
    highest few agree.

    Only hours at or above ``active_frac`` of the p95 top count, so a quiet
    night — which tops out far below any cap — neither dilutes the rate nor
    fakes one. Pure.
    """
    if not peaks:
        return 0.0, 0.0, 0, 0
    top = percentile(peaks, 0.95)
    if top <= 0:
        return 0.0, 0.0, 0, 0
    active = [p for p in peaks if p >= top * active_frac]
    if not active:
        return 0.0, top, 0, 0
    best_ceiling, best_hits = top, 0
    for cand in sorted(set(active)):
        n = sum(1 for p in active if abs(p - cand) <= cand * tolerance)
        if n > best_hits or (n == best_hits and cand > best_ceiling):
            best_ceiling, best_hits = cand, n
    return best_hits / len(active), best_ceiling, best_hits, len(active)


def classify_shaping(
    recent_peaks: list[float],
    base_peaks: list[float],
    *,
    min_ceiling_mbps: float = 1.0,
    min_drop_pct: float = 25.0,
    min_hit_rate: float = 0.6,
    min_recent_hours: int = _MIN_RECENT_H,
) -> ShapingVerdict:
    """Classify one interface's hourly PEAK series against its own past.

    Both arguments are per-hour ``value_max`` in Mbps — the recent window
    first, the preceding baseline second. Verdict priority:

    - **insufficient** — too few recent hours, or too few active hours inside
      them, to call a ceiling. A wall seen in a handful of samples is an
      artefact, so this is checked before anything else.
    - **idle** — ceiling under the floor: a spare host whose peaks are pinned
      at nothing.
    - **shaped** — the ceiling dropped materially AND the peaks piled up on it.
      Both halves are required: a drop alone is demand or a block, a ceiling
      alone is a limit that was always there.
    - **capped** — peaks on a ceiling, no drop. Either a pre-existing rate
      limit or genuinely constant demand; throughput cannot tell those apart.
    - **dropped** — a material drop whose peaks still spread. The
      ``detect_traffic_drops`` story, restated so the two tools agree.
    - **no_baseline** — the recent window is fine, but there is nothing before
      it to compare against, so no statement about a fall is possible.
    - **normal** — otherwise, and only ever against a real baseline.

    The split between the last two is the point. `normal` is a COMPARATIVE
    claim; making it without a baseline asserts health from no evidence, which
    is how a host whose trend history was destroyed reads as healthy. `capped`
    survives a missing baseline because it is a statement about the recent
    window alone — a ceiling is visible without knowing what came before, so
    such a host reads *capped*, never *shaped*.

    Pure.
    """
    if len(recent_peaks) < min_recent_hours:
        return ShapingVerdict(
            INSUFFICIENT, 0.0, 0.0, 0, 0, None, 0.0,
            f"only {len(recent_peaks)} recent hour(s) (< {min_recent_hours}) "
            "— too few to tell a ceiling from a coincidence",
        )

    hit_rate, ceiling, hits, active = ceiling_hit_rate(recent_peaks)
    if active < _MIN_ACTIVE_H:
        return ShapingVerdict(
            INSUFFICIENT, ceiling, 0.0, hits, active, None, 0.0,
            f"only {active} active hour(s) (< {_MIN_ACTIVE_H}) — a ceiling over "
            "this few samples is an artefact, not a cap",
        )
    if ceiling < min_ceiling_mbps:
        return ShapingVerdict(
            IDLE, ceiling, hit_rate, hits, active, None, 0.0,
            f"ceiling {ceiling:.2f} Mbps below the {min_ceiling_mbps:g} Mbps "
            "floor — idle/spare, nothing to shape",
        )

    _, base_ceiling, _, base_active = ceiling_hit_rate(base_peaks)
    if base_active < _MIN_ACTIVE_H or base_ceiling <= 0:
        base_ceiling = None
    drop_pct = (
        (base_ceiling - ceiling) / base_ceiling * 100.0 if base_ceiling else 0.0
    )

    walled = hit_rate >= min_hit_rate
    have_baseline = base_ceiling is not None
    dropped = have_baseline and drop_pct >= min_drop_pct

    if walled and dropped:
        return ShapingVerdict(
            SHAPED, ceiling, hit_rate, hits, active, base_ceiling, drop_pct,
            f"peaks pinned at {ceiling:.1f} Mbps ({hits}/{active} active hours "
            f"within {_HIT_TOLERANCE:.0%} of it) after falling {drop_pct:.0f}% "
            f"from {base_ceiling:.1f} Mbps — a cap, not demand",
        )
    if walled:
        return ShapingVerdict(
            CAPPED, ceiling, hit_rate, hits, active, base_ceiling, drop_pct,
            f"peaks pinned at {ceiling:.1f} Mbps ({hits}/{active} active hours) "
            "with no material drop — a pre-existing limit, or constant demand",
        )
    if dropped:
        return ShapingVerdict(
            DROPPED, ceiling, hit_rate, hits, active, base_ceiling, drop_pct,
            f"peaks fell {drop_pct:.0f}% to {ceiling:.1f} Mbps but still spread "
            f"({hit_rate:.0%} of active hours at the top) — demand or "
            "reachability, not a cap",
        )
    if not have_baseline:
        # Everything above this point is a recent-window observation and stands
        # on its own. "normal" is not: it means "compared with before, nothing
        # changed", and there is no before. Recreating a host's items destroys
        # its trend history, so this is exactly where a rebuilt host lands —
        # absent evidence, which must never render as evidence of absence
        # (ADR 107).
        return ShapingVerdict(
            NO_BASELINE, ceiling, hit_rate, hits, active, None, 0.0,
            f"ceiling {ceiling:.1f} Mbps, but no history before the recent "
            "window to compare against — a fall here would be invisible",
        )
    return ShapingVerdict(
        NORMAL, ceiling, hit_rate, hits, active, base_ceiling, drop_pct,
        f"peaks spread normally ({hit_rate:.0%} of active hours at the top)",
    )


async def host_added_hours(client, hostids, now: int) -> dict[str, int]:
    """``{hostid: hours since the host was ADDED}`` from the audit log.

    A host missing from the result has no Add record inside audit retention,
    which means *cannot tell* — never *not new*. Callers must keep that third
    state rather than collapsing it into either answer (ADR 111).

    Best-effort: a failure returns ``{}`` and the caller degrades to "unknown",
    because this only ever enriches a disclosure and must not break it.
    """
    ids = [h for h in hostids if h]
    if not ids:
        return {}
    try:
        rows = await client.call("auditlog.get", {
            "output": ["resourceid", "clock"],
            "filter": {
                "resourcetype": AUDIT_RESOURCE_HOST,
                "action": AUDIT_ACTION_ADD,
                "resourceid": ids,
            },
        })
    except (httpx.HTTPError, ValueError, KeyError):
        return {}
    out: dict[str, int] = {}
    for r in rows or []:
        rid = str(r.get("resourceid", "") or "")
        try:
            clock = int(r["clock"])
        except (KeyError, TypeError, ValueError):
            continue
        age = max(0, (now - clock) // 3600)
        if rid and (rid not in out or age < out[rid]):
            out[rid] = age
    return out


def explain_unjudged(history_h: int, added_h: int | None, slack_h: int = 6) -> str:
    """Why a host has so little history: new, rebuilt, or unknown. Pure.

    The point of ADR 111. The disclosure used to state item recreation as the
    cause, which was wrong the first time it was read in anger — the hosts had
    simply been provisioned two days earlier. Both readings fit the same
    observation, and the audit log settles it, so the tool should answer rather
    than hedge.
    """
    if added_h is None:
        return f"{history_h}h of history, age unknown (no Add record in audit retention)"
    if added_h <= history_h + slack_h:
        return f"{history_h}h of history — host added {added_h}h ago, so it is simply new"
    return (f"{history_h}h of history but host added {added_h}h ago "
            "— its items were rebuilt and the earlier history is gone")


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()) -> None:

    if "detect_traffic_shaping" not in skip:

        @mcp.tool()
        async def detect_traffic_shaping(
            group: str = "",
            product: str = "",
            country: str = "",
            region: str = "",
            hours: int = 48,
            baseline_days: int = 7,
            min_ceiling_mbps: float = 1.0,
            min_drop_pct: float = 25.0,
            min_hit_rate: float = 0.6,
            include_capped: bool = True,
            max_results: int = 25,
            include_test: bool = False,
            instance: str = "",
        ) -> str:
            """Detect hosts whose throughput is pinned against a ceiling — rate
            limiting / traffic shaping, as distinct from lost demand.

            ``detect_traffic_drops`` and ``detect_traffic_erosion`` both answer
            "is it lower". Neither answers "why", and a shaped host and a
            quiet one look identical in an average. This one reads hourly
            **peaks**: a policer truncates the top of the distribution, so the
            active hours pile up on a single value, while real demand reaches
            its top once and spreads out below it. `shaped` means open a
            provider ticket; `dropped` means it is demand or reachability.

            Verdicts: **shaped** (ceiling fell AND peaks pile on it),
            **capped** (peaks on a ceiling, no drop — a pre-existing limit, or
            constant demand: throughput cannot separate those), **dropped**
            (lower peaks that still spread), **normal**, **idle**,
            **insufficient**.

            Args:
                group: Zabbix host group (optional)
                product: Filter by product (optional)
                country: Country code filter (optional)
                region: LATAM, APAC, EMEA, NA, CIS, ALL (optional)
                hours: Recent window in hours (default: 48)
                baseline_days: Days of history before the recent window to
                    compare against (default: 7)
                min_ceiling_mbps: Ceiling below this = idle/spare (default: 1.0)
                min_drop_pct: Ceiling fall vs baseline to count as a drop
                    (default: 25)
                min_hit_rate: Share of active hours that must sit within 2% of
                    the ceiling before peaks count as pinned (default: 0.6)
                include_capped: Also list pre-existing caps, not just new ones
                    (default: True)
                max_results: Max rows shown (default: 25)
                include_test: Keep test/staging hosts (default: False)
                instance: Zabbix instance (optional)
            """
            try:
                client = resolver.resolve(instance)
                rec_h = max(_MIN_RECENT_H, int(hours))
                total_h = min(_MAX_HOURS, rec_h + max(1, int(baseline_days)) * 24)

                hosts = await client.call("host.get", {
                    "output": ["hostid", "host"],
                    "selectGroups": ["name"],
                    "filter": {"status": "0"},
                })
                excluded: list[dict] = []
                if not include_test:
                    hosts, excluded = partition_test_hosts(hosts)
                host_map = {h["hostid"]: h for h in hosts}

                filtered_ids = []
                for h in hosts:
                    prod, _ = _classify_host(h.get("groups", []))
                    if product and product.lower() not in (prod or "").lower():
                        continue
                    if group and not any(
                        g["name"].lower() == group.lower()
                        for g in h.get("groups", [])
                    ):
                        continue
                    if country and extract_country(h.get("host", "")).lower() != country.lower():
                        continue
                    if region:
                        rc = countries_for_region(region)
                        if rc and extract_country(h.get("host", "")).upper() not in rc:
                            continue
                    filtered_ids.append(h["hostid"])

                if not filtered_ids:
                    return "No servers match the filter." + excluded_test_note(excluded)

                # Discover by item KEY, never by item NAME. Zabbix's stock
                # "Linux by Zabbix agent" template calls this metric
                # "Interface enp3s0: Bits received" while the in-house template
                # calls it "Incoming network traffic" — a name search examines
                # only one of the two fleets and reports silence for the other
                # (ADR 105).
                traffic_items = await physical_traffic_items(client, filtered_ids)
                if not traffic_items:
                    return (
                        "No physical-NIC traffic items found for the scope."
                        + excluded_test_note(excluded)
                    )

                def _lv(it: dict) -> float:
                    try:
                        return float(it.get("lastvalue", "0") or 0)
                    except (ValueError, TypeError):
                        return 0.0

                by_host_items: dict[str, list[dict]] = {}
                for it in traffic_items:
                    by_host_items.setdefault(it["hostid"], []).append(it)
                shortlist: list[str] = []
                for items in by_host_items.values():
                    items.sort(key=_lv, reverse=True)
                    shortlist.extend(i["itemid"] for i in items[:_IFACE_CANDIDATES])

                now = int(_time.time())
                # value_MAX, never value_avg: averaging erases the clipping this
                # tool exists to find (a steady load has a stable average and
                # still-moving peaks; only a cap flattens the peaks).
                trends = await client.call("trend.get", {
                    "itemids": shortlist,
                    "time_from": now - total_h * 3600,
                    "output": ["itemid", "clock", "value_max"],
                    "limit": len(shortlist) * total_h + 1000,
                })
                cut = now - rec_h * 3600
                recent: dict[str, list[float]] = {}
                base: dict[str, list[float]] = {}
                oldest: dict[str, int] = {}   # first sample per item — how far
                                              # back this item's history reaches
                for t in trends:
                    try:
                        clock = int(t["clock"])
                        val = float(t["value_max"]) / TRAFFIC_DIVISOR
                    except (ValueError, TypeError, KeyError):
                        continue
                    (recent if clock >= cut else base).setdefault(
                        t["itemid"], []).append(val)
                    iid = t["itemid"]
                    if clock < oldest.get(iid, clock + 1):
                        oldest[iid] = clock

                # One interface per host: the one with the highest baseline
                # ceiling, so a spiking idle tunnel can never stand in for the
                # real uplink (same rule as the acute detector).
                verdicts: dict[str, ShapingVerdict] = {}
                history_h: dict[str, int] = {}
                for hid, items in by_host_items.items():
                    best_iid, best_ceiling = None, -1.0
                    for it in items[:_IFACE_CANDIDATES]:
                        iid = it["itemid"]
                        series = base.get(iid) or recent.get(iid) or []
                        c = percentile(series, 0.95)
                        if c > best_ceiling:
                            best_iid, best_ceiling = iid, c
                    if best_iid is None:
                        continue
                    history_h[hid] = (
                        (now - oldest[best_iid]) // 3600 if best_iid in oldest else 0
                    )
                    verdicts[hid] = classify_shaping(
                        recent.get(best_iid, []), base.get(best_iid, []),
                        min_ceiling_mbps=min_ceiling_mbps,
                        min_drop_pct=min_drop_pct, min_hit_rate=min_hit_rate,
                    )

                if not verdicts:
                    return (
                        "No trend history in the window (check trend retention)."
                        + excluded_test_note(excluded)
                    )

                # A host that could not be examined must say so. Absent from
                # the table has to mean "unmeasured", never "healthy" — the
                # whole reason this bug survived was that it looked like a
                # clean result (ADR 105).
                unexamined = sorted(
                    host_map.get(hid, {}).get("host", hid)
                    for hid in filtered_ids if hid not in verdicts
                )
                unexamined_note = (
                    f"\n\n_{len(unexamined)} host(s) in scope had no usable "
                    f"physical-NIC trend data and were NOT examined: "
                    f"{', '.join(unexamined[:5])}"
                    f"{'…' if len(unexamined) > 5 else ''}. Absent from this table "
                    "means unmeasured, not healthy._" if unexamined else ""
                )

                # Hosts the tool looked at and could NOT judge. Counting them
                # in the header is not enough — "2 insufficient" is exactly the
                # line a reader skips, and a host whose trend history was just
                # destroyed lands here looking identical to a healthy one. Name
                # them, with how far back their history actually reaches
                # (ADR 107).
                unjudged_ids = [hid for hid, v in verdicts.items()
                                if v.verdict in (INSUFFICIENT, NO_BASELINE)]
                # One extra call for at most a handful of names, and it turns a
                # hedge into an answer: a short history means the items were
                # rebuilt OR the host is new, and the audit log knows which
                # (ADR 111). The previous wording asserted "recreated" and was
                # wrong the first time anyone read it in anger.
                added = await host_added_hours(client, unjudged_ids, now)
                unjudged = sorted(
                    (host_map.get(hid, {}).get("host", hid),
                     explain_unjudged(history_h.get(hid, 0), added.get(hid)))
                    for hid in unjudged_ids
                )
                unjudged_note = (
                    f"\n\n_{len(unjudged)} host(s) could NOT be judged — "
                    + "; ".join(f"{n}: {why}" for n, why in unjudged[:5])
                    + ("…" if len(unjudged) > 5 else "")
                    + ". Absent from the table above means unmeasured, not healthy._"
                    if unjudged else ""
                )

                counts: dict[str, int] = {}
                for v in verdicts.values():
                    counts[v.verdict] = counts.get(v.verdict, 0) + 1

                wanted = {SHAPED, CAPPED} if include_capped else {SHAPED}
                flagged = [(hid, v) for hid, v in verdicts.items()
                           if v.verdict in wanted]
                _order = {SHAPED: 0, CAPPED: 1}
                flagged.sort(key=lambda hv: (_order[hv[1].verdict],
                                             -hv[1].drop_pct))

                header = (
                    f"**Traffic shaping** ({rec_h}h vs {total_h - rec_h}h baseline, "
                    f"{len(verdicts)} hosts; {counts.get(SHAPED, 0)} shaped, "
                    f"{counts.get(CAPPED, 0)} capped, {counts.get(DROPPED, 0)} dropped, "
                    f"{counts.get(NORMAL, 0)} normal, {counts.get(IDLE, 0)} idle, "
                    f"{counts.get(NO_BASELINE, 0)} no-baseline, "
                    f"{counts.get(INSUFFICIENT, 0)} insufficient)\n"
                )
                if not flagged:
                    return (
                        header
                        + "\nNo host is pinned against a throughput ceiling in "
                        "the window."
                        + unjudged_note
                        + unexamined_note
                        + excluded_test_note(excluded)
                    )

                parts = [
                    header,
                    "| Server | Country | Ceiling | Was | Drop | At ceiling | Verdict |",
                    "|--------|---------|--------:|----:|-----:|-----------:|---------|",
                ]
                for hid, v in flagged[:max_results]:
                    h = host_map.get(hid, {})
                    was = f"{v.base_ceiling_mbps:.1f}" if v.base_ceiling_mbps else "–"
                    drop = f"**{v.drop_pct:.0f}%**" if v.verdict == SHAPED else "–"
                    parts.append(
                        f"| {h.get('host', hid)} | {extract_country(h.get('host', ''))} | "
                        f"{v.ceiling_mbps:.1f} Mbps | {was} | {drop} | "
                        f"{v.hits}/{v.active_hours} ({v.hit_rate:.0%}) | "
                        f"{'SHAPED' if v.verdict == SHAPED else 'capped'} |"
                    )
                if len(flagged) > max_results:
                    parts.append(f"\n*{len(flagged) - max_results} more omitted*")
                parts.append(
                    "\n_`SHAPED` = the ceiling fell AND the peaks pile up on it: a "
                    "cap that was applied. `capped` = peaks on a ceiling with no "
                    "drop — a pre-existing limit, or genuinely constant demand, "
                    "which throughput alone cannot separate. Hosts that merely "
                    "lost traffic are counted as `dropped` above and belong to "
                    "`detect_traffic_drops`._"
                )
                return ("\n".join(parts) + unjudged_note + unexamined_note
                        + excluded_test_note(excluded))
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"
