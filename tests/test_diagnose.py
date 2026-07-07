"""diagnose_host / bulk_diagnose helper and threading tests (split from test_analytics, ADR 074)."""



class TestDiagnoseHostHelpers:
    """Pure-helper tests for diagnose_host (#2 composite)."""

    def test_classify_mode_server_via_traffic_keys(self):
        from zbbx_mcp.tools.diagnose import _classify_host_mode

        items = [{"key_": "net.if.in[eth0]"}, {"key_": "agent.ping"}]
        assert _classify_host_mode({}, items) == "server"

    def test_classify_mode_server_via_agent_ping_alone(self):
        from zbbx_mcp.tools.diagnose import _classify_host_mode

        items = [{"key_": "agent.ping"}]
        assert _classify_host_mode({}, items) == "server"

    def test_classify_mode_domain_when_no_agent_no_traffic(self):
        from zbbx_mcp.tools.diagnose import _classify_host_mode

        items = [{"key_": "webcheck.https.status"}]
        assert _classify_host_mode({}, items) == "domain"

    def test_classify_mode_domain_when_no_items(self):
        from zbbx_mcp.tools.diagnose import _classify_host_mode

        assert _classify_host_mode({}, []) == "domain"

    def test_verdict_traffic_lost_when_traffic_collapses_with_healthy_agent(self):
        from zbbx_mcp.tools.diagnose import _classify_verdict

        v, action = _classify_verdict(
            mode="server",
            agent_ping_val=1,
            agent_ping_age_min=0.5,
            traffic_baseline_mbps=200.0,
            traffic_recent_mbps=2.0,  # 1% of baseline
            open_problems=0,
            https_down=False,
            https_age_h=None,
        )
        assert v == "traffic_lost"
        assert "rotat" in action.lower() or "external" in action.lower()

    def test_verdict_down_when_agent_and_traffic_both_gone(self):
        from zbbx_mcp.tools.diagnose import _classify_verdict

        v, _ = _classify_verdict(
            mode="server",
            agent_ping_val=0,
            agent_ping_age_min=60.0,
            traffic_baseline_mbps=200.0,
            traffic_recent_mbps=0.5,
            open_problems=0,
            https_down=False,
            https_age_h=None,
        )
        assert v == "down"

    def test_verdict_degraded_agent_down_traffic_ok(self):
        from zbbx_mcp.tools.diagnose import _classify_verdict

        v, _ = _classify_verdict(
            mode="server",
            agent_ping_val=0,
            agent_ping_age_min=10.0,
            traffic_baseline_mbps=200.0,
            traffic_recent_mbps=180.0,
            open_problems=0,
            https_down=False,
            https_age_h=None,
        )
        assert v == "degraded"

    def test_verdict_degraded_when_open_problems(self):
        from zbbx_mcp.tools.diagnose import _classify_verdict

        v, action = _classify_verdict(
            mode="server",
            agent_ping_val=1,
            agent_ping_age_min=0.5,
            traffic_baseline_mbps=200.0,
            traffic_recent_mbps=180.0,
            open_problems=3,
            https_down=False,
            https_age_h=None,
        )
        assert v == "degraded"
        assert "3" in action

    def test_verdict_healthy_when_everything_ok(self):
        from zbbx_mcp.tools.diagnose import _classify_verdict

        v, _ = _classify_verdict(
            mode="server",
            agent_ping_val=1,
            agent_ping_age_min=0.5,
            traffic_baseline_mbps=200.0,
            traffic_recent_mbps=180.0,
            open_problems=0,
            https_down=False,
            https_age_h=None,
        )
        assert v == "healthy"

    def test_verdict_https_down_in_domain_mode(self):
        from zbbx_mcp.tools.diagnose import _classify_verdict

        v, action = _classify_verdict(
            mode="domain",
            agent_ping_val=None,
            agent_ping_age_min=None,
            traffic_baseline_mbps=None,
            traffic_recent_mbps=None,
            open_problems=2,
            https_down=True,
            https_age_h=17.5,
        )
        assert v == "https_down"
        assert "17" in action

    def test_verdict_domain_healthy_no_problems(self):
        from zbbx_mcp.tools.diagnose import _classify_verdict

        v, _ = _classify_verdict(
            mode="domain",
            agent_ping_val=None,
            agent_ping_age_min=None,
            traffic_baseline_mbps=None,
            traffic_recent_mbps=None,
            open_problems=0,
            https_down=False,
            https_age_h=None,
        )
        assert v == "healthy"

    def test_verdict_agent_age_5min_boundary_mutation_sentinel(self):
        """Mutation sentinel — pins the agent-age threshold's strict-greater semantics.

        ``_classify_verdict`` marks an agent unreachable when
        ``agent_ping_age_min > 5``. The 5-minute constant is a load-bearing
        threshold: it gates every server-mode diagnosis. This test fixes
        three boundary points around it so any off-by-one mutation
        (``> 5`` → ``>= 5``, ``> 4``, ``> 6``, etc.) shows up as a test
        failure rather than silently misclassifying healthy hosts as
        degraded on every run.

        Pairing each boundary point with healthy traffic + healthy
        agent.ping isolates the age check — the only path to a non-
        ``healthy`` verdict here goes through the age clause.
        """
        from zbbx_mcp.tools.diagnose import _classify_verdict

        common: dict = dict(
            mode="server",
            agent_ping_val=1,
            traffic_baseline_mbps=200.0,
            traffic_recent_mbps=180.0,
            open_problems=0,
            https_down=False,
            https_age_h=None,
        )

        # Just below the boundary — must stay healthy.
        v, _ = _classify_verdict(agent_ping_age_min=4.99, **common)
        assert v == "healthy", "4.99m must be healthy; catches `> 4` mutations"

        # Exactly at the boundary — must NOT flip (strict-greater).
        # The off-by-one trap: `>= 5` would mark every host at exactly
        # 5min ago as degraded — high-volume false positive.
        v, _ = _classify_verdict(agent_ping_age_min=5.00, **common)
        assert v == "healthy", "5.00m exactly must stay healthy; catches `>= 5` mutations"

        # Just above — must flip to degraded.
        v, _ = _classify_verdict(agent_ping_age_min=5.01, **common)
        assert v == "degraded", "5.01m must mark agent unreachable; catches `> 6` mutations"

    def test_verdict_traffic_below_5mbps_baseline_not_flagged_as_traffic_lost(self):
        from zbbx_mcp.tools.diagnose import _classify_verdict

        # A 0.5 Mbps -> 0.05 Mbps drop is technically 90%, but the baseline
        # is too small to count as a real signal; should not flip to traffic_lost.
        v, _ = _classify_verdict(
            mode="server",
            agent_ping_val=1,
            agent_ping_age_min=0.5,
            traffic_baseline_mbps=0.5,
            traffic_recent_mbps=0.05,
            open_problems=0,
            https_down=False,
            https_age_h=None,
        )
        assert v == "healthy"

