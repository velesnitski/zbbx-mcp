"""Outage correlation, cluster, and host-flood tests (split from test_analytics, ADR 074)."""



class TestIdleRelayDetection:
    """Pure-helper tests for get_idle_relays bucket+filter logic."""

    def _phys(self) -> frozenset[str]:
        return frozenset({"net.if.in[eth0]", "net.if.in[eno1]"})

    def test_split_buckets_physical_vs_tunnel(self):
        from zbbx_mcp.tools.correlation import _split_iface_metrics

        items = [
            {"hostid": "h1", "key_": "net.if.in[eth0]", "lastvalue": "20000"},
            {"hostid": "h1", "key_": "net.if.in[tun0]", "lastvalue": "0"},
            {"hostid": "h1", "key_": "net.if.in[gre1]", "lastvalue": "0"},
            {"hostid": "h1", "key_": "net.if.in[lo]", "lastvalue": "999"},  # ignored
        ]
        per_host = _split_iface_metrics(items, [], self._phys())
        assert per_host["h1"]["physical_bps"] == 20000
        assert per_host["h1"]["tunnel_bps"] == 0
        assert per_host["h1"]["tunnel_count"] == 2
        assert sorted(per_host["h1"]["tunnel_names"]) == ["gre1", "tun0"]

    def test_split_skips_docker_bridges(self):
        from zbbx_mcp.tools.correlation import _split_iface_metrics

        items = [
            {"hostid": "h1", "key_": "net.if.in[docker0]", "lastvalue": "1"},
            {"hostid": "h1", "key_": "net.if.in[br-abc]", "lastvalue": "1"},
        ]
        per_host = _split_iface_metrics(items, [], self._phys())
        assert per_host == {}

    def test_split_handles_garbage_values(self):
        from zbbx_mcp.tools.correlation import _split_iface_metrics

        items = [
            {"hostid": "h1", "key_": "net.if.in[eth0]", "lastvalue": ""},
            {"hostid": "h1", "key_": "net.if.in[tun0]", "lastvalue": None},
            {"hostid": "h1", "key_": "not-a-net-key", "lastvalue": "5"},
            {"hostid": "h1", "key_": "net.if.in[", "lastvalue": "5"},  # malformed
        ]
        per_host = _split_iface_metrics(items, [], self._phys())
        # eth0 zero is still a recorded physical, no tunnel flagged
        assert per_host["h1"]["physical_bps"] == 0.0
        assert per_host["h1"]["tunnel_bps"] == 0.0
        assert per_host["h1"]["tunnel_count"] == 1
        assert per_host["h1"]["tunnel_names"] == ["tun0"]

    def test_idle_relay_flagged_when_tunnels_silent(self):
        from zbbx_mcp.tools.correlation import _find_idle_relays

        per_host = {
            "h1": {
                "physical_bps": 200_000,
                "physical_out_bps": 0,  # receives but doesn't forward → flagged
                "tunnel_bps": 0,
                "tunnel_count": 3,
                "tunnel_names": ["tun0", "tun1", "tun2"],
            },
        }
        idle = _find_idle_relays(per_host, min_mgmt_kbps=100)
        assert len(idle) == 1
        hid, in_kbps, out_kbps, tun_count, sample = idle[0]
        assert hid == "h1"
        assert in_kbps == 200.0
        assert out_kbps == 0.0
        assert tun_count == 3
        assert sample == ["tun0", "tun1", "tun2"]

    def test_idle_relay_skipped_when_forwarding_healthy(self):
        # NAT-mode relay: physical out ≈ in (forwards) with idle tunnels by
        # design — must NOT be flagged (the out<<in gate, ADR 043).
        from zbbx_mcp.tools.correlation import _find_idle_relays

        per_host = {
            "h1": {
                "physical_bps": 200_000,
                "physical_out_bps": 190_000,  # out ≈ in → healthy forwarder
                "tunnel_bps": 0,
                "tunnel_count": 3,
                "tunnel_names": ["tun0", "tun1", "tun2"],
            },
        }
        assert _find_idle_relays(per_host, min_mgmt_kbps=100) == []

    def test_idle_relay_skipped_when_tunnels_have_traffic(self):
        from zbbx_mcp.tools.correlation import _find_idle_relays

        per_host = {
            "h1": {
                "physical_bps": 200_000,
                "tunnel_bps": 10,  # one tunnel forwarding
                "tunnel_count": 2,
                "tunnel_names": ["tun0", "tun1"],
            },
        }
        assert _find_idle_relays(per_host, 100) == []

    def test_idle_relay_skipped_below_mgmt_floor(self):
        from zbbx_mcp.tools.correlation import _find_idle_relays

        per_host = {
            "h1": {
                "physical_bps": 50_000,  # 50 kbps, below 100
                "tunnel_bps": 0,
                "tunnel_count": 2,
                "tunnel_names": ["tun0", "tun1"],
            },
        }
        assert _find_idle_relays(per_host, 100) == []

    def test_idle_relay_skipped_when_no_tunnels(self):
        from zbbx_mcp.tools.correlation import _find_idle_relays

        per_host = {
            "h1": {
                "physical_bps": 200_000,
                "tunnel_bps": 0,
                "tunnel_count": 0,
                "tunnel_names": [],
            },
        }
        assert _find_idle_relays(per_host, 100) == []

    def test_idle_relays_sorted_by_mgmt_traffic_desc(self):
        from zbbx_mcp.tools.correlation import _find_idle_relays

        per_host = {
            "h1": {"physical_bps": 100_000, "physical_out_bps": 0, "tunnel_bps": 0, "tunnel_count": 1, "tunnel_names": ["tun0"]},
            "h2": {"physical_bps": 500_000, "physical_out_bps": 0, "tunnel_bps": 0, "tunnel_count": 1, "tunnel_names": ["tun0"]},
            "h3": {"physical_bps": 250_000, "physical_out_bps": 0, "tunnel_bps": 0, "tunnel_count": 1, "tunnel_names": ["tun0"]},
        }
        out = _find_idle_relays(per_host, 50)
        assert [r[0] for r in out] == ["h2", "h3", "h1"]

