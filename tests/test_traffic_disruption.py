"""Traffic, disruption-wave, and shutdown-headroom tests (split from test_analytics, ADR 074)."""



class TestTrendSanity:
    """Test the trend/change consistency rules used in CEO report and geo tools.

    Rules:
    1. change < -10% and trend == "rising" → override to "stable"
    2. change > 0 and trend == "dropping" → override to "stable"
    3. current > avg * 1.5 and trend == "dropping" → override to "rising"
    4. current < 0.01 and avg > 0.05 → "dead"
    """

    @staticmethod
    def _apply_sanity(change: float, trend: str, traffic_gbps: float, avg_gbps: float) -> str:
        """Replicate the sanity logic from ceo_report.py / geo.py."""
        if change <= -30 and trend in ("stable", "rising"):
            trend = "dropping"
        elif change >= 30 and trend in ("stable", "dropping"):
            trend = "rising"
        elif change <= -10 and trend == "rising" or change > 0 and trend == "dropping":
            trend = "stable"
        if traffic_gbps < 0.01 and avg_gbps > 0.05:
            trend = "dead"
        return trend

    def test_rising_with_negative_change(self):
        """Rising trend but negative change should become stable."""
        result = self._apply_sanity(change=-13, trend="rising", traffic_gbps=22.0, avg_gbps=25.4)
        assert result == "stable", f"Expected stable, got {result}"

    def test_dropping_with_large_decline(self):
        """Legitimate large decline stays dropping."""
        result = self._apply_sanity(change=-87, trend="dropping", traffic_gbps=0.3, avg_gbps=2.2)
        assert result == "dropping"

    def test_rising_with_strong_growth(self):
        """Legitimate strong growth stays rising."""
        result = self._apply_sanity(change=123, trend="rising", traffic_gbps=26.3, avg_gbps=11.8)
        assert result == "rising"

    def test_dropping_positive_change_becomes_stable(self):
        """Small positive change with dropping trend becomes stable."""
        result = self._apply_sanity(change=11, trend="dropping", traffic_gbps=1.9, avg_gbps=1.7)
        assert result == "stable"

    def test_dropping_huge_current_becomes_rising(self):
        """Large positive change with dropping trend becomes rising."""
        result = self._apply_sanity(change=100, trend="dropping", traffic_gbps=4.0, avg_gbps=2.0)
        # change > 30 catches this → rising
        assert result == "rising"

    def test_dead_overrides_all(self):
        """Zero traffic with prior average triggers dead."""
        result = self._apply_sanity(change=-100, trend="dropping", traffic_gbps=0.0, avg_gbps=0.9)
        assert result == "dead"

    def test_moderate_decline_stays_stable(self):
        """Moderate decline within threshold stays stable."""
        result = self._apply_sanity(change=-21, trend="stable", traffic_gbps=20.3, avg_gbps=25.7)
        assert result == "stable"

    def test_stable_large_decline_becomes_dropping(self):
        """Large decline with stable trend becomes dropping."""
        result = self._apply_sanity(change=-47, trend="stable", traffic_gbps=0.8, avg_gbps=1.6)
        assert result == "dropping"

    def test_stable_significant_decline_becomes_dropping(self):
        """Significant decline with stable trend becomes dropping."""
        result = self._apply_sanity(change=-43, trend="stable", traffic_gbps=0.4, avg_gbps=0.7)
        assert result == "dropping"

    def test_stable_severe_decline_becomes_dropping(self):
        """Severe decline with stable trend becomes dropping."""
        result = self._apply_sanity(change=-70, trend="stable", traffic_gbps=1.1, avg_gbps=3.7)
        assert result == "dropping"

    def test_big_positive_change_stable_becomes_rising(self):
        """Large positive change overrides stable to rising."""
        result = self._apply_sanity(change=50, trend="stable", traffic_gbps=15.0, avg_gbps=10.0)
        assert result == "rising"

    def test_small_negative_change_keeps_rising(self):
        """Small decline within threshold keeps rising."""
        result = self._apply_sanity(change=-5, trend="rising", traffic_gbps=9.5, avg_gbps=10.0)
        assert result == "rising"

    def test_zero_traffic_zero_avg_stays_stable(self):
        """No traffic and no history stays stable."""
        result = self._apply_sanity(change=0, trend="stable", traffic_gbps=0.0, avg_gbps=0.0)
        assert result == "stable"

    def test_dropping_large_positive_becomes_rising(self):
        """Large positive change overrides dropping to rising."""
        result = self._apply_sanity(change=83, trend="dropping", traffic_gbps=4.1, avg_gbps=2.2)
        assert result == "rising"

    def test_dropping_moderate_positive_becomes_rising(self):
        """Moderate positive change overrides dropping to rising."""
        result = self._apply_sanity(change=36, trend="dropping", traffic_gbps=8.3, avg_gbps=6.1)
        assert result == "rising"

    def test_dropping_positive_15pct_becomes_stable(self):
        """Small positive change below threshold becomes stable."""
        result = self._apply_sanity(change=15, trend="dropping", traffic_gbps=1.15, avg_gbps=1.0)
        assert result == "stable"

    
    def test_exactly_minus_30_becomes_dropping(self):
        """Boundary: -30% exactly should trigger dropping (<=, not <)."""
        result = self._apply_sanity(change=-30, trend="stable", traffic_gbps=0.7, avg_gbps=1.0)
        assert result == "dropping", f"change=-30 stable should be dropping, got {result}"

    def test_minus_29_stays_stable(self):
        """Boundary: -29% should NOT trigger dropping."""
        result = self._apply_sanity(change=-29, trend="stable", traffic_gbps=0.71, avg_gbps=1.0)
        assert result == "stable"

    def test_exactly_plus_30_becomes_rising(self):
        """Boundary: +30% exactly should trigger rising (>=, not >)."""
        result = self._apply_sanity(change=30, trend="stable", traffic_gbps=1.3, avg_gbps=1.0)
        assert result == "rising", f"change=+30 stable should be rising, got {result}"

    def test_plus_29_stays_stable(self):
        """Boundary: +29% should NOT trigger rising."""
        result = self._apply_sanity(change=29, trend="stable", traffic_gbps=1.29, avg_gbps=1.0)
        assert result == "stable"

    def test_exactly_minus_10_rising_becomes_stable(self):
        """Boundary: -10% with rising should become stable."""
        result = self._apply_sanity(change=-10, trend="rising", traffic_gbps=0.9, avg_gbps=1.0)
        assert result == "stable"

    def test_minus_9_rising_stays_rising(self):
        """Boundary: -9% with rising should stay rising."""
        result = self._apply_sanity(change=-9, trend="rising", traffic_gbps=0.91, avg_gbps=1.0)
        assert result == "rising"

    def test_exactly_minus_30_rising_becomes_dropping(self):
        """Boundary: -30% with rising should become dropping (not stable)."""
        result = self._apply_sanity(change=-30, trend="rising", traffic_gbps=0.7, avg_gbps=1.0)
        assert result == "dropping"

    def test_exactly_plus_30_dropping_becomes_rising(self):
        """Boundary: +30% with dropping should become rising (not stable)."""
        result = self._apply_sanity(change=30, trend="dropping", traffic_gbps=1.3, avg_gbps=1.0)
        assert result == "rising"

