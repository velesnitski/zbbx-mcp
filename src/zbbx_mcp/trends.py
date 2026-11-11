"""Hourly trend reads sized to what the server can answer (ADR 160).

``trend.get`` returns one row per item per hour. A fleet-wide read over a
month in one request is hundreds of thousands of rows; the frontend builds the
whole answer in PHP memory and fails with HTTP 500 long before the database
does. Callers had each worked around that differently: three chunked by host,
the rest sent everything at once and failed at thirty days.

The other half of the same defect is the row ``limit``. It was computed per
call site as ``items × days × 24``, which is one row short per item (a window
of N hours touches N + 1 hourly buckets) and, at two sites, a constant that
ignored the requested window — so a long window came back silently cut, with
no error to say so.

:func:`fetch_trends` is the one place that knows both: it splits the items so
no request carries more than :data:`ROWS_PER_CALL` rows, runs a bounded number
of those requests at once, and sets each request's limit from the window it
actually asks for.
"""

from __future__ import annotations

import asyncio
import math
import os
import time
from collections.abc import Iterable, Sequence

# Rows one request may return. The report tools have chunked at 200 hosts ×
# 30 days (≈144k rows) without failures; half of that leaves headroom for
# wider output columns.
ROWS_PER_CALL = 75_000
# Requests in flight at once: enough to overlap round trips, few enough not to
# occupy every frontend worker of a shared monitoring server. An operator who
# owns the server's capacity may raise it with ZABBIX_TREND_CONCURRENCY (1–16).
CONCURRENCY = 4
MAX_CONCURRENCY = 16


def concurrency() -> int:
    """Trend requests in flight at once, from ``ZABBIX_TREND_CONCURRENCY``
    clamped to 1–16; unset or unparsable means the default. Read per call."""
    raw = os.environ.get("ZABBIX_TREND_CONCURRENCY", "").strip()
    try:
        return min(MAX_CONCURRENCY, max(1, int(raw))) if raw else CONCURRENCY
    except ValueError:
        return CONCURRENCY


def window_hours(time_from: int, time_till: int | None = None) -> int:
    """Hourly buckets a ``[time_from, time_till]`` window can touch. Pure."""
    till = int(time.time()) if time_till is None else int(time_till)
    return max(1, math.ceil((till - int(time_from)) / 3600) + 1)


def plan_chunks(itemids: Sequence[str], hours: int,
                rows_per_call: int = ROWS_PER_CALL) -> list[list[str]]:
    """Split ``itemids`` so each chunk asks for at most ``rows_per_call`` rows.
    Order and duplicates are preserved as given; a window longer than the
    budget still gets one item per request. Pure."""
    per = max(1, rows_per_call // max(1, hours))
    ids = list(itemids)
    return [ids[i:i + per] for i in range(0, len(ids), per)]


async def fetch_trends(
    client,
    itemids: Iterable[str],
    time_from: int,
    output: Sequence[str] = ("itemid", "clock", "value_avg"),
    time_till: int | None = None,
) -> list[dict]:
    """Every trend row for ``itemids`` in the window, fetched in sized chunks.

    Rows come back in chunk order; callers group by ``itemid`` and never rely
    on the server's order. An error in any chunk propagates — a partial answer
    presented as complete is the defect this replaces.
    """
    ids = list(dict.fromkeys(itemids))
    if not ids:
        return []
    hours = window_hours(time_from, time_till)
    sem = asyncio.Semaphore(concurrency())

    async def one(chunk: list[str]) -> list[dict]:
        params: dict = {
            "itemids": chunk,
            "time_from": int(time_from),
            "output": list(output),
            "limit": len(chunk) * hours,
        }
        if time_till is not None:
            params["time_till"] = int(time_till)
        async with sem:
            return list(await client.call("trend.get", params) or [])

    parts = await asyncio.gather(*(one(c) for c in plan_chunks(ids, hours)))
    return [row for part in parts for row in part]