class TestOutageClustering:
    """Pure-helper tests for get_outage_clusters time-window grouping."""

    def _rec(self, clock: int, hostid: str, key: str, name: str = "Down", sev: int = 4) -> dict:
        return {
            "clock": clock,
            "hostid": hostid,
            "host": f"host-{hostid}",
            "name": name,
            "severity": sev,
            "key": key,
        }

    def test_subnet_helper(self):
        from zbbx_mcp.tools.correlation import subnet24

        assert subnet24("10.0.5.42") == "10.0.5.0/24"
        assert subnet24("") == ""
        assert subnet24("not-an-ip") == ""
        assert subnet24("1.2.3") == ""
        assert subnet24("::1") == ""

    def test_three_hosts_same_subnet_within_window_form_cluster(self):
        from zbbx_mcp.tools.correlation import _cluster_problems

        records = [
            self._rec(1000, "h1", "10.0.0.0/24"),
            self._rec(1100, "h2", "10.0.0.0/24"),
            self._rec(1200, "h3", "10.0.0.0/24"),
        ]
        clusters = _cluster_problems(records, window_sec=600, min_hosts=3)
        assert len(clusters) == 1
        c = clusters[0]
        assert c["host_count"] == 3
        assert c["events"] == 3
        assert c["start"] == 1000
        assert c["end"] == 1200

    def test_below_min_hosts_does_not_cluster(self):
        from zbbx_mcp.tools.correlation import _cluster_problems

        records = [
            self._rec(1000, "h1", "10.0.0.0/24"),
            self._rec(1100, "h2", "10.0.0.0/24"),
        ]
        assert _cluster_problems(records, 600, 3) == []

    def test_outside_window_does_not_cluster(self):
        from zbbx_mcp.tools.correlation import _cluster_problems

        records = [
            self._rec(1000, "h1", "10.0.0.0/24"),
            self._rec(1500, "h2", "10.0.0.0/24"),
            self._rec(2200, "h3", "10.0.0.0/24"),  # outside 600s of h1
        ]
        # Greedy run grows h1..h2 (500s), h3 starts new run with only 1 host
        assert _cluster_problems(records, 600, 3) == []

    def test_two_separate_subnets_yield_two_clusters(self):
        from zbbx_mcp.tools.correlation import _cluster_problems

        records = [
            self._rec(1000, "h1", "10.0.0.0/24"),
            self._rec(1100, "h2", "10.0.0.0/24"),
            self._rec(1200, "h3", "10.0.0.0/24"),
            self._rec(2000, "h4", "10.0.1.0/24"),
            self._rec(2100, "h5", "10.0.1.0/24"),
            self._rec(2200, "h6", "10.0.1.0/24"),
        ]
        clusters = _cluster_problems(records, 600, 3)
        assert len(clusters) == 2
        assert {c["key"] for c in clusters} == {"10.0.0.0/24", "10.0.1.0/24"}

    def test_max_severity_propagates(self):
        from zbbx_mcp.tools.correlation import _cluster_problems

        records = [
            self._rec(1000, "h1", "10.0.0.0/24", sev=2),
            self._rec(1100, "h2", "10.0.0.0/24", sev=5),
            self._rec(1200, "h3", "10.0.0.0/24", sev=3),
        ]
        clusters = _cluster_problems(records, 600, 3)
        assert clusters[0]["max_severity"] == 5

    def test_duplicate_hostids_count_once(self):
        from zbbx_mcp.tools.correlation import _cluster_problems

        records = [
            self._rec(1000, "h1", "10.0.0.0/24", name="A"),
            self._rec(1100, "h1", "10.0.0.0/24", name="B"),
            self._rec(1200, "h2", "10.0.0.0/24"),
        ]
        # 3 events but only 2 distinct hosts — does not meet min_hosts=3
        assert _cluster_problems(records, 600, 3) == []
        # min_hosts=2 should pass
        clusters = _cluster_problems(records, 600, 2)
        assert clusters[0]["host_count"] == 2
        assert clusters[0]["events"] == 3

    def test_clusters_sorted_by_host_count_then_severity(self):
        from zbbx_mcp.tools.correlation import _cluster_problems

        records = [
            # Big cluster, low severity
            self._rec(1000, "h1", "A", sev=2),
            self._rec(1050, "h2", "A", sev=2),
            self._rec(1100, "h3", "A", sev=2),
            self._rec(1150, "h4", "A", sev=2),
            # Smaller cluster, high severity
            self._rec(1000, "h5", "B", sev=5),
            self._rec(1050, "h6", "B", sev=5),
            self._rec(1100, "h7", "B", sev=5),
        ]
        clusters = _cluster_problems(records, 600, 3)
        assert [c["key"] for c in clusters] == ["A", "B"]  # bigger first