class TestTrafficDropsSkipBreakdown:
    """Pure-helper tests for the no-baseline visibility footer."""

    def test_empty_when_nothing_skipped(self):
        from zbbx_mcp.tools.traffic import _format_skip_breakdown

        assert _format_skip_breakdown({"no_history": 0, "no_baseline_window": 0, "below_floor": 0}, 1.0) == ""

    def test_single_reason_renders(self):
        from zbbx_mcp.tools.traffic import _format_skip_breakdown

        out = _format_skip_breakdown({"no_history": 12, "no_baseline_window": 0, "below_floor": 0}, 1.0)
        assert out == "12 skipped: 12 no-history."

    def test_all_three_reasons_render_in_order(self):
        from zbbx_mcp.tools.traffic import _format_skip_breakdown

        out = _format_skip_breakdown(
            {"no_history": 5, "no_baseline_window": 3, "below_floor": 30}, 1.0,
        )
        assert out == "38 skipped: 5 no-history, 3 no-baseline-window, 30 below-1Mbps-floor."

    def test_floor_uses_min_baseline_arg(self):
        from zbbx_mcp.tools.traffic import _format_skip_breakdown

        out = _format_skip_breakdown(
            {"no_history": 0, "no_baseline_window": 0, "below_floor": 5}, 0.5,
        )
        assert "below-0.5Mbps-floor" in out

    def test_zero_categories_omitted(self):
        from zbbx_mcp.tools.traffic import _format_skip_breakdown

        out = _format_skip_breakdown(
            {"no_history": 0, "no_baseline_window": 7, "below_floor": 0}, 1.0,
        )
        # Other reasons should not appear
        assert "no-history" not in out
        assert "below-" not in out
        assert "7 no-baseline-window" in out

