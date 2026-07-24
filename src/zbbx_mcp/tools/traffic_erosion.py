"""Multi-week traffic-erosion detection — the slow-decline blind spot (ADR 091).

``detect_traffic_drops`` (ADR 040) is *acute*: it compares a recent window
against a 7-day seasonal baseline and fires on a large same-hour drop. A
**gradual** multi-week decline never trips it — each day sits only slightly
below the trailing week, so the ratio never crosses the threshold, yet over a
couple of months a host can bleed most of its traffic. That is the slow failure
mode: gradual reachability loss or demand rot, a little every week.

This tool fits a slope to each host's **weekly-mean** throughput over a
multi-week window and flags sustained downslopes. The false positive it must
avoid is a scope-wide demand dip (seasonality, a holiday lull) dragging every
host down together — that is not a host-specific problem. So the verdict is
**cohort-relative**: a host is flagged as eroding only when it declines
materially faster than the median of its scope. A decline that merely tracks the
cohort is labelled *demand*, not erosion.

The pure functions (``weekly_means``, ``linreg_slope``, ``classify_erosion``)
are unit-tested in isolation; the async tool wires real trend data into them.
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass
from statistics import median

import httpx

from zbbx_mcp.anomaly import pick_traffic_interface
from zbbx_mcp.classify import classify_host as _classify_host
from zbbx_mcp.classify import detect_provider
from zbbx_mcp.data import (
    countries_for_region,
    excluded_test_note,
    extract_country,
    host_ip,
    partition_test_hosts,
)
from zbbx_mcp.fetch import TRAFFIC_DIVISOR
from zbbx_mcp.resolver import InstanceResolver

_IFACE_CANDIDATES = 3     # top-N interfaces per host by current value (bound trend volume)
_WEEK = 7 * 86400
_MAX_WEEKS = 12           # window cap — hourly trends over many weeks are heavy
_MIN_WEEKS = 4            # fewer weekly points than this can't support a slope
_MIN_COHORT = 3           # fewer non-idle peers than this = no meaningful cohort
                          # (a 1-host median is that host's own slope — degenerate),
                          # so fall back to absolute decline (cohort_slope_pct=None)

# Verdict vocabulary.
ERODING = "eroding"          # sustained decline, faster than the cohort
DEMAND = "demand"            # declining, but tracking the cohort (not host-specific)
RECOVERING = "recovering"    # sustained rise
STABLE = "stable"            # within tolerance
IDLE = "idle"                # peak below the floor — spare/out-of-rotation, not eroding
INSUFFICIENT = "insufficient"  # too few weekly points to judge


def weekly_means(
    points: list[tuple[int, float]], now: int, weeks: int
) -> list[tuple[int, float]]:
    """Bucket ``[(epoch, value), ...]`` into weekly means, oldest→newest.

    Week ``k`` (0-indexed from the oldest) covers
    ``[now-(weeks-k)*7d, now-(weeks-k-1)*7d)``. Only weeks with at least one
    sample are returned, so a gap in retention shrinks the series rather than
    injecting a zero (a false trough). Pure.
    """
    start = now - weeks * _WEEK
    buckets: dict[int, list[float]] = {}
    for clock, val in points:
        c = int(clock)
        if c < start or c >= now:
            continue
        k = (c - start) // _WEEK
        if 0 <= k < weeks:
            buckets.setdefault(int(k), []).append(float(val))
    return [(k, sum(v) / len(v)) for k, v in sorted(buckets.items())]


def linreg_slope(xs: list[float], ys: list[float]) -> float:
    """Least-squares slope of ``ys`` over ``xs``.

    Returns 0.0 for fewer than two points or when ``xs`` has no variance
    (division-by-zero guard). Pure.
    """
    n = len(xs)
    if n < 2:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        return 0.0
    return sum((xs[i] - mx) * (ys[i] - my) for i in range(n)) / denom


@dataclass
class ErosionVerdict:
    """Result of ``classify_erosion``. Rates are Mbps; percentages are 0-100."""

    state: str
    slope_pct: float           # %/week vs the mean level (negative = declining)
    cum_decline_pct: float     # first-third vs last-third (positive = declined)
    first: float
    last: float
    peak: float
    weeks: int
    relative_pct: float | None = None   # slope_pct - cohort_slope_pct
    reason: str = ""


def _third_means(vals: list[float]) -> tuple[float, float]:
    """Mean of the first third and the last third of ``vals``.

    A two-window compare that is robust to a single spiky week (unlike
    first-vs-last point). Pure.
    """
    n = len(vals)
    k = max(1, n // 3)
    return sum(vals[:k]) / k, sum(vals[-k:]) / k


def classify_erosion(
    weekly: list[tuple[int, float]],
    *,
    min_baseline: float,
    min_decline_pct: float,
    cohort_slope_pct: float | None = None,
    cohort_relative: bool = True,
    relative_margin_pct: float = 5.0,
    min_weeks: int = _MIN_WEEKS,
) -> ErosionVerdict:
    """Classify one host's weekly-mean throughput series.

    ``weekly`` is the oldest→newest ``[(week_index, mean_mbps), ...]`` from
    ``weekly_means``. Verdict priority:

    - **insufficient** — fewer than ``min_weeks`` weekly points to trust a slope.
    - **idle** — peak weekly mean below ``min_baseline``: a spare / out-of-
      rotation host whose "decline" would be noise on a near-zero denominator.
    - **recovering** — a material *rise* (guards against calling a rebound a drop).
    - **eroding** — declined at least ``min_decline_pct`` (first-third vs
      last-third) with a negative slope, AND (when ``cohort_relative``) doing so
      materially faster than ``cohort_slope_pct``.
    - **demand** — same decline, but only tracking the cohort — not host-specific.
    - **stable** — everything else.

    Pure.
    """
    vals = [v for _, v in weekly]
    weeks = len(vals)
    peak = max(vals) if vals else 0.0
    first = vals[0] if vals else 0.0
    last = vals[-1] if vals else 0.0

    if weeks < min_weeks:
        return ErosionVerdict(
            INSUFFICIENT, 0.0, 0.0, first, last, peak, weeks, None,
            f"only {weeks} weekly point(s) (< {min_weeks}) — cannot fit a trend",
        )
    if peak < min_baseline:
        return ErosionVerdict(
            IDLE, 0.0, 0.0, first, last, peak, weeks, None,
            f"peak weekly mean {peak:.2f} below {min_baseline:g} Mbps floor "
            "— idle/spare, not eroding",
        )

    xs = [float(k) for k, _ in weekly]
    slope = linreg_slope(xs, vals)           # Mbps per week
    base = sum(vals) / len(vals)
    slope_pct = (slope / base * 100.0) if base > 0 else 0.0
    early, late = _third_means(vals)
    cum_decline_pct = ((early - late) / early * 100.0) if early > 0 else 0.0
    relative_pct = (
        slope_pct - cohort_slope_pct if cohort_slope_pct is not None else None
    )

    # Material rise — a rebound, never a drop.
    if cum_decline_pct <= -min_decline_pct and slope_pct > 0:
        return ErosionVerdict(
            RECOVERING, slope_pct, cum_decline_pct, first, last, peak, weeks,
            relative_pct, f"up {-cum_decline_pct:.0f}% over {weeks}w",
        )

    # Material decline?
    if cum_decline_pct >= min_decline_pct and slope_pct < 0:
        faster_than_cohort = (
            cohort_slope_pct is None
            or slope_pct <= cohort_slope_pct - relative_margin_pct
        )
        if cohort_relative and not faster_than_cohort:
            return ErosionVerdict(
                DEMAND, slope_pct, cum_decline_pct, first, last, peak, weeks,
                relative_pct,
                f"down {cum_decline_pct:.0f}% over {weeks}w but tracks cohort "
                f"({cohort_slope_pct:+.1f}%/wk) — demand, not host-specific",
            )
        rel = "" if relative_pct is None else f", {relative_pct:+.1f}%/wk vs cohort"
        return ErosionVerdict(
            ERODING, slope_pct, cum_decline_pct, first, last, peak, weeks,
            relative_pct,
            f"down {cum_decline_pct:.0f}% over {weeks}w "
            f"({slope_pct:+.1f}%/wk{rel})",
        )

    return ErosionVerdict(
        STABLE, slope_pct, cum_decline_pct, first, last, peak, weeks,
        relative_pct, "within tolerance",
    )


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()) -> None:

    if "detect_traffic_erosion" not in skip:

        @mcp.tool()
        async def detect_traffic_erosion(
            group: str = "",
            product: str = "",
            country: str = "",
            region: str = "",
            weeks: int = 6,
            min_baseline_mbps: float = 1.0,
            min_decline_pct: float = 30.0,
            cohort_relative: bool = True,
            relative_margin_pct: float = 5.0,
            max_results: int = 25,
            include_test: bool = False,
            instance: str = "",
        ) -> str:
            """Detect hosts on a sustained multi-week traffic downslope.

            Fills the gap ``detect_traffic_drops`` leaves: that tool is acute
            (a sharp same-hour drop vs a 7-day seasonal baseline) and cannot see
            a slow multi-week erosion, where each day is only slightly below the
            last. Fits a slope to each host's weekly-mean throughput and, to
            avoid flagging a scope-wide demand dip, judges each host
            **cohort-relative** — eroding only if it declines materially faster
            than its scope's median. A decline that tracks the cohort is labelled
            *demand*, not erosion.

            Args:
                group: Zabbix host group (optional)
                product: Filter by product (optional)
                country: Country code filter (optional)
                region: LATAM, APAC, EMEA, NA, CIS, ALL (optional)
                weeks: Window length in weeks, capped at 12 (default: 6)
                min_baseline_mbps: Peak weekly mean below this = idle/spare,
                    not eroding — the denominator floor (default: 1.0)
                min_decline_pct: First-third vs last-third decline to flag
                    (default: 30)
                cohort_relative: Require the decline to beat the cohort median
                    slope before flagging as host-specific erosion (default: True)
                relative_margin_pct: How much faster than the cohort (%/week) a
                    host must decline to count as host-specific (default: 5)
                max_results: Max rows shown (default: 25)
                include_test: Keep test/staging hosts (default: False — a test
                    box in a production group skews the cohort median)
                instance: Zabbix instance (optional)
            """
            try:
                client = resolver.resolve(instance)
                wk = max(_MIN_WEEKS, min(int(weeks), _MAX_WEEKS))

                hosts = await client.call("host.get", {
                    "output": ["hostid", "host"],
                    "selectGroups": ["name"],
                    "selectInterfaces": ["ip"],
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

                # Traffic interface items + current value. Shortlist the top-N
                # per host by current value before the (heavy) trend fetch, the
                # same bound detect_traffic_drops uses: a host has one real
                # uplink plus many idle svc/tun interfaces, and pulling weeks of
                # hourly trends for all of them fleet-wide overruns the API.
                traffic_items = await client.call("item.get", {
                    "hostids": filtered_ids,
                    "output": ["itemid", "hostid", "key_", "lastvalue"],
                    "search": {"name": "Incoming network traffic"},
                    "filter": {"status": "0"},
                })
                if not traffic_items:
                    return "No traffic items found." + excluded_test_note(excluded)

                def _lv(it: dict) -> float:
                    try:
                        return float(it.get("lastvalue", "0") or 0)
                    except (ValueError, TypeError):
                        return 0.0

                by_host_items: dict[str, list[dict]] = {}
                for it in traffic_items:
                    by_host_items.setdefault(it["hostid"], []).append(it)
                shortlist: list[dict] = []
                for items in by_host_items.values():
                    items.sort(key=_lv, reverse=True)
                    shortlist.extend(items[:_IFACE_CANDIDATES])

                now = int(_time.time())
                time_from = now - wk * _WEEK
                all_item_ids = [i["itemid"] for i in shortlist]
                trends = await client.call("trend.get", {
                    "itemids": all_item_ids,
                    "time_from": time_from,
                    "output": ["itemid", "clock", "value_avg"],
                    "limit": len(all_item_ids) * wk * 7 * 24 + 1000,
                })
                item_points: dict[str, list[tuple[int, float]]] = {}
                for t in trends:
                    try:
                        item_points.setdefault(t["itemid"], []).append(
                            (int(t["clock"]), float(t["value_avg"]) / TRAFFIC_DIVISOR)
                        )
                    except (ValueError, TypeError, KeyError):
                        continue

                # Per host: pick the interface with the highest whole-window mean
                # (the real uplink), then bucket it into weekly means. Choosing by
                # baseline mean — not current value — keeps a momentarily-idle
                # uplink from losing to a spiking tunnel (same rule as the acute
                # detector's pick_traffic_interface).
                host_weekly: dict[str, list[tuple[int, float]]] = {}
                for hid, items in by_host_items.items():
                    ifaces = []
                    for it in items[:_IFACE_CANDIDATES]:
                        pts = item_points.get(it["itemid"], [])
                        mean = sum(v for _, v in pts) / len(pts) if pts else None
                        ifaces.append((it["itemid"], mean))
                    iid = pick_traffic_interface(ifaces)
                    if iid is None:
                        continue
                    wm = weekly_means(item_points.get(iid, []), now, wk)
                    if wm:
                        host_weekly[hid] = wm

                if not host_weekly:
                    return (
                        "No trend history in the window (check trend retention)."
                        + excluded_test_note(excluded)
                    )

                # Pass 1: per-host slope (cohort-blind) to form the cohort median.
                prelim = {
                    hid: classify_erosion(
                        wm, min_baseline=min_baseline_mbps,
                        min_decline_pct=min_decline_pct, cohort_slope_pct=None,
                        cohort_relative=cohort_relative,
                        relative_margin_pct=relative_margin_pct,
                    )
                    for hid, wm in host_weekly.items()
                }
                cohort_slopes = [
                    v.slope_pct for v in prelim.values()
                    if v.state not in (IDLE, INSUFFICIENT)
                ]
                # A cohort of one is its own median — degenerate. Require a
                # minimum peer count; below it, judge on absolute decline.
                cohort_slope_pct = (
                    median(cohort_slopes)
                    if len(cohort_slopes) >= _MIN_COHORT else None
                )

                # Pass 2: final verdicts against the cohort median.
                verdicts = {
                    hid: classify_erosion(
                        wm, min_baseline=min_baseline_mbps,
                        min_decline_pct=min_decline_pct,
                        cohort_slope_pct=cohort_slope_pct,
                        cohort_relative=cohort_relative,
                        relative_margin_pct=relative_margin_pct,
                    )
                    for hid, wm in host_weekly.items()
                }

                # Show the declining set (eroding first, then demand); count the rest.
                counts = {ERODING: 0, DEMAND: 0, RECOVERING: 0, STABLE: 0,
                          IDLE: 0, INSUFFICIENT: 0}
                for v in verdicts.values():
                    counts[v.state] = counts.get(v.state, 0) + 1

                declining = [
                    (hid, v) for hid, v in verdicts.items()
                    if v.state in (ERODING, DEMAND)
                ]
                # Eroding before demand; within each, steepest relative decline first.
                _order = {ERODING: 0, DEMAND: 1}
                declining.sort(key=lambda hv: (
                    _order[hv[1].state],
                    hv[1].relative_pct if hv[1].relative_pct is not None else hv[1].slope_pct,
                ))

                cohort_str = (
                    f"{cohort_slope_pct:+.1f}%/wk"
                    if cohort_slope_pct is not None
                    else "n/a (scope too small — absolute)"
                )
                header = (
                    f"**Traffic erosion** ({wk}w window, {len(host_weekly)} hosts; "
                    f"cohort median slope {cohort_str}; "
                    f"{counts[ERODING]} eroding, {counts[DEMAND]} demand-tracking, "
                    f"{counts[STABLE]} stable, {counts[IDLE]} idle)\n"
                )

                if not declining:
                    return (
                        header
                        + "\nNo host-specific erosion and no scope-wide decline "
                        "detected in the window."
                        + excluded_test_note(excluded)
                    )

                parts = [
                    header,
                    "| Server | Provider | Weeks | First → Last | Decline | "
                    "Slope/wk | vs cohort | Verdict |",
                    "|--------|----------|------:|--------------|--------:|"
                    "---------:|----------:|---------|",
                ]
                for hid, v in declining[:max_results]:
                    h = host_map.get(hid, {})
                    ip = host_ip(h) if h else ""
                    provider = detect_provider(ip) if ip else ""
                    rel = f"{v.relative_pct:+.1f}%" if v.relative_pct is not None else "–"
                    label = "ERODING" if v.state == ERODING else "demand"
                    parts.append(
                        f"| {h.get('host', hid)} | {provider} | {v.weeks} | "
                        f"{v.first:.1f} → {v.last:.1f} Mbps | "
                        f"**{v.cum_decline_pct:.0f}%** | {v.slope_pct:+.1f}% | "
                        f"{rel} | {label} |"
                    )
                if len(declining) > max_results:
                    parts.append(f"\n*{len(declining) - max_results} more omitted*")
                parts.append(
                    "\n_Erosion is cohort-relative: a host is flagged only when it "
                    "declines faster than the scope median. `demand` rows fell too, "
                    "but in step with the cohort — not host-specific._"
                )
                return "\n".join(parts) + excluded_test_note(excluded)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"