class TestOutageClusterGroupingV2:
    """Pure-helper tests for multi-level cluster grouping (#119)."""

    def testsubnet24_and_subnet16(self):
        from zbbx_mcp.tools.correlation import _group_key

        assert _group_key("subnet24", ip="10.20.30.40") == "10.20.30.0/24"
        assert _group_key("subnet16", ip="10.20.30.40") == "10.20.0.0/16"
        assert _group_key("subnet24", ip="") == ""
        assert _group_key("subnet16", ip="not-an-ip") == ""

    def test_provider_level_skips_unknown(self):
        from zbbx_mcp.tools.correlation import _group_key

        assert _group_key("provider", provider="OVH") == "OVH"
        # 'Other'/'Unknown' would lump unrelated hosts — must be empty key.
        assert _group_key("provider", provider="Other") == ""
        assert _group_key("provider", provider="Unknown") == ""
        assert _group_key("provider", provider="") == ""

    def test_hostgroup_level(self):
        from zbbx_mcp.tools.correlation import _group_key

        assert _group_key("hostgroup", hostgroup="EU/edge") == "EU/edge"
        assert _group_key("hostgroup", hostgroup="") == ""

    def test_unknown_level_is_empty(self):
        from zbbx_mcp.tools.correlation import _group_key

        assert _group_key("subnet8", ip="1.2.3.4") == ""

    def test_auto_levels_constant_is_narrowest_first(self):
        from zbbx_mcp.tools.correlation import _AUTO_LEVELS

        assert _AUTO_LEVELS == ("subnet24", "subnet16", "provider")