class TestShutdownPeerHeadroom:
    """Pure-helper tests for shutdown peer-cohort headroom logic."""

    def test_solo_when_no_peers(self):
        from zbbx_mcp.tools.trends_health import _compute_shutdown_safety

        label, headroom = _compute_shutdown_safety(50.0, [])
        assert label == "SOLO"
        assert headroom == 0.0

    def test_safe_when_cohort_headroom_covers_load_with_margin(self):
        from zbbx_mcp.tools.trends_health import _compute_shutdown_safety

        # 4 peers, each averaging 50 Mbps with peaks at 120 → 280 Mbps headroom
        peers = [{"peak": 120.0, "avg": 50.0}] * 4
        label, headroom = _compute_shutdown_safety(100.0, peers)
        assert label == "SAFE"
        assert headroom == 280.0  # 4 × (120 - 50)

    def test_risky_when_headroom_below_safety_margin(self):
        from zbbx_mcp.tools.trends_health import _compute_shutdown_safety

        # Candidate avg 100 Mbps × 1.5 margin = 150 Mbps required.
        # Peers offer 80 Mbps headroom — positive but insufficient.
        peers = [{"peak": 60.0, "avg": 20.0}, {"peak": 60.0, "avg": 20.0}]
        label, headroom = _compute_shutdown_safety(100.0, peers)
        assert label == "RISKY"
        assert headroom == 80.0

    def test_safety_margin_is_configurable(self):
        from zbbx_mcp.tools.trends_health import _compute_shutdown_safety

        peers = [{"peak": 100.0, "avg": 50.0}]  # 50 Mbps headroom
        # With margin 1.0, 50 Mbps headroom is exactly enough for 50 Mbps load
        assert _compute_shutdown_safety(50.0, peers, safety_margin=1.0)[0] == "SAFE"
        # With margin 1.5 (default), 50 Mbps load needs 75 Mbps headroom
        assert _compute_shutdown_safety(50.0, peers, safety_margin=1.5)[0] == "RISKY"

    def test_negative_spare_peers_do_not_subtract(self):
        from zbbx_mcp.tools.trends_health import _compute_shutdown_safety

        # A peer at peak < avg is impossible in real data but defensible:
        # such a peer should contribute zero, never negative headroom.
        peers = [{"peak": 100.0, "avg": 50.0}, {"peak": 10.0, "avg": 30.0}]
        label, headroom = _compute_shutdown_safety(20.0, peers)
        assert headroom == 50.0
        assert label == "SAFE"

    def test_peers_with_missing_metrics_are_dropped(self):
        from zbbx_mcp.tools.trends_health import _compute_shutdown_safety

        peers = [
            {"peak": None, "avg": None},  # no trend data — skip
            {"peak": 100.0, "avg": 30.0},  # 70 headroom
        ]
        label, headroom = _compute_shutdown_safety(40.0, peers)
        assert headroom == 70.0
        assert label == "SAFE"

    def test_candidate_without_traffic_returns_na(self):
        from zbbx_mcp.tools.trends_health import _compute_shutdown_safety

        peers = [{"peak": 100.0, "avg": 30.0}]
        label, headroom = _compute_shutdown_safety(None, peers)
        assert label == "N/A"
        assert headroom == 70.0  # still computed for the report

    def test_zero_load_candidate_is_safe(self):
        from zbbx_mcp.tools.trends_health import _compute_shutdown_safety

        # DEAD candidates with traffic_avg=0 — any peer headroom is enough.
        peers = [{"peak": 1.0, "avg": 0.5}]
        label, _ = _compute_shutdown_safety(0.0, peers)
        assert label == "SAFE"

