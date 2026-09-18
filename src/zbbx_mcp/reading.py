"""A last value is a value with an age (ADR 141).

Zabbix keeps an item's ``lastvalue`` forever. Read on its own it is
indistinguishable from a live reading, and three releases in a row removed
the same defect from one surface each: a dead agent's final idle reading of
0 printed as "CPU 100% now" (ADR 136), a never-collected item printed as
"0" (ADR 137), a day-old value printed as current (ADR 140). It recurred
because every tool read ``item["lastvalue"]`` directly, and nothing stopped
the next one from doing the same.

This module is the one place that turns an item into a :class:`Reading` —
a value **and** its age **and** a state. The guard test in
``tests/test_guards.py`` keeps every other module from reading the raw
field, so a tool can no longer obtain a bare last value by accident. Pure:
no I/O, no package imports.
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass

__all__ = ["LIVE_VALUE_MAX_AGE_S", "NOT_REPORTING", "READING_STATES", "Reading", "read_item"]

#: A "last value" older than this is not a current reading. Agent items refresh
#: every 1–5 minutes; half an hour of silence means the agent is down, and the
#: value it left behind describes the past, not now (ADR 140).
LIVE_VALUE_MAX_AGE_S = 1800

#: Every state a reading can be in. Only ``live`` carries a value.
READING_STATES = ("live", "stale", "never", "unparsable", "missing_clock")

#: How a non-reading renders. Pinned by the ADR 140 tests; keep the wording.
NOT_REPORTING = "n/a (not reporting)"


@dataclass(frozen=True, slots=True)
class Reading:
    """One item's last value, with its age and whether it is current.

    ``value`` is a number only in state ``live``; in every other state it is
    ``None``, so arithmetic on a non-reading fails loudly instead of producing
    a figure. ``age_s`` is known whenever the item has a clock (``live``,
    ``stale``, ``unparsable``) and ``None`` when it has none.
    """

    value: float | None
    age_s: int | None
    state: str

    @property
    def is_live(self) -> bool:
        return self.state == "live"

    def text(self, unit: str = "") -> str:
        """``"12.3 %"`` for a live reading, ``"n/a (not reporting)"`` otherwise."""
        if self.value is None:
            return NOT_REPORTING
        return f"{self.value:.1f} {unit}".rstrip()


def read_item(item: dict, now: int | None = None) -> Reading:
    """The only way to turn a Zabbix item into a value. Pure.

    States, in the order they are decided:

    - ``missing_clock`` — the caller did not request ``lastclock`` (or it is
      unreadable). Fails closed: no clock, no reading.
    - ``never`` — ``lastclock == 0``, Zabbix's never-collected sentinel; the
      ``lastvalue`` beside it is a placeholder, not a measurement (ADR 136/137).
    - ``stale`` — older than :data:`LIVE_VALUE_MAX_AGE_S`; the value describes
      the past, not now (ADR 140).
    - ``unparsable`` — fresh, but not a number.
    - ``live`` — a current reading.
    """
    raw_clock = item.get("lastclock")
    if raw_clock is None or raw_clock == "":
        return Reading(None, None, "missing_clock")
    try:
        clock = int(raw_clock)
    except (TypeError, ValueError):
        return Reading(None, None, "missing_clock")
    if clock <= 0:
        return Reading(None, None, "never")
    age = (now if now is not None else int(_time.time())) - clock
    if age > LIVE_VALUE_MAX_AGE_S:
        return Reading(None, age, "stale")
    raw_value = item.get("lastvalue")
    if raw_value is None:
        return Reading(None, age, "unparsable")
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return Reading(None, age, "unparsable")
    return Reading(value, age, "live")
