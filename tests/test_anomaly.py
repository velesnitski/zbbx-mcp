"""Tests for the traffic-drop classifier (anomaly.py, ADR 040).

These exercise the false-positive-prevention logic in isolation, using
the exact shapes that produced false positives in practice:
  - a diurnal trough that the naive detector called a 96% drop
  - a drop measured on an idle interface while the primary uplink flowed
  - a tiny-baseline ratio that is statistically meaningless
and the true-positive shapes that must still fire immediately:
  - an acute block (below seasonal band now, host still serving)
  - a sustained block (anomalous across consecutive buckets)
"""

from zbbx_mcp.anomaly import (
    ARTIFACT,
    BLOCKED_ACUTE,
    BLOCKED_SUSTAINED,
    HEALTHY,
    LOW_DEMAND,
    UNKNOWN,
    classify_drop,
    percentile,
    pick_traffic_interface,
    seasonal_floor,
)


class TestPercentile:
    def test_empty_is_none(self):
        assert percentile([], 10) is None

    def test_single_value(self):
        assert percentile([42.0], 10) == 42.0

    def test_p10_nearest_rank(self):
        # 10 values 1..10; p10 nearest-rank → rank ceil(0.1*10)=1 → 1
        assert percentile([float(i) for i in range(1, 11)], 10) == 1.0

    def test_p50_median_ish(self):
        assert percentile([float(i) for i in range(1, 11)], 50) == 5.0

    def test_p100_is_max(self):
        assert percentile([3.0, 1.0, 2.0], 100) == 3.0

    def test_clamps_out_of_range(self):
        assert percentile([1.0, 2.0, 3.0], -5) == 1.0
        assert percentile([1.0, 2.0, 3.0], 150) == 3.0


class TestSeasonalFloor:
    def _series(self, per_hour):
        # per_hour: dict hour-of-day -> list of values. Build epoch points.
        pts = []
        for hour, vals in per_hour.items():
            for day, v in enumerate(vals):
                # epoch for (day d, hour h): d*24h + hour, in seconds
                clock = (day * 24 + hour) * 3600
                pts.append((clock, v))
        return pts

    def test_too_few_samples_returns_none(self):
        pts = self._series({2: [10.0, 11.0]})  # only 2 samples for hour 2
        assert seasonal_floor(pts, 2, min_samples=3) is None

    def test_floor_is_low_percentile_of_hour_bucket(self):
        # Hour 3 sees [50,52,48,51,49,53,47] across 7 days; nightly hour 3
        pts = self._series({3: [50.0, 52.0, 48.0, 51.0, 49.0, 53.0, 47.0]})
        floor = seasonal_floor(pts, 3, pct=10)
        # p10 nearest-rank of 7 sorted → rank 1 → the min (47)
        assert floor == 47.0

    def test_only_matching_hour_counted(self):
        pts = self._series({
            3: [50.0, 50.0, 50.0],   # the hour we ask about
            14: [5.0, 5.0, 5.0],     # a different (busy-trough) hour, ignored
        })
        assert seasonal_floor(pts, 3, pct=10) == 50.0


class TestPickTrafficInterface:
    def test_none_when_no_baselines(self):
        assert pick_traffic_interface([("tun0", None), ("svc_1", None)]) is None

    def test_picks_highest_baseline_not_current(self):
        # The classic false-positive shape: an idle interface reads near
        # zero, the real uplink carries the load. Baseline selection must
        # pick the uplink ("primary"), never the idle tunnel.
        ifaces = [
            ("tun0", 0.0),
            ("svc_1", 0.0),
            ("primary", 36.0),
            ("ppp1", 0.003),
        ]
        assert pick_traffic_interface(ifaces) == "primary"

    def test_ignores_none_baselines_among_real(self):
        ifaces = [("a", None), ("b", 12.0), ("c", 4.0)]
        assert pick_traffic_interface(ifaces) == "b"