class TestServicePortSplit:
    """Pure-helper tests for detect_service_port_split classification."""

    def test_split_label_when_service_alone_collapses(self):
        from zbbx_mcp.tools.disruption import _classify_service_split

        # Service: 100→20 (-80%), Mgmt: 50→48 (-4%)
        label, details = _classify_service_split(100.0, 20.0, 50.0, 48.0)
        assert label == "split"
        assert details["service_drop_pct"] == 80.0
        assert details["mgmt_drop_pct"] == 4.0

    def test_full_outage_label_when_both_collapse(self):
        from zbbx_mcp.tools.disruption import _classify_service_split

        label, _ = _classify_service_split(100.0, 20.0, 50.0, 5.0)  # both -80% / -90%
        assert label == "full-outage"

    def test_ok_when_neither_collapses(self):
        from zbbx_mcp.tools.disruption import _classify_service_split

        label, _ = _classify_service_split(100.0, 95.0, 50.0, 49.0)
        assert label == "ok"

    def test_na_when_baseline_missing(self):
        from zbbx_mcp.tools.disruption import _classify_service_split

        assert _classify_service_split(None, 20.0, 50.0, 48.0)[0] == "n/a"
        assert _classify_service_split(0.0, 20.0, 50.0, 48.0)[0] == "n/a"

    def test_thresholds_configurable(self):
        from zbbx_mcp.tools.disruption import _classify_service_split

        # 30% service drop: not flagged at default (50%), flagged when threshold lowered.
        assert _classify_service_split(100.0, 70.0, 50.0, 49.0)[0] == "ok"
        assert _classify_service_split(
            100.0, 70.0, 50.0, 49.0, service_drop_pct=20.0,
        )[0] == "split"

class TestRegionalLossClassification:
    """Pure-helper tests for detect_regional_traffic_loss."""

    def test_collapsed_when_one_region_drops_others_flat(self):
        from zbbx_mcp.tools.disruption import _classify_regional_loss

        # EU collapses 80%, NA stays flat (-2%).
        regions = {"EU": (1000.0, 200.0), "NA": (500.0, 490.0)}
        flagged = _classify_regional_loss(regions)
        assert len(flagged) == 1
        assert flagged[0]["region"] == "EU"
        assert flagged[0]["label"] == "collapsed"

    def test_solo_drop_when_no_flat_peer(self):
        from zbbx_mcp.tools.disruption import _classify_regional_loss

        # Both regions drop heavily — no peer is flat, so solo-drop label.
        regions = {"EU": (1000.0, 200.0), "NA": (500.0, 100.0)}
        flagged = _classify_regional_loss(regions)
        assert {r["region"] for r in flagged} == {"EU", "NA"}
        assert all(r["label"] == "solo-drop" for r in flagged)

    def test_below_threshold_not_flagged(self):
        from zbbx_mcp.tools.disruption import _classify_regional_loss

        regions = {"EU": (100.0, 80.0), "NA": (100.0, 95.0)}  # 20% / 5%
        assert _classify_regional_loss(regions) == []  # 20% < default 30% threshold

    def test_missing_data_skipped(self):
        from zbbx_mcp.tools.disruption import _classify_regional_loss

        regions = {"EU": (None, 200.0), "NA": (500.0, 50.0), "APAC": (300.0, 290.0)}
        flagged = _classify_regional_loss(regions)
        # APAC is flat (~3%), so NA gets 'collapsed'.
        assert len(flagged) == 1
        assert flagged[0]["region"] == "NA"

