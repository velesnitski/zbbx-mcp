"""Shaping detection — the flat-ceiling signature (ADR 104).

The tool's whole value is a distinction two existing detectors cannot make:
a host that was rate-limited vs one that simply lost demand. Both read as
"less traffic" in an average. So the tests that matter most are the negative
controls — a healthy host with varying peaks, and a normal diurnal curve,
must never read as shaped.
"""

import time

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp.tools import traffic_shaping as ts_mod
from zbbx_mcp.tools.traffic_shaping import (
    CAPPED,
    DROPPED,
    IDLE,
    INSUFFICIENT,
    NORMAL,
    SHAPED,
    ceiling_hit_rate,
    classify_shaping,
    percentile,
)

# 24 hours of each shape.
FLAT = [100.0, 100.1, 99.9, 100.0, 100.2, 99.8] * 4      # pinned at ~100
VARY = [400.0, 380.0, 420.0, 350.0, 410.0, 390.0] * 4    # healthy, wobbling
DIURNAL = [80.0, 140.0, 260.0, 390.0, 420.0, 300.0] * 4  # day/night curve
LOWVARY = [120.0, 60.0, 200.0, 90.0, 150.0, 70.0] * 4    # fell, still spread


class TestPercentile:
    def test_nearest_rank_and_empty(self):
        assert percentile([], 0.95) == 0.0
        assert percentile([1.0], 0.95) == 1.0
        assert percentile([1.0, 2.0, 3.0, 4.0], 0.0) == 1.0
        assert percentile([1.0, 2.0, 3.0, 4.0], 1.0) == 4.0

    def test_one_freak_minute_does_not_become_the_ceiling(self):
        # p95 over 24 samples ignores the single outlier; max would not.
        assert percentile(VARY + [5000.0], 0.95) < 1000.0


class TestCeilingHitRate:
    def test_clipped_series_piles_up_at_the_ceiling(self):
        rate, ceiling, hits, active = ceiling_hit_rate(FLAT)
        assert rate == 1.0 and hits == active
        assert 99.0 < ceiling < 101.0

    def test_healthy_varying_series_does_not(self):
        # THE control that killed the first metric: measuring the spread of
        # the top quartile called this flat, because selecting the largest
        # values compresses any distribution. Counting how many hours reach
        # the ceiling does not have that failure mode.
        rate, _, _, _ = ceiling_hit_rate(VARY)
        assert rate < 0.4, rate

    def test_diurnal_curve_does_not(self):
        rate, _, _, _ = ceiling_hit_rate(DIURNAL)
        assert rate < 0.4, rate

    def test_quiet_hours_neither_dilute_nor_fake_the_rate(self):
        # Night hours top out far below any cap; they must not count as
        # active, or a shaped host's rate would be diluted below threshold.
        shaped_with_nights = ([100.0, 100.0, 100.1, 99.9] + [8.0, 6.0]) * 4
        rate, _, _, active = ceiling_hit_rate(shaped_with_nights)
        assert rate == 1.0
        assert active == 16          # the 8 quiet hours excluded

    def test_empty_and_zero_series_are_not_flat(self):
        assert ceiling_hit_rate([]) == (0.0, 0.0, 0, 0)
        assert ceiling_hit_rate([0.0] * 24)[0] == 0.0


class TestClassify:
    def test_flat_after_a_fall_is_shaped(self):
        v = classify_shaping(FLAT, VARY)
        assert v.verdict == SHAPED
        assert v.drop_pct > 25 and v.base_ceiling_mbps
        assert "cap, not demand" in v.note

    def test_varying_after_a_fall_is_dropped_not_shaped(self):
        # The distinction the tool exists for: traffic fell, but the peaks
        # still spread — that is demand or reachability, someone else's tool.
        v = classify_shaping(LOWVARY, VARY)
        assert v.verdict == DROPPED

    def test_healthy_host_is_normal(self):
        assert classify_shaping(VARY, VARY).verdict == NORMAL
        assert classify_shaping(DIURNAL, DIURNAL).verdict == NORMAL

    def test_ceiling_with_no_drop_is_capped(self):
        assert classify_shaping(FLAT, FLAT).verdict == CAPPED

    def test_no_baseline_reads_capped_never_shaped(self):
        # Without a baseline the drop half cannot be evaluated, so the tool
        # must not claim a change it had no way to see.
        v = classify_shaping(FLAT, [])
        assert v.verdict == CAPPED
        assert v.base_ceiling_mbps is None

    def test_idle_host_is_not_shaped(self):
        # A spare box is flat at nothing — the most obvious false positive.
        assert classify_shaping([0.4] * 24, [0.4] * 24).verdict == IDLE

    def test_too_few_hours_is_insufficient_not_a_verdict(self):
        assert classify_shaping([100.0] * 4, VARY).verdict == INSUFFICIENT

    def test_too_few_active_hours_is_insufficient(self):
        # 24 hours, but only 3 of them active: a ceiling seen that rarely is
        # an artefact.
        series = [100.0, 100.0, 100.0] + [1.5] * 21
        assert classify_shaping(series, VARY).verdict == INSUFFICIENT


