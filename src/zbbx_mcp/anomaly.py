"""Traffic-drop classification — false-positive-resistant, acute-aware.

The naive "current vs N-day-average drop %" detector produces systematic
false positives on diurnal / bursty traffic: any spot reading caught in a
normal trough reads as an 80–96% drop. This module replaces that with a
layered classifier that distinguishes:

  - ``healthy``           recent traffic within the normal band
  - ``low_demand``        traffic down, but corroborating signals (CPU /
                          connections) fell with it → genuinely fewer users
  - ``blocked_acute``     traffic anomalously low *right now* (below the
                          same-hour-of-day seasonal band) with the host
                          still up — caught on the current bucket, no
                          persistence delay
  - ``blocked_sustained`` an acute block that has persisted across several
                          consecutive buckets
  - ``artifact``          baseline too small to judge (denominator rule) /
                          measurement noise
  - ``unknown``           insufficient data, or host appears down (a
                          different verdict that belongs to diagnose_host)

Design principles (in priority order):

  1. Compare like windows: recent-window *average* vs baseline-window
     *average* — never an instantaneous spot reading vs a distribution.
  2. Seasonality: judge against the same-hour-of-day band, so a normal
     nightly trough isn't mistaken for a drop AND a genuine drop is
     flagged immediately (below-band-now == anomalous-now).
  3. Persistence escalates, it does not gate: an anomaly is flagged
     acute on the first bucket; persistence only upgrades it to
     sustained. This is what lets immediate blocking be distinguished
     at the same time as sustained blocking.
  4. Corroboration: a block leaves the host *trying to serve* — CPU /
     connections hold up while bytes don't. Low demand drags CPU /
     connections down with the traffic. The divergence separates them.
  5. Denominator rule: a percentage is meaningless when the absolute
     numbers are tiny; sub-floor baselines never produce a drop verdict.

All functions here are pure (no Zabbix calls) so the logic is unit-tested
in isolation; the async tool wires real trend / item data into them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Verdict state vocabulary.
HEALTHY = "healthy"
LOW_DEMAND = "low_demand"
BLOCKED_ACUTE = "blocked_acute"
BLOCKED_SUSTAINED = "blocked_sustained"
ARTIFACT = "artifact"
UNKNOWN = "unknown"


@dataclass
class DropVerdict:
    """Result of classify_drop. ``state`` is the vocabulary above."""
    state: str
    confidence: int          # 0-100
    drop_pct: float          # recent vs baseline, 0-100 (0 when not a drop)
    reasons: list[str] = field(default_factory=list)


def percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile of ``values`` (0-100). None for empty input.

    Nearest-rank (not interpolated) — robust for the small samples we get
    from a 7-day hourly series bucketed by hour-of-day (~7 points/hour).
    """
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    pct = max(0.0, min(100.0, pct))
    # nearest-rank: rank = ceil(pct/100 * N), 1-indexed
    import math
    rank = max(1, math.ceil(pct / 100.0 * len(s)))
    return s[rank - 1]