class TestDisruptionWaveDetection:
    """Pure-helper tests for the wave-clustering algorithm."""

    def _drop(self, clock, hostid, subnet, drop_pct=50.0):
        return {
            "clock": clock,
            "hostid": hostid,
            "host": f"h-{hostid}",
            "subnet": subnet,
            "hostgroup": "test",
            "drop_pct": drop_pct,
        }

    def test_wave_fires_when_thresholds_met(self):
        from zbbx_mcp.tools.disruption import _compute_waves

        drops = [
            self._drop(1000, "h1", "10.0.1.0/24"),
            self._drop(1100, "h2", "10.0.2.0/24"),
            self._drop(1200, "h3", "10.0.3.0/24"),
            self._drop(1300, "h4", "10.0.4.0/24"),
            self._drop(1400, "h5", "10.0.5.0/24"),
        ]
        waves = _compute_waves(drops, window_sec=3600, min_hosts=5, min_subnets=3)
        assert len(waves) == 1
        assert waves[0]["host_count"] == 5
        assert waves[0]["subnet_count"] == 5

    def test_wave_does_not_fire_when_subnets_collapse(self):
        from zbbx_mcp.tools.disruption import _compute_waves

        # 5 hosts, all in same /24 — fails min_subnets=3
        drops = [self._drop(1000 + i * 100, f"h{i}", "10.0.1.0/24") for i in range(5)]
        assert _compute_waves(drops, min_hosts=5, min_subnets=3) == []

    def test_window_boundary_excludes_late_arrivals(self):
        from zbbx_mcp.tools.disruption import _compute_waves

        drops = [
            self._drop(1000, "h1", "10.0.1.0/24"),
            self._drop(1100, "h2", "10.0.2.0/24"),
            self._drop(1200, "h3", "10.0.3.0/24"),
            self._drop(5000, "h4", "10.0.4.0/24"),  # outside 3600s window
            self._drop(5100, "h5", "10.0.5.0/24"),
        ]
        # First three meet min_subnets=3 but only 3 hosts; lower bar.
        waves = _compute_waves(drops, window_sec=3600, min_hosts=3, min_subnets=3)
        assert len(waves) == 1
        assert waves[0]["host_count"] == 3

    def test_severity_label_by_avg_drop(self):
        from zbbx_mcp.tools.disruption import _compute_waves

        drops = [
            self._drop(1000, "h1", "a", drop_pct=80),
            self._drop(1100, "h2", "b", drop_pct=80),
            self._drop(1200, "h3", "c", drop_pct=80),
        ]
        waves = _compute_waves(drops, min_hosts=3, min_subnets=3)
        assert waves[0]["severity"] == "critical"

        drops = [
            self._drop(1000, "h1", "a", drop_pct=40),
            self._drop(1100, "h2", "b", drop_pct=40),
            self._drop(1200, "h3", "c", drop_pct=40),
        ]
        waves = _compute_waves(drops, min_hosts=3, min_subnets=3)
        assert waves[0]["severity"] == "medium"

class TestPhysicalNicRegexFallback:
    """Pure-helper tests for #129 — NIC name regex fallback in _split_iface_metrics."""

    def test_unused_secondary_nic_classified_physical(self):
        from zbbx_mcp.tools.correlation import _split_iface_metrics

        # eno3 / enp130s0f0 are physical NICs not in the curated TRAFFIC_IN_KEYS
        # list. Without the regex they would fall into the tunnel bucket.
        items = [
            {"hostid": "h1", "key_": "net.if.in[eno3]", "lastvalue": "0"},
            {"hostid": "h1", "key_": "net.if.in[enp130s0f0]", "lastvalue": "0"},
            {"hostid": "h1", "key_": "net.if.in[tun0]", "lastvalue": "0"},
        ]
        per_host = _split_iface_metrics(items, [], frozenset())
        # Only tun0 should land in tunnel_names.
        assert per_host["h1"]["tunnel_count"] == 1
        assert per_host["h1"]["tunnel_names"] == ["tun0"]

    def test_usb_ethernet_enx_prefix(self):
        from zbbx_mcp.tools.correlation import _split_iface_metrics

        items = [
            {"hostid": "h1", "key_": "net.if.in[enx00aa11bb22cc]", "lastvalue": "0"},
            {"hostid": "h1", "key_": "net.if.in[gre1]", "lastvalue": "0"},
        ]
        per_host = _split_iface_metrics(items, [], frozenset())
        assert per_host["h1"]["tunnel_names"] == ["gre1"]

    def test_explicit_physical_keys_still_win(self):
        from zbbx_mcp.tools.correlation import _split_iface_metrics

        # Matching the curated key takes precedence; the regex is only a fallback.
        items = [
            {"hostid": "h1", "key_": "net.if.in[eth0]", "lastvalue": "100"},
        ]
        per_host = _split_iface_metrics(items, [], frozenset({"net.if.in[eth0]"}))
        assert per_host["h1"]["physical_bps"] == 100
        assert per_host["h1"]["tunnel_count"] == 0

    def test_unknown_prefix_still_treated_as_tunnel(self):
        from zbbx_mcp.tools.correlation import _split_iface_metrics

        items = [
            {"hostid": "h1", "key_": "net.if.in[gre1]", "lastvalue": "0"},
            {"hostid": "h1", "key_": "net.if.in[mytun0]", "lastvalue": "0"},
        ]
        per_host = _split_iface_metrics(items, [], frozenset())
        assert sorted(per_host["h1"]["tunnel_names"]) == ["gre1", "mytun0"]