class TestWire:
    def _client(self, recent, base):
        now = int(time.time())
        rows = [{"itemid": "1", "clock": str(now - 3600 * (i + 1)),
                 "value_max": str(v * 1_000_000)} for i, v in enumerate(recent)]
        rows += [{"itemid": "1", "clock": str(now - 3600 * (200 + i)),
                  "value_max": str(v * 1_000_000)} for i, v in enumerate(base)]
        return RecordingClient({
            "host.get": [{"hostid": "9", "host": "node-fj1",
                          "groups": [{"name": "edge"}]}],
            "item.get": [{"itemid": "1", "hostid": "9",
                          "key_": "net.if.in[eth0]", "lastvalue": "5000000"}],
            "trend.get": rows,
        })

    def test_requests_value_max_never_value_avg(self):
        # The core design decision: averaging erases clipping. If this ever
        # regresses to value_avg the tool silently stops working.
        c = self._client(FLAT, VARY)
        run_tool(ts_mod, "detect_traffic_shaping", c, hours=24, baseline_days=14)
        trend = [p for m, p in c.calls if m == "trend.get"]
        assert trend, "no trend.get issued"
        assert "value_max" in trend[0]["output"]
        assert "value_avg" not in trend[0]["output"]

    def test_shaped_host_is_rendered(self):
        out = run_tool(ts_mod, "detect_traffic_shaping",
                       self._client(FLAT, VARY), hours=24, baseline_days=14)
        assert "SHAPED" in out and "node-fj1" in out

    def test_healthy_host_is_not_flagged(self):
        out = run_tool(ts_mod, "detect_traffic_shaping",
                       self._client(VARY, VARY), hours=24, baseline_days=14)
        assert "SHAPED" not in out
        assert "No host is pinned" in out


class TestDiscoveryIsTemplateAgnostic:
    """ADR 105 — found in the field: three hosts with an obvious anomaly and
    the tool said nothing, because it had never looked at them."""

    def _client(self, key):
        now = int(time.time())
        rows = [{"itemid": "1", "clock": str(now - 3600 * (i + 1)),
                 "value_max": str(v * 1_000_000)} for i, v in enumerate(FLAT)]
        rows += [{"itemid": "1", "clock": str(now - 3600 * (200 + i)),
                  "value_max": str(v * 1_000_000)} for i, v in enumerate(VARY)]
        return RecordingClient({
            "host.get": [{"hostid": "9", "host": "node-fj1",
                          "groups": [{"name": "edge"}]}],
            "item.get": [{"itemid": "1", "hostid": "9", "key_": key,
                          "lastvalue": "5000000"}],
            "trend.get": rows,
        })

    def test_discovers_by_key_not_by_item_name(self):
        # A name search ("Incoming network traffic") matches the in-house
        # template only; the stock template names the same metric
        # "Interface enp3s0: Bits received".
        c = self._client('net.if.in["enp3s0"]')
        run_tool(ts_mod, "detect_traffic_shaping", c, hours=24, baseline_days=14)
        item_calls = [p for m, p in c.calls if m == "item.get"]
        assert item_calls, "no item.get issued"
        assert "name" not in item_calls[0].get("search", {})
        assert item_calls[0]["search"]["key_"] == "*net.if.in[*"
        assert item_calls[0]["searchWildcardsEnabled"] is True

    def test_stock_template_host_is_actually_examined(self):
        out = run_tool(ts_mod, "detect_traffic_shaping",
                       self._client('net.if.in["enp3s0"]'),
                       hours=24, baseline_days=14)
        assert "SHAPED" in out and "node-fj1" in out

    def test_host_without_usable_items_is_disclosed_not_dropped(self):
        # The failure mode that hid the bug: a host the tool could not measure
        # simply vanished, and an empty table read as a clean bill of health.
        c = RecordingClient({
            "host.get": [{"hostid": "9", "host": "node-fj1",
                          "groups": [{"name": "edge"}]},
                         {"hostid": "8", "host": "node-ki1",
                          "groups": [{"name": "edge"}]}],
            "item.get": [{"itemid": "1", "hostid": "9",
                          "key_": "net.if.in[eth0]", "lastvalue": "5000000"}],
            "trend.get": [],
        })
        out = run_tool(ts_mod, "detect_traffic_shaping", c,
                       hours=24, baseline_days=14)
        assert "NOT examined" in out
        assert "node-ki1" in out
        assert "unmeasured, not healthy" in out