def seasonal_floor(
    hourly_points: list[tuple[int, float]],
    target_hour: int,
    *,
    pct: float = 10.0,
    min_samples: int = 3,
) -> float | None:
    """Return the ``pct``-percentile traffic for ``target_hour`` (0-23 UTC).

    ``hourly_points`` is ``[(epoch_seconds, value), ...]`` — typically a
    7-day hourly trend series. Buckets by hour-of-day and returns the
    low-percentile of the matching bucket: the floor of *normal* traffic
    for that hour. A recent average at or above this floor is within the
    normal diurnal band (not a drop); below it is genuinely anomalous for
    this time of day.

    Returns None when there aren't enough same-hour samples to form a band
    (caller then falls back to the plain baseline-ratio path with reduced
    confidence).
    """
    bucket = [
        v for (clock, v) in hourly_points
        if (int(clock) // 3600) % 24 == target_hour
    ]
    if len(bucket) < min_samples:
        return None
    return percentile(bucket, pct)


def pick_traffic_interface(
    interfaces: list[tuple[str, float | None]],
) -> str | None:
    """Pick the interface that carries the host's real load.

    ``interfaces`` is ``[(item_id_or_name, baseline_avg), ...]``. Selects
    the highest *baseline* — NOT the highest current value — so a
    momentarily-spiking idle interface (or a dead tunnel reading near
    zero) is never chosen over the steady primary uplink. This is the
    fix for the "drop measured on an idle interface" false positive:
    an always-idle interface has ~0 baseline and is never selected, so
    its zero reading can't fabricate a drop.

    Returns None when no interface has a usable baseline.
    """
    candidates = [(name, b) for (name, b) in interfaces if b is not None]
    if not candidates:
        return None
    return max(candidates, key=lambda x: x[1])[0]


def aggregate_hourly_by_country(
    host_series: dict[str, list[tuple[int, float]]],
    host_country: dict[str, str],
) -> dict[str, list[tuple[int, float]]]:
    """Sum per-host hourly series into per-country hourly series.

    ``host_series`` maps hostid → ``[(epoch, value), ...]``;
    ``host_country`` maps hostid → country code. Values at the same epoch
    (hour bucket) are summed across a country's hosts, giving the
    country's aggregate hourly throughput — the series the acute regional
    detector classifies (ADR 051). Returns country → sorted
    ``[(epoch, summed), ...]``.

    Pure helper.
    """
    by_country: dict[str, dict[int, float]] = {}
    for hid, series in host_series.items():
        cc = host_country.get(hid)
        if not cc:
            continue
        acc = by_country.setdefault(cc, {})
        for clock, val in series:
            acc[clock] = acc.get(clock, 0.0) + float(val)
    return {cc: sorted(acc.items()) for cc, acc in by_country.items()}


def recent_baseline_from_daily(
    daily: dict,
    recent_days: int = 2,
) -> tuple[float | None, float | None]:
    """Split a date→value daily series into recent-avg vs baseline-avg.

    ``daily`` maps date strings (lexically sortable, e.g. ``"2026-06-04"``)
    to that day's average. Returns ``(recent_avg, baseline_avg)`` where
    recent is the mean of the last ``recent_days`` entries and baseline is
    the mean of the earlier ones. Returns ``(None, None)`` when there are
    not enough days to form both windows.

    Daily aggregates are inherently diurnal-safe — a full day's mean can't
    show a nightly trough — so this is the right grain for the per-country
    regional detector, which lacks the hourly series needed for a
    same-hour seasonal floor (ADR 047). Using the most-recent *day* also
    avoids the instantaneous-spot-reading false positive that the raw
    ``current`` value produced.
    """
    if not daily or len(daily) < recent_days + 1:
        return None, None
    ordered = [daily[k] for k in sorted(daily)]
    recent = ordered[-recent_days:]
    baseline = ordered[:-recent_days]
    if not recent or not baseline:
        return None, None
    try:
        r = sum(float(v) for v in recent) / len(recent)
        b = sum(float(v) for v in baseline) / len(baseline)
    except (ValueError, TypeError):
        return None, None
    return r, b


def metric_recent_baseline_ratio(
    records: list[tuple[int, float]],
    recent_start: int,
    *,
    invert_pct: bool = False,
) -> float | None:
    """Recent-window / baseline-window ratio of a metric's trend series.

    ``records`` is ``[(epoch_seconds, value), ...]``. Splits at
    ``recent_start`` (>= recent, < baseline), averages each window, and
    returns recent_avg / baseline_avg. Returns None when either window is
    empty or the baseline average is non-positive.

    ``invert_pct=True`` converts a percentage-idle metric to its
    "used" complement (``100 - x``) on both windows *before* the ratio —
    e.g. ``system.cpu.util[,idle]`` becomes CPU-used, so the ratio
    reflects load (a busy host has a high used-ratio), not idleness.
    Getting this inversion wrong would flip CPU corroboration, so it is
    pinned by tests.
    """
    rec = [v for (clock, v) in records if clock >= recent_start]
    base = [v for (clock, v) in records if clock < recent_start]
    if not rec or not base:
        return None
    r_avg = sum(rec) / len(rec)
    b_avg = sum(base) / len(base)
    if invert_pct:
        r_avg, b_avg = 100.0 - r_avg, 100.0 - b_avg
    if b_avg <= 0:
        return None
    return max(0.0, r_avg / b_avg)


def classify_drop(
    *,
    recent_avg: float | None,
    baseline_avg: float | None,
    seasonal_floor_value: float | None = None,
    min_baseline: float = 5.0,
    drop_pct_threshold: float = 50.0,
    sustained_buckets: int = 0,
    sustained_threshold: int = 3,
    agent_reachable: bool | None = None,
    cpu_ratio: float | None = None,
    conn_ratio: float | None = None,
) -> DropVerdict:
    """Classify a traffic drop, distinguishing acute / sustained blocking
    from diurnal troughs and genuine low demand.

    Units of ``recent_avg`` / ``baseline_avg`` / ``seasonal_floor_value`` /
    ``min_baseline`` must match (e.g. all Mbps).

    ``cpu_ratio`` / ``conn_ratio`` are recent/baseline ratios of CPU and
    connection count — corroboration signals. A value >= ~0.7 means the
    metric "held up" (block signature: host still working but no bytes);
    a value tracking the traffic ratio means demand fell (low-demand).
    Pass None when unavailable; the classifier degrades gracefully and
    caps confidence.

    ``sustained_buckets`` is the count of consecutive recent buckets that
    were anomalous. It does not gate detection — an anomaly is flagged
    acute immediately and only *escalated* to sustained at/above
    ``sustained_threshold``.
    """
    reasons: list[str] = []

    # --- Data sufficiency ---
    if recent_avg is None or baseline_avg is None:
        return DropVerdict(UNKNOWN, 0, 0.0, ["insufficient trend data"])

    # --- Denominator rule (P5): tiny baselines don't get an opinion ---
    if baseline_avg < min_baseline:
        return DropVerdict(
            ARTIFACT, 0, 0.0,
            [f"baseline {baseline_avg:.1f} below {min_baseline:.0f} floor "
             "— ratio not meaningful"],
        )

    ratio = recent_avg / baseline_avg if baseline_avg > 0 else 1.0
    drop_pct = max(0.0, (1.0 - ratio) * 100.0)

    # --- Not a significant drop ---
    if drop_pct < drop_pct_threshold:
        return DropVerdict(
            HEALTHY, 0, drop_pct,
            [f"recent {recent_avg:.1f} is {ratio * 100:.0f}% of baseline "
             f"{baseline_avg:.1f} — within tolerance"],
        )

    # Significant raw drop. Now distinguish real vs diurnal vs demand.

    # --- Seasonality (P2): is this normal for this hour-of-day? ---
    if seasonal_floor_value is not None:
        if recent_avg >= seasonal_floor_value:
            return DropVerdict(
                HEALTHY, 0, drop_pct,
                [f"recent {recent_avg:.1f} >= seasonal floor "
                 f"{seasonal_floor_value:.1f} for this hour — diurnal trough, "
                 "not a drop"],
            )
        reasons.append(
            f"recent {recent_avg:.1f} below seasonal floor "
            f"{seasonal_floor_value:.1f} for this hour — anomalous now"
        )
    else:
        reasons.append("no seasonal baseline — confidence limited")

    # --- Host-down rules out a *traffic* block (P5) ---
    if agent_reachable is False:
        return DropVerdict(
            UNKNOWN, 0, drop_pct,
            [*reasons, "agent unreachable — host-down, not a traffic block "
             "(see diagnose_host)"],
        )

    # --- Corroboration: block vs low-demand (P5) ---
    # A block leaves the host trying to serve: CPU / connections hold up
    # while bytes collapse. Low demand drags them down with the traffic.
    demand_ratio = None
    demand_src = ""
    if conn_ratio is not None:
        demand_ratio, demand_src = conn_ratio, "connections"
    elif cpu_ratio is not None:
        demand_ratio, demand_src = cpu_ratio, "cpu"

    corroborated_block = False
    if demand_ratio is not None:
        # If the demand signal fell roughly in step with traffic (allowing
        # 1.5x slack), users left — low demand, not a block.
        if demand_ratio <= ratio * 1.5:
            return DropVerdict(
                LOW_DEMAND,
                40,
                drop_pct,
                [*reasons,
                 f"{demand_src} fell with traffic "
                 f"({demand_src} ratio {demand_ratio:.2f} ~ traffic ratio "
                 f"{ratio:.2f}) — fewer users, not a block"],
            )
        # Demand signal held up while bytes collapsed → block signature.
        corroborated_block = True
        reasons.append(
            f"{demand_src} held up ({demand_src} ratio {demand_ratio:.2f}) "
            f"while traffic collapsed — host serving but no bytes flow"
        )

    # --- Confidence scoring ---
    confidence = 50
    if seasonal_floor_value is not None and seasonal_floor_value > 0:
        # How far below the seasonal floor: deeper = more confident.
        margin = max(0.0, 1.0 - recent_avg / seasonal_floor_value)
        confidence += int(round(margin * 25))
    else:
        confidence = min(confidence, 55)  # no seasonal context — cap
    if corroborated_block:
        confidence += 20
    if sustained_buckets >= sustained_threshold:
        confidence += 15
    confidence = max(0, min(95, confidence))

    # --- Acute vs sustained (the same-time distinction) ---
    if sustained_buckets >= sustained_threshold:
        reasons.append(
            f"anomalous for {sustained_buckets} consecutive buckets — sustained"
        )
        return DropVerdict(BLOCKED_SUSTAINED, confidence, drop_pct, reasons)

    reasons.append(
        f"anomalous on the current bucket ({sustained_buckets} consecutive) "
        "— immediate"
    )
    return DropVerdict(BLOCKED_ACUTE, confidence, drop_pct, reasons)


__all__ = [
    "HEALTHY", "LOW_DEMAND", "BLOCKED_ACUTE", "BLOCKED_SUSTAINED",
    "ARTIFACT", "UNKNOWN",
    "DropVerdict", "percentile", "seasonal_floor",
    "pick_traffic_interface", "metric_recent_baseline_ratio",
    "recent_baseline_from_daily", "aggregate_hourly_by_country", "classify_drop",
]