class TestWaveCohesionGuard:
    """Pure-helper tests for the country-concentration check inside _compute_waves (#134)."""

    def _drop(self, clock, hostid, subnet, country, drop_pct=60.0):
        return {
            "clock": clock,
            "hostid": hostid,
            "host": f"h-{hostid}",
            "subnet": subnet,
            "hostgroup": "test",
            "country": country,
            "drop_pct": drop_pct,
        }

    def test_globally_spread_drops_are_filtered_out(self):
        from zbbx_mcp.tools.disruption import _compute_waves

        # 5 hosts, 5 different countries — top-country share = 1/5 = 20%, well below 40%.
        drops = [
            self._drop(1000 + 100 * i, f"h{i}", f"10.0.{i}.0/24", c)
            for i, c in enumerate(["DE", "AE", "MX", "GT", "ID"])
        ]
        waves = _compute_waves(drops, min_hosts=5, min_subnets=3)
        assert waves == []

    def test_concentrated_country_passes(self):
        from zbbx_mcp.tools.disruption import _compute_waves

        # 5 hosts, all in TR — concentration 100%.
        drops = [
            self._drop(1000 + 100 * i, f"h{i}", f"10.0.{i}.0/24", "TR")
            for i in range(5)
        ]
        waves = _compute_waves(drops, min_hosts=5, min_subnets=3)
        assert len(waves) == 1
        assert waves[0]["top_country"] == "TR"
        assert waves[0]["top_country_share"] == 1.0

    def test_partial_concentration_at_default_threshold(self):
        from zbbx_mcp.tools.disruption import _compute_waves

        # 6 hosts: 4 TR (67%) + 2 elsewhere — should pass at default 0.4.
        drops = [
            self._drop(1000, "h1", "10.0.1.0/24", "TR"),
            self._drop(1100, "h2", "10.0.2.0/24", "TR"),
            self._drop(1200, "h3", "10.0.3.0/24", "TR"),
            self._drop(1300, "h4", "10.0.4.0/24", "TR"),
            self._drop(1400, "h5", "10.0.5.0/24", "ID"),
            self._drop(1500, "h6", "10.0.6.0/24", "MX"),
        ]
        waves = _compute_waves(drops, min_hosts=5, min_subnets=3)
        assert len(waves) == 1
        assert waves[0]["top_country"] == "TR"
        assert abs(waves[0]["top_country_share"] - 4 / 6) < 1e-9

    def test_threshold_is_configurable(self):
        from zbbx_mcp.tools.disruption import _compute_waves

        # 3 TR / 2 ID = 60% top share at exactly min_hosts=5. Default 40%
        # passes; tighten to 90% and any sub-bucket that cohesion-passes
        # falls below min_hosts, so the whole cluster is rejected.
        drops = (
            [self._drop(1000 + 100 * i, f"a{i}", f"10.0.{i}.0/24", "TR") for i in range(3)]
            + [self._drop(1300 + 100 * i, f"b{i}", f"10.1.{i}.0/24", "ID") for i in range(2)]
        )
        assert len(_compute_waves(drops, min_hosts=5, min_subnets=3)) == 1
        assert _compute_waves(
            drops, min_hosts=5, min_subnets=3, min_country_concentration=0.9,
        ) == []

    def test_records_without_country_bypass_cohesion(self):
        from zbbx_mcp.tools.disruption import _compute_waves

        # No `country` field — backwards compat. Cohesion check is skipped.
        records = [
            {"clock": 1000 + 100 * i, "hostid": f"h{i}", "host": f"h-{i}",
             "subnet": f"10.0.{i}.0/24", "hostgroup": "test", "drop_pct": 60.0}
            for i in range(5)
        ]
        waves = _compute_waves(records, min_hosts=5, min_subnets=3)
        assert len(waves) == 1
        assert waves[0]["top_country"] == ""
        assert waves[0]["top_country_share"] == 1.0

