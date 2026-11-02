"""ADR 160: trend reads are sized to what the server can answer.

Synthetic item ids only; no fixture names a real host or address.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from tests.wiretest import RecordingClient, covers_window
from zbbx_mcp import trends
from zbbx_mcp.trends import fetch_trends, plan_chunks, window_hours

NOW = int(time.time())


class TestPure:
    def test_a_window_of_n_hours_touches_n_plus_one_buckets(self):
        assert window_hours(NOW - 24 * 3600, NOW) == 25
        assert window_hours(NOW - 24 * 3600 - 1, NOW) == 26
        assert window_hours(NOW, NOW) == 1

    def test_chunks_respect_the_row_budget(self):
        ids = [f"i{n}" for n in range(1000)]
        chunks = plan_chunks(ids, hours=721, rows_per_call=75_000)
        assert all(len(c) * 721 <= 75_000 for c in chunks)
        assert [i for c in chunks for i in c] == ids

    def test_a_window_longer_than_the_budget_still_sends_one_item(self):
        assert plan_chunks(["a", "b"], hours=10**6, rows_per_call=100) == [["a"], ["b"]]


def _server(budget: int, peak: list[int] | None = None, fail_on: str | None = None):
    """A trend.get that answers like the frontend: HTTP 500 above ``budget``
    rows, one row per item per hour otherwise. Records peak concurrency."""
    live = [0]

    async def trend_get(p):
        live[0] += 1
        if peak is not None:
            peak[0] = max(peak[0], live[0])
        try:
            await asyncio.sleep(0)
            if p["limit"] > budget:
                raise httpx.HTTPStatusError("500", request=None, response=None)  # type: ignore[arg-type]
            if fail_on and fail_on in p["itemids"]:
                raise ValueError("Zabbix API error (-32500): boom")
            till = p.get("time_till", NOW)
            return [{"itemid": i, "clock": str(t), "value_avg": "1"}
                    for i in p["itemids"] for t in range(p["time_from"], till + 1, 3600)]
        finally:
            live[0] -= 1

    class C(RecordingClient):
        async def call(self, method, params=None):
            self.calls.append((method, params))
            return await trend_get(params)

    return C({})


class TestFetch:
    def test_a_fleet_month_that_one_request_cannot_carry_is_read_whole(self):
        ids = [f"i{n}" for n in range(4000)]
        c = _server(budget=trends.ROWS_PER_CALL)
        rows = asyncio.run(fetch_trends(c, ids, NOW - 30 * 86400, time_till=NOW))
        assert {r["itemid"] for r in rows} == set(ids)
        assert len(rows) == 4000 * 721
        assert len(c.calls) > 1 and all(covers_window(p) for _, p in c.calls)

    def test_two_months_are_not_cut_to_one(self):
        c = _server(budget=trends.ROWS_PER_CALL)
        rows = asyncio.run(fetch_trends(c, ["a", "b"], NOW - 60 * 86400, time_till=NOW))
        clocks = sorted(int(r["clock"]) for r in rows if r["itemid"] == "a")
        assert clocks[0] == NOW - 60 * 86400 and clocks[-1] == NOW

    def test_concurrency_is_bounded(self):
        peak = [0]
        c = _server(budget=10**9, peak=peak)
        asyncio.run(fetch_trends(c, [f"i{n}" for n in range(5000)], NOW - 30 * 86400))
        assert 1 < peak[0] <= trends.CONCURRENCY

    def test_an_error_in_any_chunk_is_an_error_not_a_partial_answer(self):
        ids = [f"i{n}" for n in range(1000)]
        c = _server(budget=10**9, fail_on="i999")
        with pytest.raises(ValueError):
            asyncio.run(fetch_trends(c, ids, NOW - 30 * 86400))

    def test_duplicates_are_asked_once_and_nothing_is_asked_for_nothing(self):
        c = _server(budget=10**9)
        asyncio.run(fetch_trends(c, ["a", "a", "b"], NOW - 3600, ("itemid", "value_max")))
        (_, p), = c.calls
        assert p["itemids"] == ["a", "b"] and p["output"] == ["itemid", "value_max"]
        assert "time_till" not in p
        c2 = _server(budget=10**9)
        assert asyncio.run(fetch_trends(c2, [], NOW)) == [] and c2.calls == []
