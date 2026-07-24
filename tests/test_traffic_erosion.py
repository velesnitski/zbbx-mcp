"""detect_traffic_erosion tests (ADR 091).

Pure-core invariants (weekly bucketing, slope, cohort-relative classification)
plus the wire contract. Fixtures are synthetic.
"""

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp.tools import traffic_erosion as ero_mod
from zbbx_mcp.tools.traffic_erosion import (
    DEMAND,
    ERODING,
    IDLE,
    INSUFFICIENT,
    RECOVERING,
    STABLE,
    classify_erosion,
    linreg_slope,
    weekly_means,
)

WEEK = 7 * 86400


def _weekly(vals):
    """Turn a list of weekly means into the (week_index, mean) form classify wants."""
    return list(enumerate(float(v) for v in vals))


class TestWeeklyMeans:
    def test_buckets_oldest_to_newest(self):
        now = 100 * WEEK
        # one point per week, value == week number, over 4 weeks
        pts = [(now - (4 - k) * WEEK + 3600, float(k)) for k in range(4)]
        wm = weekly_means(pts, now, 4)
        assert [k for k, _ in wm] == [0, 1, 2, 3]
        assert [v for _, v in wm] == [0.0, 1.0, 2.0, 3.0]

    def test_means_within_a_week(self):
        now = 10 * WEEK
        # two samples in the oldest week -> their mean
        base = now - 10 * WEEK
        pts = [(base + 3600, 4.0), (base + 7200, 6.0)]
        wm = weekly_means(pts, now, 10)
        assert wm == [(0, 5.0)]

    def test_gap_week_omitted_not_zeroed(self):
        now = 6 * WEEK
        start = now - 6 * WEEK
        # data only in weeks 0 and 5 — the empty middle weeks must not appear
        pts = [(start + 3600, 2.0), (start + 5 * WEEK + 3600, 8.0)]
        wm = weekly_means(pts, now, 6)
        assert [k for k, _ in wm] == [0, 5]

    def test_out_of_window_dropped(self):
        now = 6 * WEEK
        pts = [(now - 100 * WEEK, 9.0), (now + WEEK, 9.0)]  # too old / future
        assert weekly_means(pts, now, 6) == []


class TestLinregSlope:
    def test_declining_negative(self):
        assert linreg_slope([0, 1, 2, 3], [10, 8, 6, 4]) < 0

    def test_flat_zero(self):
        assert linreg_slope([0, 1, 2, 3], [5, 5, 5, 5]) == 0.0

    def test_single_point_zero(self):
        assert linreg_slope([0], [5]) == 0.0