class TestBulkDiagnoseHelpers:
    """Pure-helper tests for bulk_diagnose (#148)."""

    def _facts(self, **overrides):
        base = {
            "host": "h1", "verdict": "healthy", "mode": "server",
            "action": "no issues", "problems": [],
            "agent_ping_val": 1, "agent_ping_age_min": 0.5,
            "traffic_baseline_mbps": 200.0, "traffic_recent_mbps": 190.0,
            "https_down": False, "https_age_h": None,
        }
        base.update(overrides)
        return base

    def test_primary_signal_healthy(self):
        from zbbx_mcp.tools.diagnose import _verdict_primary_signal
        assert _verdict_primary_signal(self._facts()) == "OK"

    def test_primary_signal_down(self):
        from zbbx_mcp.tools.diagnose import _verdict_primary_signal
        f = self._facts(verdict="down")
        assert "agent" in _verdict_primary_signal(f).lower()

    def test_primary_signal_traffic_lost_shows_mbps(self):
        from zbbx_mcp.tools.diagnose import _verdict_primary_signal
        f = self._facts(
            verdict="traffic_lost",
            traffic_baseline_mbps=255.9, traffic_recent_mbps=2.3,
        )
        s = _verdict_primary_signal(f)
        assert "256" in s and "2.3" in s

    def test_primary_signal_https_down_shows_hours(self):
        from zbbx_mcp.tools.diagnose import _verdict_primary_signal
        f = self._facts(
            verdict="https_down", mode="domain",
            https_down=True, https_age_h=17.5,
        )
        assert "17" in _verdict_primary_signal(f)

    def test_primary_signal_degraded_with_problems(self):
        from zbbx_mcp.tools.diagnose import _verdict_primary_signal
        f = self._facts(
            verdict="degraded",
            problems=[{"name": "x"}, {"name": "y"}, {"name": "z"}],
        )
        assert "3" in _verdict_primary_signal(f)

    def test_render_bulk_table_empty(self):
        from zbbx_mcp.tools.diagnose import _render_bulk_table
        assert "No hosts" in _render_bulk_table([], 0)

    def test_render_bulk_table_sorts_by_severity(self):
        from zbbx_mcp.tools.diagnose import _render_bulk_table
        rows = [
            self._facts(host="ok-host", verdict="healthy"),
            self._facts(host="dead-host", verdict="down"),
            self._facts(host="slow-host", verdict="degraded"),
            self._facts(host="lost-host", verdict="traffic_lost"),
        ]
        out = _render_bulk_table(rows, 4)
        down_pos = out.find("dead-host")
        traffic_pos = out.find("lost-host")
        degraded_pos = out.find("slow-host")
        healthy_pos = out.find("ok-host")
        assert down_pos < traffic_pos < degraded_pos < healthy_pos

    def test_render_bulk_table_counts_flagged(self):
        from zbbx_mcp.tools.diagnose import _render_bulk_table
        rows = [
            self._facts(host="a", verdict="healthy"),
            self._facts(host="b", verdict="down"),
            self._facts(host="c", verdict="traffic_lost"),
        ]
        out = _render_bulk_table(rows, 3)
        assert "2 flagged" in out

    def test_render_bulk_table_truncates_long_action(self):
        from zbbx_mcp.tools.diagnose import _render_bulk_table
        rows = [self._facts(
            verdict="traffic_lost",
            action="a" * 200,
        )]
        out = _render_bulk_table(rows, 1)
        assert "..." in out