class TestMetricRecentBaselineRatio:
    """The CPU idle→used inversion is the one place a silent bug would
    flip corroboration (a busy host read as idle), so it is pinned here."""

    def _series(self, recent_vals, baseline_vals, recent_start=1000):
        # recent points at >= recent_start, baseline points before it
        pts = []
        for i, v in enumerate(baseline_vals):
            pts.append((recent_start - 100 - i, v))
        for i, v in enumerate(recent_vals):
            pts.append((recent_start + i, v))
        return pts

    def test_plain_ratio(self):
        from zbbx_mcp.anomaly import metric_recent_baseline_ratio
        # recent avg 5, baseline avg 10 → 0.5
        pts = self._series([4.0, 6.0], [10.0, 10.0])
        assert metric_recent_baseline_ratio(pts, 1000) == 0.5

    def test_idle_inversion_busy_host_high_ratio(self):
        from zbbx_mcp.anomaly import metric_recent_baseline_ratio
        # idle: baseline 90% idle (=10% used), recent 80% idle (=20% used)
        # used-ratio = 20/10 = 2.0 → host got BUSIER (correct direction)
        pts = self._series([80.0], [90.0])
        r = metric_recent_baseline_ratio(pts, 1000, invert_pct=True)
        assert r == 2.0

    def test_idle_inversion_idling_host_low_ratio(self):
        from zbbx_mcp.anomaly import metric_recent_baseline_ratio
        # baseline 50% idle (50% used), recent 90% idle (10% used)
        # used-ratio = 10/50 = 0.2 → host went quiet (demand fell)
        pts = self._series([90.0], [50.0])
        r = metric_recent_baseline_ratio(pts, 1000, invert_pct=True)
        assert abs(r - 0.2) < 1e-9

    def test_empty_window_is_none(self):
        from zbbx_mcp.anomaly import metric_recent_baseline_ratio
        # only baseline points, no recent
        pts = [(500, 10.0), (600, 10.0)]
        assert metric_recent_baseline_ratio(pts, 1000) is None

    def test_zero_baseline_is_none(self):
        from zbbx_mcp.anomaly import metric_recent_baseline_ratio
        pts = self._series([0.0], [0.0])
        assert metric_recent_baseline_ratio(pts, 1000) is None

    def test_fully_idle_baseline_inverts_to_zero_used_none(self):
        from zbbx_mcp.anomaly import metric_recent_baseline_ratio
        # baseline 100% idle → 0% used → b_avg<=0 → None (can't ratio)
        pts = self._series([100.0], [100.0])
        assert metric_recent_baseline_ratio(pts, 1000, invert_pct=True) is None


class TestClassifyDropFalsePositives:
    """The shapes that must NOT be flagged as blocks."""

    def test_diurnal_trough_within_seasonal_band_is_healthy(self):
        # Naive view: recent 1.3 vs baseline 36 = 96% drop. But 1.3 is at
        # or above the normal floor for this (nighttime) hour → healthy.
        v = classify_drop(
            recent_avg=6.5, baseline_avg=36.0,
            seasonal_floor_value=5.0,  # normal floor for this hour is ~5
            min_baseline=5.0, drop_pct_threshold=50.0,
        )
        assert v.state == HEALTHY
        assert "seasonal floor" in " ".join(v.reasons)

    def test_tiny_baseline_is_artifact(self):
        # 0.5 -> 0.05 is "90% drop" but signal-free.
        v = classify_drop(
            recent_avg=0.05, baseline_avg=0.5,
            min_baseline=5.0, drop_pct_threshold=50.0,
        )
        assert v.state == ARTIFACT

    def test_minor_dip_is_healthy(self):
        v = classify_drop(
            recent_avg=30.0, baseline_avg=36.0,
            min_baseline=5.0, drop_pct_threshold=50.0,
        )
        assert v.state == HEALTHY

    def test_demand_drop_not_block(self):
        # Traffic down 80%, but connections fell with it → fewer users.
        v = classify_drop(
            recent_avg=4.0, baseline_avg=36.0,
            seasonal_floor_value=20.0,   # below band → anomalous
            min_baseline=5.0,
            conn_ratio=0.10,   # connections collapsed too
        )
        assert v.state == LOW_DEMAND
        assert "fewer users" in " ".join(v.reasons)


