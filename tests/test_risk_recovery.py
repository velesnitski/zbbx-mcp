"""IP-history, loss-drift, at-risk, and recovery tests (split from test_analytics, ADR 074)."""



class TestExternalIpHistoryParsing:
    """Pure-helper tests for audit-log details parsing and recovery scoring."""

    def test_list_shape_picks_ip_updates(self):
        from zbbx_mcp.tools.ip_history import parse_ip_changes

        details = (
            '[["update", "interfaces.42.ip", "1.2.3.4", "5.6.7.8"],'
            ' ["update", "host.name", "old", "new"],'
            ' ["update", "interfaces.42.port", "10050", "10050"]]'
        )
        out = parse_ip_changes(details)
        assert out == [("1.2.3.4", "5.6.7.8")]

    def test_dict_shape_picks_ip_updates(self):
        from zbbx_mcp.tools.ip_history import parse_ip_changes

        details = '{"interfaces.7.ip": ["update", "10.0.0.1", "10.0.0.2"]}'
        assert parse_ip_changes(details) == [("10.0.0.1", "10.0.0.2")]

    def test_no_change_when_old_equals_new(self):
        from zbbx_mcp.tools.ip_history import parse_ip_changes

        # Renames that touch the field but leave the value equal must be skipped.
        details = '[["update", "interfaces.42.ip", "1.2.3.4", "1.2.3.4"]]'
        assert parse_ip_changes(details) == []

    def test_non_ip_field_ignored(self):
        from zbbx_mcp.tools.ip_history import parse_ip_changes

        details = '[["update", "host.host", "a", "b"]]'
        assert parse_ip_changes(details) == []

    def test_garbage_input_returns_empty(self):
        from zbbx_mcp.tools.ip_history import parse_ip_changes

        assert parse_ip_changes("") == []
        assert parse_ip_changes("not-json") == []
        assert parse_ip_changes("[1, 2, 3]") == []  # not the expected shape

    def test_recovery_scores(self):
        from zbbx_mcp.tools.ip_history import _score_recovery

        assert _score_recovery(100.0, 90.0) == "recovered"   # 0.9
        assert _score_recovery(100.0, 70.0) == "recovered"   # 0.7 boundary
        assert _score_recovery(100.0, 50.0) == "partial"     # 0.5
        assert _score_recovery(100.0, 30.0) == "partial"     # 0.3 boundary
        assert _score_recovery(100.0, 5.0) == "still-down"   # 0.05

    def test_recovery_na_cases(self):
        from zbbx_mcp.tools.ip_history import _score_recovery

        assert _score_recovery(None, 50.0) == "n/a"
        assert _score_recovery(50.0, None) == "n/a"
        assert _score_recovery(0.0, 50.0) == "n/a"  # divide-by-zero baseline

class TestLossDriftDetection:
    """Pure-helper tests for sliding-window loss/RTT classification."""

    def testsplit_baseline_recent_partitions_by_clock(self):
        from zbbx_mcp.tools.loss_drift import split_baseline_recent

        trends = [
            {"clock": 100, "value_avg": "1.0"},
            {"clock": 200, "value_avg": "2.0"},
            {"clock": 300, "value_avg": "10.0"},  # recent
            {"clock": 400, "value_avg": "12.0"},  # recent
        ]
        base, recent = split_baseline_recent(trends, cutoff_clock=300)
        assert base == 1.5  # (1+2)/2
        assert recent == 11.0  # (10+12)/2

    def test_split_handles_missing_sides(self):
        from zbbx_mcp.tools.loss_drift import split_baseline_recent

        # Only baseline records
        b, r = split_baseline_recent([{"clock": 100, "value_avg": "5"}], 300)
        assert b == 5.0 and r is None

        # Only recent records
        b, r = split_baseline_recent([{"clock": 400, "value_avg": "5"}], 300)
        assert b is None and r == 5.0

        # Empty
        assert split_baseline_recent([], 300) == (None, None)

    def test_split_skips_garbage_values(self):
        from zbbx_mcp.tools.loss_drift import split_baseline_recent

        trends = [
            {"clock": 100, "value_avg": "not-a-number"},
            {"clock": 200, "value_avg": "2.0"},
        ]
        base, _ = split_baseline_recent(trends, 300)
        assert base == 2.0

    def test_new_loss_takes_priority_over_loss_up(self):
        from zbbx_mcp.tools.loss_drift import compute_loss_drift

        # baseline ~0% loss, recent jumps to 8% — both flags fire, prefer new-loss.
        label, details = compute_loss_drift(0.5, 8.0, None, None)
        assert label == "new-loss"
        assert details["loss_delta"] == 7.5

    def test_loss_up_when_baseline_already_high(self):
        from zbbx_mcp.tools.loss_drift import compute_loss_drift

        label, _ = compute_loss_drift(3.0, 10.0, None, None)
        assert label == "loss-up"  # baseline >= 1%, so not 'new-loss'

    def test_rtt_up_alone(self):
        from zbbx_mcp.tools.loss_drift import compute_loss_drift

        label, details = compute_loss_drift(None, None, 50.0, 90.0)
        assert label == "rtt-up"
        assert details["rtt_delta_pct"] == 80.0

    def test_loss_and_rtt_combo(self):
        from zbbx_mcp.tools.loss_drift import compute_loss_drift

        # Loss baseline >= 1% so 'new-loss' does not preempt.
        label, _ = compute_loss_drift(2.0, 10.0, 50.0, 90.0)
        assert label == "loss-and-rtt"

    def test_below_thresholds_is_ok(self):
        from zbbx_mcp.tools.loss_drift import compute_loss_drift

        label, _ = compute_loss_drift(2.0, 4.0, 50.0, 60.0)  # +2% loss, +20% RTT
        assert label == "ok"

    def test_no_data_is_na(self):
        from zbbx_mcp.tools.loss_drift import compute_loss_drift

        label, _ = compute_loss_drift(None, None, None, None)
        assert label == "n/a"

    def test_thresholds_are_configurable(self):
        from zbbx_mcp.tools.loss_drift import compute_loss_drift

        # Default loss_step=5 → not flagged at +3.
        assert compute_loss_drift(2.0, 5.0, None, None)[0] == "ok"
        # Tighten to 2 → +3 flags.
        assert compute_loss_drift(2.0, 5.0, None, None, loss_step=2.0)[0] == "loss-up"

    def test_degraded_baseline_suppresses_false_rtt_drift(self):
        from zbbx_mcp.tools.loss_drift import compute_loss_drift

        # Baseline measured during an outage (47% loss); recent recovered to ~0%.
        # RTT "doubling" vs that unreliable baseline is recovery, not real drift.
        label, _ = compute_loss_drift(47.24, 0.09, 76.4, 142.5)
        assert label == "ok"