class TestFreshestAgentPing:
    """Pure-helper tests for _freshest_agent_ping (#158, ADR 049)."""

    def test_none_when_no_ping(self):
        from zbbx_mcp.tools.diagnose import _freshest_agent_ping
        assert _freshest_agent_ping([{"key_": "net.if.in[primary]"}]) is None

    def test_picks_freshest_across_vips(self):
        from zbbx_mcp.tools.diagnose import _freshest_agent_ping
        # parent's ping is live (clock 200, up); a stale sub-host ping (clock
        # 100, down) must not win.
        items = [
            {"key_": "agent.ping", "lastvalue": "0", "lastclock": "100"},
            {"key_": "agent.ping", "lastvalue": "1", "lastclock": "200"},
        ]
        ping = _freshest_agent_ping(items)
        assert ping["lastvalue"] == "1" and ping["lastclock"] == "200"

    def test_single_ping_returned(self):
        from zbbx_mcp.tools.diagnose import _freshest_agent_ping
        items = [{"key_": "agent.ping", "lastvalue": "1", "lastclock": "50"}]
        assert _freshest_agent_ping(items)["lastclock"] == "50"

    def test_missing_clock_treated_as_zero(self):
        from zbbx_mcp.tools.diagnose import _freshest_agent_ping
        items = [
            {"key_": "agent.ping", "lastvalue": "1"},  # no clock → 0
            {"key_": "agent.ping", "lastvalue": "0", "lastclock": "5"},
        ]
        assert _freshest_agent_ping(items)["lastclock"] == "5"