class TestClassifyDropTruePositives:
    """The shapes that MUST fire — including immediate blocks."""

    def test_acute_block_fires_immediately(self):
        # Below seasonal band right now, host up, demand held up.
        # sustained_buckets=0 → must still flag, as ACUTE (not delayed).
        v = classify_drop(
            recent_avg=2.0, baseline_avg=36.0,
            seasonal_floor_value=20.0,
            min_baseline=5.0,
            agent_reachable=True,
            conn_ratio=0.95,           # connections held up → still serving
            sustained_buckets=0,
        )
        assert v.state == BLOCKED_ACUTE
        assert v.confidence >= 60
        assert "immediate" in " ".join(v.reasons)

    def test_sustained_block_escalates(self):
        v = classify_drop(
            recent_avg=2.0, baseline_avg=36.0,
            seasonal_floor_value=20.0,
            min_baseline=5.0,
            agent_reachable=True,
            conn_ratio=0.95,
            sustained_buckets=5, sustained_threshold=3,
        )
        assert v.state == BLOCKED_SUSTAINED
        assert "sustained" in " ".join(v.reasons)
        assert v.confidence >= 70

    def test_acute_without_seasonal_data_still_flags_but_capped(self):
        # No seasonal band available — can't confirm diurnal vs real, so
        # still flag (don't suppress a possible block) but cap confidence.
        v = classify_drop(
            recent_avg=2.0, baseline_avg=36.0,
            seasonal_floor_value=None,
            min_baseline=5.0,
            agent_reachable=True,
        )
        assert v.state == BLOCKED_ACUTE
        assert v.confidence <= 55
        assert "no seasonal baseline" in " ".join(v.reasons)

    def test_agent_down_is_unknown_not_block(self):
        # Host-down is diagnose_host's job, not a traffic-block verdict.
        v = classify_drop(
            recent_avg=0.0, baseline_avg=36.0,
            seasonal_floor_value=20.0,
            min_baseline=5.0,
            agent_reachable=False,
        )
        assert v.state == UNKNOWN
        assert "host-down" in " ".join(v.reasons)

    def test_deeper_below_floor_raises_confidence(self):
        shallow = classify_drop(
            recent_avg=18.0, baseline_avg=36.0, seasonal_floor_value=20.0,
            min_baseline=5.0, drop_pct_threshold=40.0, agent_reachable=True,
        )
        deep = classify_drop(
            recent_avg=1.0, baseline_avg=36.0, seasonal_floor_value=20.0,
            min_baseline=5.0, drop_pct_threshold=40.0, agent_reachable=True,
        )
        assert deep.confidence > shallow.confidence


class TestClassifyDropEdges:
    def test_missing_data_is_unknown(self):
        assert classify_drop(recent_avg=None, baseline_avg=36.0).state == UNKNOWN
        assert classify_drop(recent_avg=2.0, baseline_avg=None).state == UNKNOWN

    def test_cpu_corroboration_when_no_connections(self):
        # No conn data, but CPU held up while traffic collapsed → block.
        v = classify_drop(
            recent_avg=2.0, baseline_avg=36.0,
            seasonal_floor_value=20.0, min_baseline=5.0,
            agent_reachable=True,
            cpu_ratio=1.05,   # CPU even rose
        )
        assert v.state in (BLOCKED_ACUTE, BLOCKED_SUSTAINED)
        assert "cpu held up" in " ".join(v.reasons)

    def test_cpu_fell_with_traffic_is_low_demand(self):
        v = classify_drop(
            recent_avg=2.0, baseline_avg=36.0,
            seasonal_floor_value=20.0, min_baseline=5.0,
            agent_reachable=True,
            cpu_ratio=0.08,   # CPU collapsed with traffic
        )
        assert v.state == LOW_DEMAND


