"""A last value is only "current" while the item is reporting (ADR 140).

Zabbix keeps an item's ``lastvalue`` forever. Read without ``lastclock`` it is
indistinguishable from a live reading, and the tools printed a dead agent's
final idle reading of 0 as "CPU 100% now" and a never-collected total-memory
item as "0 GB". Both are the missing-value-as-a-number defect that ADR 136 and
137 removed from CPU verdicts and connection counts; this closes it for the
remaining "now" surfaces.
"""

from __future__ import annotations

import time

import pytest

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp.data import TrendRow
from zbbx_mcp.fetch import LIVE_VALUE_MAX_AGE_S, fetch_cpu_map, fetch_trends_batch, live_items, live_value
from zbbx_mcp.tools import trends_compare

NOW = 1_800_000_000


class TestLiveValue:
    def test_a_fresh_reading_is_a_number(self):
        assert live_value({"lastvalue": "12.5", "lastclock": str(NOW - 60)}, NOW) == 12.5

    def test_never_collected_is_none(self):
        # lastclock 0 is Zabbix's "never produced a value"; lastvalue is a placeholder.
        assert live_value({"lastvalue": "0", "lastclock": "0"}, NOW) is None

    def test_stale_is_none(self):
        assert live_value({"lastvalue": "0", "lastclock": str(NOW - LIVE_VALUE_MAX_AGE_S - 1)}, NOW) is None
        assert live_value({"lastvalue": "0", "lastclock": str(NOW - LIVE_VALUE_MAX_AGE_S + 1)}, NOW) == 0.0

    def test_missing_lastclock_fails_closed(self):
        # A caller that forgot to request lastclock gets "not reporting", never a number.
        assert live_value({"lastvalue": "7"}, NOW) is None

    def test_unparsable_is_none(self):
        assert live_value({"lastvalue": "n/a", "lastclock": str(NOW)}, NOW) is None

    def test_live_items_keeps_only_reporting_items(self):
        items = [{"hostid": "1", "lastvalue": "1", "lastclock": str(NOW)},
                 {"hostid": "2", "lastvalue": "1", "lastclock": "0"}]
        assert [i["hostid"] for i in live_items(items, NOW)] == ["1"]


class TestFetchCpuMap:
    @pytest.mark.asyncio
    async def test_dead_agent_is_absent_not_100_percent(self):
        c = RecordingClient({"item.get": [
            {"hostid": "1", "lastvalue": "60", "lastclock": str(NOW - 30)},   # idle 60 -> used 40
            {"hostid": "2", "lastvalue": "0", "lastclock": str(NOW - 86400)},  # agent silent a day
            {"hostid": "3", "lastvalue": "0", "lastclock": "0"},              # never collected
        ]})
        m = await fetch_cpu_map(c, ["1", "2", "3"], now=NOW)
        assert m == {"1": 40.0}
        assert "lastclock" in c.sent("item.get")["output"]


class TestTrendCurrent:
    def test_current_text_says_not_reporting(self):
        r = TrendRow(hostid="1", hostname="srv-aq9001", metric="cpu", avg=5.0, peak=9.0, min_val=1.0, current=None)
        assert r.current_text("%") == "n/a (not reporting)"
        r.current = 12.3
        assert r.current_text("%") == "12.3 %"

    @pytest.mark.asyncio
    async def test_stale_item_yields_no_current_but_keeps_the_trend(self):
        now = int(time.time())
        c = RecordingClient({
            "host.get": [{"hostid": "1", "host": "srv-aq9001"}],
            "item.get": [{"itemid": "i1", "hostid": "1", "key_": "system.cpu.util[,idle]",
                          "lastvalue": "0", "lastclock": str(now - 86400), "value_type": "0"}],
            "trend.get": [{"itemid": "i1", "clock": str(now - 3600 * k), "num": "60",
                           "value_min": "80", "value_avg": "90", "value_max": "95"} for k in range(1, 25)],
        })
        rows, _ = await fetch_trends_batch(c, ["1"], ["cpu"], "1d")
        assert len(rows) == 1
        assert rows[0].current is None              # NOT 100.0
        assert rows[0].avg == 10.0                  # the week's history is still real
        assert "lastclock" in c.sent("item.get")["output"]


class TestBatchRendersNotReporting:
    def test_table_shows_not_reporting_instead_of_a_number(self):
        now = int(time.time())
        c = RecordingClient({
            "host.get": [{"hostid": "1", "host": "srv-aq9001", "groups": [{"name": "app_free"}]}],
            "item.get": [{"itemid": "i1", "hostid": "1", "key_": "system.cpu.util[,idle]",
                          "lastvalue": "0", "lastclock": "0", "value_type": "0"}],
            "trend.get": [{"itemid": "i1", "clock": str(now - 3600), "num": "60",
                           "value_min": "80", "value_avg": "90", "value_max": "95"}],
        })
        out = run_tool(trends_compare, "get_trends_batch", c, hosts="srv-aq9001", metrics="cpu")
        assert "n/a (not reporting)" in out, out
        assert "| 100.0 %" not in out