class TestBulkDiagnosePreFold:
    """Pure-helper tests for `_dedupe_records_by_canonical` (ADR 039).

    Pre-fold of the input host list before bulk diagnose so the
    fan-out emits one row per physical machine.
    """

    def test_standalone_hosts_pass_through(self):
        from zbbx_mcp.tools.diagnose import _dedupe_records_by_canonical
        records = [
            {"hostid": "1", "host": "host-a"},
            {"hostid": "2", "host": "host-b"},
        ]
        deduped, subs = _dedupe_records_by_canonical(records)
        assert len(deduped) == 2
        names = {r["host"] for r in deduped}
        assert names == {"host-a", "host-b"}
        assert all(c == 0 for c in subs.values())

    def test_parent_plus_subhosts_collapse_to_parent(self):
        from zbbx_mcp.tools.diagnose import _dedupe_records_by_canonical
        records = [
            {"hostid": "1", "host": "parent01"},
            {"hostid": "2", "host": "parent01 v1"},
            {"hostid": "3", "host": "parent01 v2"},
            {"hostid": "4", "host": "parent01 v3"},
        ]
        deduped, subs = _dedupe_records_by_canonical(records)
        assert len(deduped) == 1
        # Parent preferred as the representative
        assert deduped[0]["host"] == "parent01"
        assert deduped[0]["hostid"] == "1"
        assert subs["parent01"] == 3
        # The rep carries every VIP's hostid so the diagnosis queries
        # problems across the whole box (ADR 046).
        assert set(deduped[0]["_group_hostids"]) == {"1", "2", "3", "4"}

    def test_standalone_group_hostids_is_self(self):
        from zbbx_mcp.tools.diagnose import _dedupe_records_by_canonical
        deduped, _ = _dedupe_records_by_canonical([{"hostid": "9", "host": "solo"}])
        assert deduped[0]["_group_hostids"] == ["9"]

    def test_subhost_only_set_picks_first_as_rep(self):
        from zbbx_mcp.tools.diagnose import _dedupe_records_by_canonical
        records = [
            {"hostid": "2", "host": "parent01 v1"},
            {"hostid": "3", "host": "parent01 v2"},
            {"hostid": "4", "host": "parent01 v3"},
        ]
        deduped, subs = _dedupe_records_by_canonical(records)
        assert len(deduped) == 1
        assert deduped[0]["host"] == "parent01 v1"
        assert subs["parent01"] == 2

    def test_mixed_standalone_and_groups(self):
        from zbbx_mcp.tools.diagnose import _dedupe_records_by_canonical
        records = [
            {"hostid": "1", "host": "host-a"},
            {"hostid": "2", "host": "parent01"},
            {"hostid": "3", "host": "parent01 v1"},
            {"hostid": "4", "host": "host-b"},
        ]
        deduped, subs = _dedupe_records_by_canonical(records)
        names = {r["host"] for r in deduped}
        assert names == {"host-a", "parent01", "host-b"}
        assert subs["host-a"] == 0
        assert subs["parent01"] == 1
        assert subs["host-b"] == 0

    def test_empty_input(self):
        from zbbx_mcp.tools.diagnose import _dedupe_records_by_canonical
        deduped, subs = _dedupe_records_by_canonical([])
        assert deduped == []
        assert subs == {}

class _ProblemOnlyClient:
    """Minimal async client returning a fixed problem.get payload.

    Records every call so a test can assert nothing else was hit. A
    domain-mode host (no items) makes ``_collect_diagnosis_inner`` issue
    only ``problem.get``, which keeps this stub tiny.
    """

    def __init__(self, problems):
        self._problems = problems
        self.calls = []

    async def call(self, method, params):
        self.calls.append((method, params))
        if method == "problem.get":
            return [dict(p) for p in self._problems]
        return []

