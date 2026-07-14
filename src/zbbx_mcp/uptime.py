"""Time-honest uptime math and trend-retention coverage (ADR 075).

Zabbix trends only exist for hours the item actually reported. Counting
"observed trend rows" as the denominator therefore inflates uptime: a
host that wrote one sample then died reads 100%, and a chronically dead
host (no rows at all) vanishes from the report entirely — the worst
offenders become invisible (tasks 168/169).

And Zabbix trend housekeeping retains only ~14 days on this fleet, so a
"30d" window silently returns 14d of data — and a month-over-month
comparison pits the live period against an empty void (task 170).

Both are pure, unit-testable, and shared across the uptime/SLA/trend
tools.
"""

_HOUR = 3600

__all__ = [
    "compute_host_uptime", "coverage_note", "retention_too_short",
    "traffic_hours_from_trends",
]


def traffic_hours_from_trends(traffic_rows, divisor, min_mbps=1.0):
    """Hour buckets in which the host moved real traffic.

    ``traffic_rows``: ``[(clock, value_avg), ...]`` hourly trends from the
    host's physical NICs (rows from several items may be mixed; an hour
    counts if ANY row clears the bar, so an idle NIC can never mask a busy
    carrier). ``divisor`` converts raw value_avg to Mbps (pass
    ``fetch._TRAFFIC_DIVISOR`` so the bytes-vs-bits convention stays in one
    place). Pure; feeds ``compute_host_uptime(host_has_traffic=...)``.
    """
    hours = set()
    for clock, avg in traffic_rows:
        try:
            if float(avg) / divisor >= min_mbps:
                hours.add(int(clock) // _HOUR)
        except (ValueError, TypeError):
            continue
    return hours


def compute_host_uptime(
    service_rows,
    now,
    window_start,
    host_has_traffic,
    up_threshold=0.5,
):
    """Return (up_hours, total_hours) for one host from hourly service trends.

    ``service_rows``: ``[(clock, value_avg), ...]`` hourly service-check
    trends (a service check is 1=up / 0=down; ``value_avg >= up_threshold``
    means the hour was mostly up). ``host_has_traffic``: the traffic gate for
    the deprecated-check false-down guard (task 169). Preferred form: a
    **set of hour buckets** (``clock // 3600``) in which the host moved real
    traffic (see ``traffic_hours_from_trends``) — a missing check-hour is
    then rescued only if THAT hour had traffic. Legacy form: a bool meaning
    "had traffic at some point in the window" — kept for callers without
    per-hour data, but it rescues every silent hour, so a host that served
    for a week and then hard-died reads ~100% instead of ~50% (task 172).

    Denominator spans every hour from the host's **first observed sample**
    (clamped to ``window_start``) through ``now`` — a missing hour is not
    free. Each hour resolves as:

    - explicit sample present  → trust it (up iff avg >= threshold);
    - missing hour, traffic that hour (or legacy bool True) → UP (host was
      alive, the check just wasn't recording — kills the false-down class);
    - missing hour, no traffic  → DOWN (a hard-down host writes no trends,
      task 168).

    Returns ``(0, 0)`` when the host has no samples at all in the window
    (uptime unmeasurable — the caller treats it as no-data, never a
    false 100% and never a false 0%).
    """
    now_h = now // _HOUR
    start_h = window_start // _HOUR
    sample: dict[int, bool] = {}
    for clock, avg in service_rows:
        try:
            h = int(clock) // _HOUR
        except (ValueError, TypeError):
            continue
        if h < start_h or h > now_h:
            continue
        try:
            sample[h] = float(avg) >= up_threshold
        except (ValueError, TypeError):
            continue

    if not sample:
        return (0, 0)

    per_hour_gate = isinstance(host_has_traffic, (set, frozenset))
    first_h = min(sample)
    total = now_h - first_h + 1
    up = 0
    for h in range(first_h, now_h + 1):
        if h in sample:
            up += 1 if sample[h] else 0
        elif (h in host_has_traffic) if per_hour_gate else host_has_traffic:
            up += 1  # alive per traffic that hour, check silent
        # else: missing + no traffic → down (counted in denominator only)
    return (up, total)


def coverage_note(min_clock_seen, now, requested_seconds):
    """One-line warning when observed data covers less than requested.

    ``min_clock_seen`` is the earliest trend timestamp actually returned
    (0/None when nothing came back). Returns "" when coverage is within
    ~5% of the request (retention is adequate), else a note stating the
    effective vs requested days so the reader never mistakes a
    retention-clipped window for the real period.
    """
    if not min_clock_seen or requested_seconds <= 0:
        return ""
    covered = max(0, now - int(min_clock_seen))
    if covered >= requested_seconds * 0.95:
        return ""
    cov_d = covered / 86400
    req_d = requested_seconds / 86400
    return (
        f"\n\n⚠ Trend retention: covered ~{cov_d:.1f}d of the requested "
        f"{req_d:.0f}d (Zabbix housekeeping keeps limited trend history) — "
        "figures reflect the shorter window."
    )


def retention_too_short(min_clock_seen, now, requested_seconds):
    """True when observed history can't fill BOTH comparison periods.

    A month-over-month compares [now-2P, now-P] against [now-P, now]; if
    the earliest data is younger than ``2 * requested_seconds`` the prior
    period is partly or wholly empty and the comparison is a void. Pure.
    """
    if not min_clock_seen or requested_seconds <= 0:
        return False
    covered = max(0, now - int(min_clock_seen))
    return covered < 2 * requested_seconds
