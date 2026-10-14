"""A traffic-drop alert is judged by traffic, not by the problem table (ADR 152).

A "traffic fell sharply" trigger compares the last minutes with a short
baseline. Once a node settles at a few percent of normal the condition no
longer holds, Zabbix closes the problem, and a verdict read off the problem
table says "recovered" over a node that is still down. For this one trigger
class the problem table is exactly the untrustworthy state a triage tool
exists to see past, so the verdict comes from the carrier interface instead:
the last two hours against what this hour of day normally carries.

Before the verdict the drop gets a *shape*, because four different events
look identical to a threshold trigger:

- ``cliff``       — from normal to under 5 % within minutes: a hard cut;
- ``decay``       — the same fall spread over hours: new sessions refused,
                    old ones draining;
- ``periodic``    — the node has gone down and come back on its own at least
                    twice in the window, and the current trough is no longer
                    than the troughs it recovered from;
- ``maintenance`` — the drop sits on a gap in the samples (agent or service
                    restart, redeploy).

Everything here is pure; the tool fetches the series and calls in.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from statistics import median

# Alert text that marks the traffic-drop trigger class. Generic phrases only;
# a deployment whose trigger is named otherwise sets ZABBIX_TRAFFIC_DROP_PATTERN.
_DEFAULT_PATTERN = (
    r"traffic\s+(drop|fell|loss|down)|drop\s+in\s+traffic|no\s+traffic|net\.if\.in"
)

RECOVERED_SHARE = 0.5     # recent >= 50 % of the usual for this hour = recovered
UP_SHARE = 0.5            # a sample at or above this share of usual is "up"
DOWN_SHARE = 0.3          # below this it is "down" (hysteresis between the two)
CLIFF_SHARE = 0.05        # a cliff ends under 5 % of usual ...
CLIFF_MINUTES = 15        # ... within this many minutes of the last "up" sample
GAP_SECONDS = 300         # a hole longer than this between samples is a gap
GAP_WINDOW_SECONDS = 600  # a gap within ±10 min of the drop marks maintenance
RECENT_SECONDS = 2 * 3600


def traffic_drop_pattern() -> re.Pattern[str]:
    raw = os.environ.get("ZABBIX_TRAFFIC_DROP_PATTERN", "").strip() or _DEFAULT_PATTERN
    try:
        return re.compile(raw, re.IGNORECASE)
    except re.error:
        return re.compile(_DEFAULT_PATTERN, re.IGNORECASE)


def is_traffic_drop_alert(*texts: str) -> bool:
    """True when any of ``texts`` (the alert line, problem names) names the
    traffic-drop trigger class."""
    rx = traffic_drop_pattern()
    return any(rx.search(t or "") for t in texts)


def usual_for_hour(hourly: list[tuple[int, float]], hour: int) -> float | None:
    """Median traffic for ``hour`` (±1 h) over the hourly series, or None when
    fewer than 9 samples exist — the same bucket the drop detector uses
    (ADR 097/100), at the median rather than the floor, because "recovered"
    asks "is it back to normal", not "is it above the worst normal hour"."""
    neighbours = {(hour - 1) % 24, hour % 24, (hour + 1) % 24}
    bucket = [v for (clock, v) in hourly if (int(clock) // 3600) % 24 in neighbours]
    if len(bucket) < 9:
        return None
    return float(median(bucket))


@dataclass(frozen=True)
class Shape:
    kind: str                 # cliff | decay | periodic | maintenance | none | unknown
    reason: str
    drop_ts: int | None = None


def _gap_near(series: list[tuple[int, float]], ts: int) -> tuple[int, int] | None:
    for (a, _), (b, _) in zip(series, series[1:], strict=False):
        if b - a > GAP_SECONDS and a - GAP_WINDOW_SECONDS <= ts <= b + GAP_WINDOW_SECONDS:
            return a, b
    return None


def classify_shape(series: list[tuple[int, float]], usual: float | None, now: int) -> Shape:
    """Shape of the most recent drop in ``series`` ([(epoch, mbps)], any
    order, ~1-min samples over the last day) against ``usual`` Mbps. Pure."""
    pts = sorted((int(t), float(v)) for t, v in series)
    if not pts or not usual or usual <= 0:
        return Shape("unknown", "no usable traffic history for this hour")
    up, down = UP_SHARE * usual, DOWN_SHARE * usual

    # Walk with hysteresis: state flips to down below DOWN, back up at UP.
    state = "up" if pts[0][1] >= up else ("down" if pts[0][1] < down else "up")
    last_up_ts = pts[0][0] if state == "up" else None
    drops: list[int] = []           # timestamps of up→down transitions
    rises: list[int] = []           # timestamps of down→up transitions
    for ts, v in pts:
        if state == "up":
            if v >= up:
                last_up_ts = ts
            elif v < down:
                state = "down"
                drops.append(ts)
        elif v >= up:
            state = "up"
            rises.append(ts)
            last_up_ts = ts

    if not drops:
        return Shape("none", "no fall below 30 % of usual in the window")
    drop_ts = drops[-1]

    gap = _gap_near(pts, drop_ts)
    if gap:
        a, b = gap
        return Shape("maintenance",
                     f"samples missing for {(b - a) // 60} min around the drop "
                     "(agent or service restart, redeploy)", drop_ts)

    # Completed troughs: a drop followed by a rise.
    troughs = [r - d for d in drops for r in rises if r > d and not any(d < d2 < r for d2 in drops)]
    current = (now - drop_ts) if state == "down" else 0
    if len(troughs) >= 2 and state == "down" and current <= 1.5 * max(troughs):
        return Shape("periodic",
                     f"{len(troughs)} earlier troughs recovered on their own "
                     f"(longest {max(troughs) // 60} min); this one is {current // 60} min old",
                     drop_ts)

    first_floor = next((ts for ts, v in pts if ts >= drop_ts and v < CLIFF_SHARE * usual), None)
    if last_up_ts is not None and first_floor is not None and first_floor - last_up_ts <= CLIFF_MINUTES * 60:
        return Shape("cliff", f"under 5 % of usual within {(first_floor - last_up_ts) // 60 or 1} min",
                     drop_ts)
    span = (drop_ts - last_up_ts) // 60 if last_up_ts is not None else None
    return Shape("decay",
                 f"fell over {span} min" if span else "fell gradually", drop_ts)


@dataclass(frozen=True)
class Evidence:
    recent_mbps: float | None
    usual_mbps: float | None
    shape: Shape

    @property
    def share(self) -> float | None:
        if self.recent_mbps is None or not self.usual_mbps:
            return None
        return self.recent_mbps / self.usual_mbps


def _fmt(v: float | None) -> str:
    return "n/a" if v is None else (f"{v:,.0f}" if v >= 10 else f"{v:.1f}")


def traffic_triage(ev: Evidence, problem_open: bool, now: int) -> tuple[str, str]:
    """(verdict, action) for a traffic-drop alert. Every verdict quotes the
    numbers it rests on; without numbers there is no "recovered". Pure."""
    trig = "trigger still open" if problem_open else "trigger closed"
    if ev.recent_mbps is None or ev.usual_mbps is None:
        return ("unmeasured",
                f"{trig}; no usable traffic history for this host, so recovery cannot be "
                "confirmed — check the carrier interface (diagnose_host).")
    pct = (ev.share or 0) * 100
    nums = (f"last 2 h {_fmt(ev.recent_mbps)} Mbps = {pct:.0f} % of the "
            f"{_fmt(ev.usual_mbps)} Mbps usual for this hour")
    if (ev.share or 0) >= RECOVERED_SHARE:
        return ("recovered", f"{trig}; {nums}. Traffic is back — no action.")
    since = f", down {(now - ev.shape.drop_ts) // 60} min" if ev.shape.drop_ts else ""
    shape = f" [{ev.shape.kind}: {ev.shape.reason}]" if ev.shape.kind not in ("none", "unknown") else ""
    if ev.shape.kind == "maintenance":
        return ("maintenance", f"{trig}; {nums}{since}{shape}. Re-check in 30 min before acting.")
    if ev.shape.kind == "periodic":
        return ("periodic", f"{trig}; {nums}{since}{shape}. Not a block by this evidence — "
                "check rotation and firewall rules for the cycle.")
    return ("still_down", f"{trig}; {nums}{since}{shape}. The node is still down — "
            "investigate (diagnose_host), do not treat as resolved.")