class TestAggregateHourlyByCountry:
    """Pure-helper tests for aggregate_hourly_by_country (#159, ADR 051)."""

    def test_sums_same_hour_across_country_hosts(self):
        from zbbx_mcp.anomaly import aggregate_hourly_by_country
        # two hosts in country A; their hour-3600 values sum
        host_series = {
            "1": [(3600, 10.0), (7200, 20.0)],
            "2": [(3600, 5.0), (7200, 8.0)],
        }
        host_country = {"1": "A", "2": "A"}
        out = aggregate_hourly_by_country(host_series, host_country)
        assert out["A"] == [(3600, 15.0), (7200, 28.0)]

    def test_separates_countries(self):
        from zbbx_mcp.anomaly import aggregate_hourly_by_country
        host_series = {"1": [(3600, 10.0)], "2": [(3600, 7.0)]}
        host_country = {"1": "A", "2": "B"}
        out = aggregate_hourly_by_country(host_series, host_country)
        assert out["A"] == [(3600, 10.0)] and out["B"] == [(3600, 7.0)]

    def test_host_without_country_dropped(self):
        from zbbx_mcp.anomaly import aggregate_hourly_by_country
        host_series = {"1": [(3600, 10.0)], "2": [(3600, 99.0)]}
        host_country = {"1": "A"}  # host 2 has no country
        out = aggregate_hourly_by_country(host_series, host_country)
        assert out == {"A": [(3600, 10.0)]}

    def test_result_sorted_by_clock(self):
        from zbbx_mcp.anomaly import aggregate_hourly_by_country
        host_series = {"1": [(7200, 2.0), (3600, 1.0)]}
        out = aggregate_hourly_by_country(host_series, {"1": "A"})
        assert [c for c, _ in out["A"]] == [3600, 7200]

    def test_empty(self):
        from zbbx_mcp.anomaly import aggregate_hourly_by_country
        assert aggregate_hourly_by_country({}, {}) == {}


class TestRecentBaselineFromDaily:
    """Pure-helper tests for recent_baseline_from_daily (#153, ADR 047)."""

    def test_splits_recent_vs_baseline(self):
        from zbbx_mcp.anomaly import recent_baseline_from_daily
        daily = {
            "2026-06-01": 100.0, "2026-06-02": 100.0,
            "2026-06-03": 100.0, "2026-06-04": 50.0, "2026-06-05": 50.0,
        }
        r, b = recent_baseline_from_daily(daily, recent_days=2)
        assert r == 50.0   # mean of last 2 days
        assert b == 100.0  # mean of the earlier 3

    def test_lexical_date_order_not_dict_order(self):
        from zbbx_mcp.anomaly import recent_baseline_from_daily
        # insertion order scrambled — must sort by date key
        daily = {"2026-06-05": 50.0, "2026-06-01": 100.0,
                 "2026-06-03": 100.0, "2026-06-02": 100.0, "2026-06-04": 50.0}
        r, b = recent_baseline_from_daily(daily, recent_days=2)
        assert r == 50.0 and b == 100.0

    def test_too_few_days_returns_none(self):
        from zbbx_mcp.anomaly import recent_baseline_from_daily
        # recent_days=2 needs >= 3 days
        assert recent_baseline_from_daily({"a": 1.0, "b": 2.0}, recent_days=2) == (None, None)

    def test_empty_returns_none(self):
        from zbbx_mcp.anomaly import recent_baseline_from_daily
        assert recent_baseline_from_daily({}, recent_days=2) == (None, None)

    def test_malformed_values_return_none(self):
        from zbbx_mcp.anomaly import recent_baseline_from_daily
        daily = {"a": "x", "b": "y", "c": "z", "d": "w"}
        assert recent_baseline_from_daily(daily, recent_days=2) == (None, None)

    def test_diurnal_safe_no_false_drop_on_stable_days(self):
        from zbbx_mcp.anomaly import recent_baseline_from_daily
        # stable daily averages → recent ~ baseline → no drop
        daily = {f"2026-06-0{i}": 80.0 for i in range(1, 6)}
        r, b = recent_baseline_from_daily(daily, recent_days=2)
        assert r == 80.0 and b == 80.0