class TestAtRiskScoring:
    """Pure-helper tests for the composite at-risk score."""

    def test_zero_inputs_score_zero(self):
        from zbbx_mcp.tools.risk import _compute_risk_score

        score, details = _compute_risk_score(0, "ok", 0.0)
        assert score == 0.0
        assert details["drift_label"] == "ok"

    def test_more_peer_rotations_increase_score(self):
        from zbbx_mcp.tools.risk import _compute_risk_score

        low, _ = _compute_risk_score(1, "ok", 0.0)
        high, _ = _compute_risk_score(20, "ok", 0.0)
        assert high > low

    def test_drift_label_dominates_when_other_signals_zero(self):
        from zbbx_mcp.tools.risk import _compute_risk_score

        rtt, _ = _compute_risk_score(0, "rtt-up", 0.0)
        loss, _ = _compute_risk_score(0, "loss-up", 0.0)
        combo, _ = _compute_risk_score(0, "loss-and-rtt", 0.0)
        assert rtt < loss < combo

    def test_age_capped_at_90_days(self):
        from zbbx_mcp.tools.risk import _compute_risk_score

        cap, _ = _compute_risk_score(0, "ok", 90.0)
        bigger, _ = _compute_risk_score(0, "ok", 365.0)
        assert cap == bigger  # capped, so equal

    def test_none_age_treated_as_capped(self):
        from zbbx_mcp.tools.risk import _compute_risk_score

        # No prior rotation observed → treat as cap, not zero.
        cap, _ = _compute_risk_score(0, "ok", 90.0)
        none_score, _ = _compute_risk_score(0, "ok", None)
        assert cap == none_score

class TestBlastRadiusClassification:
    """Pure-helper tests for cohort connection-count delta labels."""

    def test_absorbing_when_post_gains_at_least_10pct(self):
        from zbbx_mcp.tools.risk import _compute_blast_radius

        label, delta = _compute_blast_radius(100.0, 120.0)
        assert label == "absorbing"
        assert delta == 20.0

    def test_draining_when_post_loses_more_than_10pct(self):
        from zbbx_mcp.tools.risk import _compute_blast_radius

        label, delta = _compute_blast_radius(100.0, 80.0)
        assert label == "draining"
        assert delta == -20.0

    def test_stable_within_10pct(self):
        from zbbx_mcp.tools.risk import _compute_blast_radius

        for post in (95.0, 100.0, 105.0):
            label, _ = _compute_blast_radius(100.0, post)
            assert label == "stable"

    def test_na_when_pre_missing_or_zero(self):
        from zbbx_mcp.tools.risk import _compute_blast_radius

        assert _compute_blast_radius(None, 50.0) == ("n/a", None)
        assert _compute_blast_radius(0.0, 50.0) == ("n/a", None)
        assert _compute_blast_radius(50.0, None) == ("n/a", None)

class TestRecoveryAggregate:
    """Pure-helper tests for fleet-level recovery KPI aggregation."""

    def test_aggregate_basic(self):
        from zbbx_mcp.tools.ip_history import _aggregate_recovery_scores

        rotations = [
            {"score": "recovered"},
            {"score": "recovered"},
            {"score": "recovered"},
            {"score": "partial"},
            {"score": "still-down"},
            {"score": "n/a"},
        ]
        agg = _aggregate_recovery_scores(rotations)
        assert agg["total"] == 6
        assert agg["recovered"] == 3
        assert agg["partial"] == 1
        assert agg["still_down"] == 1
        assert agg["na"] == 1
        # rate = 3 recovered / 5 determined-outcome = 60%
        assert agg["rate_pct"] == 60.0

    def test_aggregate_all_na_yields_none_rate(self):
        from zbbx_mcp.tools.ip_history import _aggregate_recovery_scores

        agg = _aggregate_recovery_scores([{"score": "n/a"}, {"score": "n/a"}])
        assert agg["total"] == 2
        assert agg["na"] == 2
        assert agg["rate_pct"] is None

    def test_aggregate_unknown_label_treated_as_na(self):
        from zbbx_mcp.tools.ip_history import _aggregate_recovery_scores

        agg = _aggregate_recovery_scores([{"score": "weird"}, {"score": "recovered"}])
        assert agg["na"] == 1
        assert agg["recovered"] == 1
        assert agg["rate_pct"] == 100.0  # 1 of 1 determined

    def test_aggregate_empty(self):
        from zbbx_mcp.tools.ip_history import _aggregate_recovery_scores

        agg = _aggregate_recovery_scores([])
        assert agg["total"] == 0
        assert agg["rate_pct"] is None