class TestProblemNameNormalization:
    """Pure-helper tests for normalize_problem_name (#127)."""

    def test_strips_on_hostname(self):
        from zbbx_mcp.formatters import normalize_problem_name

        assert normalize_problem_name(
            "ServiceX: on host-a-1 error", "host-a-1",
        ) == "ServiceX: error"

    def test_subhost_form_preferred_over_parent(self):
        from zbbx_mcp.formatters import normalize_problem_name

        # Hostname is "parent child"; the trigger names the sub-host. We must
        # strip the sub-host form, not "child" only.
        result = normalize_problem_name(
            "ServiceX: on parent child error", "parent child",
        )
        assert result == "ServiceX: error"

    def test_collapses_internal_whitespace(self):
        from zbbx_mcp.formatters import normalize_problem_name

        # After stripping, multiple spaces around the cut point collapse to one.
        result = normalize_problem_name("CPU on host-a is overloaded", "host-a")
        assert result == "CPU is overloaded"

    def test_no_match_when_hostname_absent(self):
        from zbbx_mcp.formatters import normalize_problem_name

        # "on" is in the trigger but not paired with the hostname.
        result = normalize_problem_name(
            "Listener on port 8080 is down", "host-a",
        )
        assert result == "Listener on port 8080 is down"

    def test_returns_input_when_hostname_missing(self):
        from zbbx_mcp.formatters import normalize_problem_name

        assert normalize_problem_name("Anything", "") == "Anything"
        assert normalize_problem_name("", "host-a") == ""

    def test_two_hosts_normalize_to_same_name(self):
        from zbbx_mcp.formatters import normalize_problem_name

        # The whole point: triggers that differ only by embedded host should
        # collapse to a single dedup key.
        a = normalize_problem_name("ServiceY: on host-a error", "host-a")
        b = normalize_problem_name("ServiceY: on host-b error", "host-b")
        assert a == b == "ServiceY: error"

    def test_case_insensitive_on_keyword(self):
        from zbbx_mcp.formatters import normalize_problem_name

        # Some triggers capitalise differently — match regardless of case.
        assert normalize_problem_name(
            "Boot ON host-a failed", "host-a",
        ) == "Boot failed"

class TestHostFloodGrouping:
    """Pure-helper tests for _group_host_floods (#128)."""

    def _rec(self, hostid, host, name="Trigger", severity=4, clock=1000):
        return {
            "hostid": hostid,
            "host": host,
            "name": name,
            "severity": severity,
            "clock": clock,
        }

    def test_threshold_filters_below_min(self):
        from zbbx_mcp.tools.floods import _group_host_floods

        records = [
            self._rec("h1", "host-a", name="A"),
            self._rec("h1", "host-a", name="B"),
        ]
        # 2 problems on one host, min_problems=5 → no flood.
        assert _group_host_floods(records, {}, min_problems=5) == []

    def test_flood_emitted_when_count_meets_threshold(self):
        from zbbx_mcp.tools.floods import _group_host_floods

        records = [
            self._rec("h1", "host-a", name=f"T-{i}", severity=2 + (i % 3))
            for i in range(5)
        ]
        result = _group_host_floods(records, {}, min_problems=5)
        assert len(result) == 1
        assert result[0]["host"] == "host-a"
        assert result[0]["problem_count"] == 5
        assert result[0]["max_severity"] == 4

    def test_subhost_merged_into_parent(self):
        from zbbx_mcp.tools.floods import _group_host_floods

        # Three problems on parent, two on its child — counts as one flood of 5.
        records = [
            self._rec("p1", "parent", name="A"),
            self._rec("p1", "parent", name="B"),
            self._rec("p1", "parent", name="C"),
            self._rec("c1", "parent child1", name="D"),
            self._rec("c1", "parent child1", name="E"),
        ]
        parent_map = {"c1": "p1"}
        result = _group_host_floods(records, parent_map, min_problems=5)
        assert len(result) == 1
        assert result[0]["hostid"] == "p1"
        assert result[0]["host"] == "parent"
        assert result[0]["problem_count"] == 5
        assert result[0]["child_count"] == 1

    def test_earliest_clock_picked(self):
        from zbbx_mcp.tools.floods import _group_host_floods

        records = [
            self._rec("h1", "host-a", clock=2000),
            self._rec("h1", "host-a", clock=1000),
            self._rec("h1", "host-a", clock=1500),
            self._rec("h1", "host-a", clock=2500),
            self._rec("h1", "host-a", clock=3000),
        ]
        result = _group_host_floods(records, {}, min_problems=5)
        assert result[0]["earliest_clock"] == 1000

    def test_sample_triggers_dedup_and_cap(self):
        from zbbx_mcp.tools.floods import _group_host_floods

        records = [
            self._rec("h1", "host-a", name="DupTrigger") for _ in range(7)
        ] + [
            self._rec("h1", "host-a", name="UniqueTrigger") for _ in range(3)
        ]
        result = _group_host_floods(records, {}, min_problems=5)
        # Sample is set-deduplicated, capped at 5 entries.
        assert len(result[0]["sample_triggers"]) == 2
        assert set(result[0]["sample_triggers"]) == {"DupTrigger", "UniqueTrigger"}

    def test_floods_sorted_by_count_then_severity(self):
        from zbbx_mcp.tools.floods import _group_host_floods

        records = (
            [self._rec("h1", "small", severity=5) for _ in range(5)]
            + [self._rec("h2", "big", severity=2) for _ in range(8)]
        )
        result = _group_host_floods(records, {}, min_problems=5)
        # Bigger flood comes first regardless of severity.
        assert result[0]["host"] == "big"
        assert result[1]["host"] == "small"