class TestDiagnoseSuppressThreading:
    """ADR 052 — _collect_diagnosis_inner honours include_suppressed."""

    async def test_suppressed_only_reads_healthy_by_default(self):
        """A box whose only problem is maintenance-suppressed must not
        read degraded — the false-positive class ADR 052 closes."""
        from zbbx_mcp.tools.diagnose import _collect_diagnosis_inner

        now = 1_000_000
        problems = [
            {"eventid": "1", "name": "planned reboot", "severity": "4",
             "clock": str(now - 60), "suppressed": "1"},
        ]
        client = _ProblemOnlyClient(problems)
        facts = await _collect_diagnosis_inner(
            client, {"hostid": "10", "host": "h"}, [],  # no items → domain mode
            now=now,
        )
        assert facts["problems"] == []
        assert facts["verdict"] == "healthy"

    async def test_include_suppressed_keeps_maintenance_problem(self):
        from zbbx_mcp.tools.diagnose import _collect_diagnosis_inner

        now = 1_000_000
        problems = [
            {"eventid": "1", "name": "planned reboot", "severity": "4",
             "clock": str(now - 60), "suppressed": "1"},
        ]
        client = _ProblemOnlyClient(problems)
        facts = await _collect_diagnosis_inner(
            client, {"hostid": "10", "host": "h"}, [],
            now=now, include_suppressed=True,
        )
        assert [p["name"] for p in facts["problems"]] == ["planned reboot"]
        assert facts["verdict"] == "degraded"

    async def test_mixed_keeps_only_live_problem(self):
        from zbbx_mcp.tools.diagnose import _collect_diagnosis_inner

        now = 1_000_000
        problems = [
            {"eventid": "1", "name": "live", "severity": "4",
             "clock": str(now - 60), "suppressed": "0"},
            {"eventid": "2", "name": "maint", "severity": "4",
             "clock": str(now - 60), "suppressed": "1"},
        ]
        client = _ProblemOnlyClient(problems)
        facts = await _collect_diagnosis_inner(
            client, {"hostid": "10", "host": "h"}, [], now=now,
        )
        assert [p["name"] for p in facts["problems"]] == ["live"]

class TestKeepActiveOrRecent:
    """ADR 069 — diagnose_host must not age out still-active problems."""

    NOW = 1_000_000

    def test_active_old_problem_kept(self):
        from zbbx_mcp.tools.diagnose import _keep_active_or_recent
        # Unresolved (no r_eventid), started 72h ago — must survive the window.
        probs = [{"eventid": "1", "clock": str(self.NOW - 72 * 3600)}]
        assert _keep_active_or_recent(probs, self.NOW, 24) == probs

    def test_active_old_problem_with_zero_r_eventid_kept(self):
        from zbbx_mcp.tools.diagnose import _keep_active_or_recent
        probs = [{"eventid": "1", "clock": str(self.NOW - 72 * 3600), "r_eventid": "0"}]
        assert len(_keep_active_or_recent(probs, self.NOW, 24)) == 1

    def test_resolved_old_problem_dropped(self):
        from zbbx_mcp.tools.diagnose import _keep_active_or_recent
        probs = [{"eventid": "1", "clock": str(self.NOW - 72 * 3600), "r_eventid": "9"}]
        assert _keep_active_or_recent(probs, self.NOW, 24) == []

    def test_resolved_recent_problem_kept(self):
        from zbbx_mcp.tools.diagnose import _keep_active_or_recent
        probs = [{"eventid": "1", "clock": str(self.NOW - 60), "r_eventid": "9"}]
        assert len(_keep_active_or_recent(probs, self.NOW, 24)) == 1

    async def test_days_old_active_problem_keeps_host_non_healthy(self):
        """The reported bug: a host with an unresolved Disaster from 3 days
        ago must NOT read healthy."""
        from zbbx_mcp.tools.diagnose import _collect_diagnosis_inner

        now = 1_000_000
        problems = [
            {"eventid": "1", "name": "Service down", "severity": "5",
             "clock": str(now - 72 * 3600), "suppressed": "0", "r_eventid": "0"},
        ]
        client = _ProblemOnlyClient(problems)
        facts = await _collect_diagnosis_inner(
            client, {"hostid": "10", "host": "h"}, [], now=now,
        )
        assert [p["name"] for p in facts["problems"]] == ["Service down"]
        assert facts["verdict"] != "healthy"