class TestPeerRelativeDropFilter:
    """Pure-helper tests for _compute_peer_relative_drops (#135)."""

    def test_below_absolute_threshold_dropped(self):
        from zbbx_mcp.tools.disruption import _compute_peer_relative_drops

        records = [
            {"hostid": "h1", "drop_pct": 30.0, "cohort_key": "free:tier:tr"},
        ]
        out = _compute_peer_relative_drops(records, min_drop_pct=50.0)
        assert out == []

    def test_diurnal_cohort_dropped_uniformly_filtered_out(self):
        from zbbx_mcp.tools.disruption import _compute_peer_relative_drops

        # 5 hosts in same cohort all drop 60% — peer-relative ≈ 0 — filtered out.
        records = [
            {"hostid": f"h{i}", "drop_pct": 60.0, "cohort_key": "free:tier:tr"}
            for i in range(5)
        ]
        out = _compute_peer_relative_drops(records, min_drop_pct=50.0)
        assert out == []

    def test_genuinely_impacted_host_kept(self):
        from zbbx_mcp.tools.disruption import _compute_peer_relative_drops

        # h1 drops 80% while peers drop 10% — peer-relative ~70 > 20.
        records = [
            {"hostid": "h1", "drop_pct": 80.0, "cohort_key": "free:tier:tr"},
            {"hostid": "h2", "drop_pct": 10.0, "cohort_key": "free:tier:tr"},
            {"hostid": "h3", "drop_pct": 12.0, "cohort_key": "free:tier:tr"},
            {"hostid": "h4", "drop_pct": 8.0, "cohort_key": "free:tier:tr"},
        ]
        out = _compute_peer_relative_drops(records, min_drop_pct=50.0)
        assert len(out) == 1
        assert out[0]["hostid"] == "h1"
        assert out[0]["peer_relative_drop"] == 80.0 - 10.0  # cohort_drop = (10+12+8)/3 = 10

    def test_small_cohort_passes_absolute_only(self):
        from zbbx_mcp.tools.disruption import _compute_peer_relative_drops

        # 2 hosts in cohort (below default min_cohort_size=3) — peer gate skipped,
        # absolute gate fires alone.
        records = [
            {"hostid": "h1", "drop_pct": 80.0, "cohort_key": "x"},
            {"hostid": "h2", "drop_pct": 75.0, "cohort_key": "x"},
        ]
        out = _compute_peer_relative_drops(records, min_drop_pct=50.0)
        assert len(out) == 2
        assert all(r["cohort_drop"] is None for r in out)
        assert all(r["peer_relative_drop"] is None for r in out)
        assert all(r["cohort_size"] == 2 for r in out)

    def test_solo_cohort_passes_absolute_only(self):
        from zbbx_mcp.tools.disruption import _compute_peer_relative_drops

        records = [{"hostid": "h1", "drop_pct": 80.0, "cohort_key": "x"}]
        out = _compute_peer_relative_drops(records, min_drop_pct=50.0)
        assert len(out) == 1
        assert out[0]["cohort_drop"] is None
        assert out[0]["cohort_size"] == 1

    def test_min_relative_drop_threshold_configurable(self):
        from zbbx_mcp.tools.disruption import _compute_peer_relative_drops

        # h1 drops 60%, peers average 50% — peer-relative 10. Below default 20%, kept at 5%.
        records = [
            {"hostid": "h1", "drop_pct": 60.0, "cohort_key": "x"},
            {"hostid": "h2", "drop_pct": 50.0, "cohort_key": "x"},
            {"hostid": "h3", "drop_pct": 50.0, "cohort_key": "x"},
            {"hostid": "h4", "drop_pct": 50.0, "cohort_key": "x"},
        ]
        assert _compute_peer_relative_drops(records, min_drop_pct=50.0) == []
        kept = _compute_peer_relative_drops(
            records, min_drop_pct=50.0, min_peer_relative_drop=5.0,
        )
        # h1 passes (relative=10), peers fail (relative=-3.33 each).
        assert {r["hostid"] for r in kept} == {"h1"}

    def test_separate_cohorts_evaluated_independently(self):
        from zbbx_mcp.tools.disruption import _compute_peer_relative_drops

        records = [
            # cohort_a: all drop together — all rejected
            *[{"hostid": f"a{i}", "drop_pct": 70.0, "cohort_key": "a"} for i in range(4)],
            # cohort_b: one outlier — kept
            {"hostid": "b1", "drop_pct": 90.0, "cohort_key": "b"},
            {"hostid": "b2", "drop_pct": 5.0, "cohort_key": "b"},
            {"hostid": "b3", "drop_pct": 5.0, "cohort_key": "b"},
            {"hostid": "b4", "drop_pct": 5.0, "cohort_key": "b"},
        ]
        kept = _compute_peer_relative_drops(records, min_drop_pct=50.0)
        assert {r["hostid"] for r in kept} == {"b1"}