class TestFormatAge:
    """Pure-helper tests for the compact age renderer (#136)."""

    def test_seconds_under_a_minute(self):
        from zbbx_mcp.formatters import format_age

        assert format_age(0) == "0s"
        assert format_age(45) == "45s"
        assert format_age(59) == "59s"

    def test_minutes(self):
        from zbbx_mcp.formatters import format_age

        assert format_age(60) == "1m"
        assert format_age(150) == "2m"
        assert format_age(3599) == "59m"

    def test_hours(self):
        from zbbx_mcp.formatters import format_age

        assert format_age(3600) == "1h"
        assert format_age(7200) == "2h"
        assert format_age(86399) == "23h"

    def test_days(self):
        from zbbx_mcp.formatters import format_age

        assert format_age(86400) == "1d"
        assert format_age(7 * 86400) == "7d"
        assert format_age(180 * 86400) == "180d"

    def test_negative_clamped_to_zero(self):
        from zbbx_mcp.formatters import format_age

        assert format_age(-5) == "0s"
        assert format_age(-1_000_000) == "0s"

class TestSubnetMatcher:
    """Pure-helper tests for diagnose_subnet (#149)."""

    def test_slash_24_match(self):
        from zbbx_mcp.tools.diagnose import _ip_matches_subnet
        assert _ip_matches_subnet("10.1.2.5", "10.1.2.0/24") is True

    def test_slash_24_no_match(self):
        from zbbx_mcp.tools.diagnose import _ip_matches_subnet
        assert _ip_matches_subnet("10.1.3.5", "10.1.2.0/24") is False

    def test_slash_16_match(self):
        from zbbx_mcp.tools.diagnose import _ip_matches_subnet
        assert _ip_matches_subnet("10.1.42.99", "10.1.0.0/16") is True

    def test_slash_16_no_match(self):
        from zbbx_mcp.tools.diagnose import _ip_matches_subnet
        assert _ip_matches_subnet("10.2.42.99", "10.1.0.0/16") is False

    def test_dotted_prefix_match(self):
        from zbbx_mcp.tools.diagnose import _ip_matches_subnet
        assert _ip_matches_subnet("10.1.2.5", "10.1.2") is True
        assert _ip_matches_subnet("10.1.2.5", "10.1.2.") is True

    def test_dotted_prefix_no_match(self):
        from zbbx_mcp.tools.diagnose import _ip_matches_subnet
        assert _ip_matches_subnet("10.1.20.5", "10.1.2") is False

    def test_empty_inputs(self):
        from zbbx_mcp.tools.diagnose import _ip_matches_subnet
        assert _ip_matches_subnet("", "10.1.2.0/24") is False
        assert _ip_matches_subnet("10.1.2.5", "") is False

    def test_unsupported_cidr_bits(self):
        from zbbx_mcp.tools.diagnose import _ip_matches_subnet
        # /28 etc. are not supported — return False (safer than wrong match)
        assert _ip_matches_subnet("10.1.2.5", "10.1.2.0/28") is False

    def test_malformed_cidr_does_not_crash(self):
        from zbbx_mcp.tools.diagnose import _ip_matches_subnet
        assert _ip_matches_subnet("10.1.2.5", "/24") is False
        assert _ip_matches_subnet("10.1.2.5", "garbage/24") is False
        assert _ip_matches_subnet("10.1.2.5", "10.1.2.0/abc") is False

