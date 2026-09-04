"""CPU load judgement — pure functions, no Zabbix calls.

Separate from ``tools/diagnose.py`` for two reasons: that file is at its size
budget, and the flat-signature logic below is worth testing on its own rather
than only through a verdict.

Two independent signals, because they fail in opposite directions.

**Level** answers "is this host busy". It is what every threshold alert uses,
and it has a structural blind spot: a process that uses a fixed number of cores
occupies a fixed *fraction* of the machine, so on a large host it can sit
permanently below any threshold set for a small one. Four busy cores are 100%
of a 4-core host and 50% of an 8-core one. The same workload is either an
emergency or invisible depending on hardware it does not control.

**Variance** answers "is this varying like real work". It is threshold
independent, which is exactly why it catches what level misses. Real load
breathes — it has a daily shape, it responds to traffic, its hourly min and max
differ. A process that simply runs produces an hourly min, average and max
within a hair of each other, hour after hour, with no diurnal shape at all.
That flatness is the signal, and it does not care what fraction of the host is
involved.

This mirrors ``detect_traffic_shaping``, which separates a rate limit from lost
demand by looking for a flat ceiling rather than a low number. Same reasoning,
applied to utilisation instead of egress.
"""

from __future__ import annotations

from collections.abc import Sequence

__all__ = [
    "CPU_BUSY_PCT",
    "FLAT_MAX_SPREAD_PP",
    "FLAT_MIN_HOURS",
    "FLAT_MIN_PCT",
    "cpu_pct_from_items",
    "flat_run_hours",
    "judge_cpu",
]

#: Direct utilisation key, and the idle key it is derived from when absent.
_UTIL_KEY = "system.cpu.util"
_IDLE_KEY = "system.cpu.util[,idle]"


def cpu_pct_from_items(items) -> float | None:
    """Utilisation from an already-fetched item list. Makes no API call.

    Returns ``None`` when no usable item exists — including when the item is
    present but has **never collected**.

    Zabbix reports a never-collected item with ``lastclock = 0`` and
    ``lastvalue`` empty or ``0``. Read without checking the clock, that renders
    as a real measurement of zero: a load average of ``0`` timestamped
    1970-01-01 reads as a perfectly idle host rather than as a host nothing is
    known about. The epoch-zero sentinel is the tell, and it has to be honoured
    at every point a ``lastvalue`` is consumed.
    """
    direct: float | None = None
    idle: float | None = None
    for it in items or []:
        key = (it.get("key_") or "").strip()
        if key not in (_UTIL_KEY, _IDLE_KEY):
            continue
        try:
            if int(it.get("lastclock") or 0) <= 0:
                continue  # never collected — not a value of zero
            value = float(it.get("lastvalue"))
        except (TypeError, ValueError):
            continue
        if key == _UTIL_KEY:
            direct = round(value, 1)
        else:
            idle = round(100.0 - value, 1)
    return direct if direct is not None else idle

#: Utilisation at or above which a host is called busy outright.
CPU_BUSY_PCT = 85.0

#: A flat run is only interesting above this level. Below it the host is idle
#: and flatness is just the absence of work, which is not a finding.
FLAT_MIN_PCT = 20.0

#: Hourly max minus min, in percentage points, still counted as "not varying".
#: Real load on a busy host moves several points within an hour; a fixed set of
#: spinning threads moves a fraction of one.
FLAT_MAX_SPREAD_PP = 2.0

#: Consecutive flat hours before the run is worth reporting. Long enough that a
#: quiet night or a steady batch job does not qualify.
FLAT_MIN_HOURS = 6


def flat_run_hours(hourly: Sequence[tuple[float | None, float | None]]) -> int:
    """Count consecutive most-recent hours that show no variation.

    ``hourly`` is ``(min, max)`` per hour, **most recent first**. Returns the
    length of the leading run where the hour is both busy enough to matter and
    varies by almost nothing.

    An hour with either bound missing ends the run rather than being skipped.
    Skipping it would splice two separate runs into one longer one and report a
    duration that never happened; a gap in the evidence is not evidence of
    continuity.
    """
    run = 0
    for lo, hi in hourly:
        if lo is None or hi is None:
            break
        if lo < FLAT_MIN_PCT:
            break
        if (hi - lo) > FLAT_MAX_SPREAD_PP:
            break
        run += 1
    return run


def judge_cpu(
    pct: float | None,
    flat_hours: int = 0,
) -> tuple[str | None, str]:
    """Judge a host's CPU. Returns ``(flag, note)``.

    ``flag`` is one of ``None`` (nothing to say), ``"unmeasured"``, ``"flat"``
    or ``"busy"``.

    ``unmeasured`` is a real answer, not a missing one. A host with no CPU item
    is not a host with acceptable CPU, and the difference has to survive into
    the caller's verdict — otherwise "we did not look" and "we looked and it was
    fine" become the same sentence.

    ``flat`` outranks ``busy``. A host at 95% that varies is doing work; a host
    pinned to a hair's breadth for hours is doing the same thing over and over,
    which is the more specific and more actionable statement.
    """
    if pct is None:
        return "unmeasured", (
            "CPU was NOT checked — no utilisation item on this host. That is "
            "not the same as CPU being fine, and no verdict below rests on it."
        )

    if flat_hours >= FLAT_MIN_HOURS:
        return "flat", (
            f"CPU has been flat at ~{pct:.0f}% for {flat_hours}h — hourly min "
            f"and max within {FLAT_MAX_SPREAD_PP:.0f}pp, with no daily shape. "
            "Real load varies; something is running at a constant rate. Note "
            "this is independent of any threshold, so it fires at levels an "
            "alert never would. Identify the process before assuming capacity."
        )

    if pct >= CPU_BUSY_PCT:
        return "busy", (
            f"CPU at {pct:.0f}% — at or above {CPU_BUSY_PCT:.0f}%. Check what "
            "is consuming it and whether the host needs headroom."
        )

    return None, f"CPU {pct:.0f}%"