class TestShutdownCandidateMetricFold:
    """Sanity tests for the per-canonical metric aggregation pattern used
    in `get_shutdown_candidates` (ADR 037).

    cpu = MAX, traffic = SUM, service = WORST across a canonical group.
    """

    def _aggregate_group(self, hostids, metrics, service_map):
        cpus_avg = []
        traffics_avg = []
        services = []
        for hid in hostids:
            hm = metrics.get(hid, {})
            if hm.get("cpu") is not None:
                cpus_avg.append(hm["cpu"])
            if hm.get("traffic") is not None:
                traffics_avg.append(hm["traffic"])
            if hid in service_map:
                services.append(service_map[hid])
        cpu_avg = max(cpus_avg) if cpus_avg else None
        traffic_avg = sum(traffics_avg) if traffics_avg else None
        if 0 in services:
            service = "DOWN"
        elif -1 in services:
            service = "PARTIAL"
        elif 1 in services:
            service = "OK"
        else:
            service = ""
        return cpu_avg, traffic_avg, service

    def test_cpu_max_traffic_sum_service_worst(self):
        metrics = {
            "1": {"cpu": 5, "traffic": 0.1},
            "2": {"cpu": 75, "traffic": 50.0},
            "3": {"cpu": 3, "traffic": 0.5},
        }
        services = {"1": 1, "2": 0, "3": 1}
        cpu, traffic, service = self._aggregate_group(
            ["1", "2", "3"], metrics, services,
        )
        assert cpu == 75
        assert traffic == 50.6
        assert service == "DOWN"

    def test_all_idle_group_qualifies_as_dead(self):
        # Bug-fix case: parent + sub-hosts all idle → one DEAD candidate.
        metrics = {hid: {"cpu": 0.5, "traffic": 0.1} for hid in "12345"}
        services = {hid: 1 for hid in "12345"}
        cpu, traffic, service = self._aggregate_group(
            list("12345"), metrics, services,
        )
        assert cpu == 0.5
        assert traffic == 0.5
        assert traffic < 1.0 and cpu < 5.0

    def test_busy_subhost_rescues_parent_from_dead(self):
        # Parent's own metrics zero but sub-host very busy → group should
        # NOT qualify as DEAD (post-fold reality).
        metrics = {
            "parent": {"cpu": 0, "traffic": 0},
            "sub1": {"cpu": 80, "traffic": 200.0},
        }
        services = {"parent": 1, "sub1": 1}
        cpu, traffic, service = self._aggregate_group(
            ["parent", "sub1"], metrics, services,
        )
        assert cpu == 80
        assert traffic == 200.0
        assert not (traffic < 1.0 and cpu < 5.0)
        assert not (cpu > 50 and traffic < 1.0)
        assert service != "DOWN"

    def test_empty_metrics_returns_none(self):
        cpu, traffic, service = self._aggregate_group([], {}, {})
        assert cpu is None
        assert traffic is None
        assert service == ""

    def test_partial_service_loses_to_down(self):
        services = {"1": 1, "2": -1, "3": 0}
        _, _, service = self._aggregate_group(
            ["1", "2", "3"], {}, services,
        )
        assert service == "DOWN"

    def test_partial_service_wins_over_ok(self):
        services = {"1": 1, "2": -1, "3": 1}
        _, _, service = self._aggregate_group(
            ["1", "2", "3"], {}, services,
        )
        assert service == "PARTIAL"