class TestClusterCanonicalDedupe:
    """Pure-helper tests for _cluster_problems canonical-host fold (ADR 033)."""

    def _record(self, hostid: str, host: str, clock: int = 100, key: str = "k"):
        return {
            "clock": clock, "hostid": hostid, "host": host,
            "name": "Service down", "severity": 4, "key": key,
        }

    def test_one_parent_plus_subhosts_does_not_pass_threshold(self):
        from zbbx_mcp.tools.correlation import _cluster_problems
        # Single physical machine with three VIPs. Naming: parent + " " + suffix.
        # Pre-fold this would pass min_hosts=3; post-fold it must not (canonical=1).
        records = [
            self._record("1", "parent01", clock=100),
            self._record("2", "parent01 v1", clock=101),
            self._record("3", "parent01 v2", clock=102),
            self._record("4", "parent01 v3", clock=103),
        ]
        clusters = _cluster_problems(records, window_sec=60, min_hosts=3)
        assert clusters == []

    def test_three_distinct_hosts_still_form_cluster(self):
        from zbbx_mcp.tools.correlation import _cluster_problems
        # No sub-hosts; the threshold should still fire normally.
        records = [
            self._record("1", "host-a", clock=100),
            self._record("2", "host-b", clock=101),
            self._record("3", "host-c", clock=102),
        ]
        clusters = _cluster_problems(records, window_sec=60, min_hosts=3)
        assert len(clusters) == 1
        assert clusters[0]["host_count"] == 3
        assert clusters[0]["hosts"] == ["host-a", "host-b", "host-c"]

    def test_mixed_parents_and_subhosts(self):
        from zbbx_mcp.tools.correlation import _cluster_problems
        # Two distinct hosts plus one parent-with-two-subs.
        # Canonical count = 3; threshold should fire.
        records = [
            self._record("1", "host-a", clock=100),
            self._record("2", "host-b", clock=101),
            self._record("3", "parent01", clock=102),
            self._record("4", "parent01 v1", clock=103),
            self._record("5", "parent01 v2", clock=104),
        ]
        clusters = _cluster_problems(records, window_sec=60, min_hosts=3)
        assert len(clusters) == 1
        # The hosts list shows canonical names (parent appears once, not three times)
        assert clusters[0]["host_count"] == 3
        assert set(clusters[0]["hosts"]) == {"host-a", "host-b", "parent01"}

    def test_subhosts_only_without_parent_still_dedupe_to_canonical(self):
        from zbbx_mcp.tools.correlation import _cluster_problems
        # If only sub-hosts of one machine are in the bucket (parent record
        # not present), the canonical-name fold still collapses them.
        records = [
            self._record("1", "parent02 v1", clock=100),
            self._record("2", "parent02 v2", clock=101),
            self._record("3", "parent02 v3", clock=102),
            self._record("4", "parent02 v4", clock=103),
        ]
        clusters = _cluster_problems(records, window_sec=60, min_hosts=3)
        assert clusters == []  # 1 canonical host < 3

    def test_canonical_name_helper_passes_through_standalone(self):
        from zbbx_mcp.data import canonical_host_name
        assert canonical_host_name("host-a") == "host-a"

    def test_canonical_name_helper_strips_suffix(self):
        from zbbx_mcp.data import canonical_host_name
        assert canonical_host_name("parent01 v1") == "parent01"