class TestClassifyErosion:
    def test_steady_decline_vs_flat_cohort_is_eroding(self):
        wm = _weekly([10, 8, 6, 4, 3, 2])
        v = classify_erosion(wm, min_baseline=1.0, min_decline_pct=30.0,
                             cohort_slope_pct=0.0)
        assert v.state == ERODING
        assert v.slope_pct < 0
        assert v.cum_decline_pct >= 30.0

    def test_same_decline_tracking_cohort_is_demand(self):
        # The host declines, but so does the whole cohort at the same rate.
        wm = _weekly([10, 8, 6, 4, 3, 2])
        solo = classify_erosion(wm, min_baseline=1.0, min_decline_pct=30.0,
                                cohort_slope_pct=None)
        v = classify_erosion(wm, min_baseline=1.0, min_decline_pct=30.0,
                             cohort_slope_pct=solo.slope_pct)
        assert v.state == DEMAND

    def test_faster_than_cohort_is_eroding(self):
        wm = _weekly([10, 8, 6, 4, 3, 2])
        solo = classify_erosion(wm, min_baseline=1.0, min_decline_pct=30.0,
                                cohort_slope_pct=None)
        # cohort barely declining -> this host beats it -> host-specific.
        v = classify_erosion(wm, min_baseline=1.0, min_decline_pct=30.0,
                             cohort_slope_pct=solo.slope_pct + 20.0)
        assert v.state == ERODING

    def test_cohort_none_material_decline_is_eroding(self):
        # Single-host scope: no cohort to compare, a real decline still flags.
        wm = _weekly([10, 8, 6, 4, 3, 2])
        v = classify_erosion(wm, min_baseline=1.0, min_decline_pct=30.0,
                             cohort_slope_pct=None)
        assert v.state == ERODING
        assert v.relative_pct is None

    def test_idle_below_floor(self):
        wm = _weekly([0.1, 0.1, 0.05, 0.2, 0.0, 0.1])
        v = classify_erosion(wm, min_baseline=1.0, min_decline_pct=30.0,
                             cohort_slope_pct=0.0)
        assert v.state == IDLE

    def test_insufficient_weeks(self):
        wm = _weekly([10, 5, 2])
        v = classify_erosion(wm, min_baseline=1.0, min_decline_pct=30.0,
                             cohort_slope_pct=0.0)
        assert v.state == INSUFFICIENT

    def test_stable_flat(self):
        wm = _weekly([5, 5, 5, 5, 5])
        v = classify_erosion(wm, min_baseline=1.0, min_decline_pct=30.0,
                             cohort_slope_pct=0.0)
        assert v.state == STABLE

    def test_recovering_rise(self):
        wm = _weekly([2, 3, 4, 6, 8, 10])
        v = classify_erosion(wm, min_baseline=1.0, min_decline_pct=30.0,
                             cohort_slope_pct=0.0)
        assert v.state == RECOVERING

    def test_idle_excluded_from_cohort_slope(self):
        # Sanity: an idle host reports slope_pct 0 so it neither drags nor lifts
        # a cohort median a caller computes over non-idle states.
        wm = _weekly([0.1, 0.1, 0.1, 0.1])
        v = classify_erosion(wm, min_baseline=1.0, min_decline_pct=30.0)
        assert v.state == IDLE
        assert v.slope_pct == 0.0


class TestDetectTrafficErosionWire:
    def _client(self, trend_records):
        return RecordingClient({
            "host.get": [
                {"hostid": "1", "host": "edge-aa1", "groups": [{"name": "edge"}],
                 "interfaces": [{"ip": "10.0.0.1"}]},
            ],
            "item.get": [
                {"itemid": "i1", "hostid": "1", "key_": "net.if.in[eth0]",
                 "name": "Incoming network traffic on eth0", "lastvalue": "1000000"},
            ],
            "trend.get": trend_records,
        })

    def test_wire_contract_and_eroding_row(self, monkeypatch):
        import time
        now = int(time.time())
        # Six weeks, one hourly-ish point per week, declining 12 -> 2 Mbps.
        # Raw values are Mbps * 1e6 (bits/s) so TRAFFIC_DIVISOR maps back.
        levels = [12, 10, 8, 5, 3, 2]
        recs = []
        for k, mbps in enumerate(levels):
            clock = now - (6 - k) * WEEK + 3600
            recs.append({"itemid": "i1", "clock": str(clock),
                         "value_avg": str(mbps * 1_000_000)})
        client = self._client(recs)
        out = run_tool(ero_mod, "detect_traffic_erosion", client, group="edge")

        # wire: trend.get asked for value_avg over the window on the shortlisted item
        sent = client.sent("trend.get")
        assert sent["itemids"] == ["i1"]
        assert "value_avg" in sent["output"]
        assert sent["time_from"] <= now - 6 * WEEK + 7200
        # single-host scope -> cohort n/a -> a real decline is flagged eroding
        assert "edge-aa1" in out
        assert "ERODING" in out

    def test_no_traffic_items_message(self):
        client = RecordingClient({
            "host.get": [
                {"hostid": "1", "host": "edge-aa1", "groups": [{"name": "edge"}],
                 "interfaces": [{"ip": "10.0.0.1"}]},
            ],
            "item.get": [],
            "trend.get": [],
        })
        out = run_tool(ero_mod, "detect_traffic_erosion", client, group="edge")
        assert "No traffic items" in out

    def test_no_match_message(self):
        client = self._client([])
        out = run_tool(ero_mod, "detect_traffic_erosion", client, group="nonesuch")
        assert "No servers match" in out
