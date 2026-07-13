"""Problem, acknowledge/snooze/rank, and suppress tests (split from test_analytics, ADR 074)."""



class TestProblemAgeBuckets:
    """Pure-helper tests for the age-histogram bucketer (#132)."""

    def _p(self, sev, age_sec, now=1_700_000_000):
        return {"severity": sev, "clock": now - age_sec}

    def test_empty_input_returns_empty_dict(self):
        from zbbx_mcp.tools.health import _bucket_problems_by_age

        assert _bucket_problems_by_age([], 1_700_000_000) == {}

    def test_three_problems_one_per_bucket(self):
        from zbbx_mcp.tools.health import _bucket_problems_by_age

        problems = [
            self._p(4, 3600),       # <1d
            self._p(4, 2 * 86400),  # 1-3d
            self._p(4, 5 * 86400),  # 3-7d
        ]
        out = _bucket_problems_by_age(problems, 1_700_000_000)
        assert out[4] == {"<1d": 1, "1-3d": 1, "3-7d": 1, "7d+": 0}

    def test_seven_day_overflow_lands_in_seven_d_plus(self):
        from zbbx_mcp.tools.health import _bucket_problems_by_age

        problems = [self._p(5, 14 * 86400)]
        assert _bucket_problems_by_age(problems, 1_700_000_000)[5]["7d+"] == 1

    def test_boundary_one_day_lands_in_one_three(self):
        from zbbx_mcp.tools.health import _bucket_problems_by_age

        # Exactly 86400s old = 1 day. Strict "<1d" pushes it into the 1-3d bucket.
        problems = [self._p(3, 86400)]
        out = _bucket_problems_by_age(problems, 1_700_000_000)
        assert out[3]["<1d"] == 0
        assert out[3]["1-3d"] == 1

    def test_severities_partitioned_independently(self):
        from zbbx_mcp.tools.health import _bucket_problems_by_age

        problems = [
            self._p(5, 3600),
            self._p(5, 3600),
            self._p(2, 3600),
        ]
        out = _bucket_problems_by_age(problems, 1_700_000_000)
        assert out[5]["<1d"] == 2
        assert out[2]["<1d"] == 1

    def test_bad_clock_skipped(self):
        from zbbx_mcp.tools.health import _bucket_problems_by_age

        problems = [
            {"severity": 4, "clock": 0},
            {"severity": 4, "clock": "garbage"},
            {"severity": 4, "clock": 1_700_000_000 - 3600},  # valid
        ]
        out = _bucket_problems_by_age(problems, 1_700_000_000)
        assert out[4]["<1d"] == 1
        assert sum(out[4].values()) == 1

class TestAckActionBuilder:
    """Pure-helper tests for _build_ack_action (v1.8.3 acknowledge_problem extension)."""

    def test_default_is_acknowledge_only(self):
        from zbbx_mcp.tools.problems import _build_ack_action
        assert _build_ack_action() == 2

    def test_close_only(self):
        from zbbx_mcp.tools.problems import _build_ack_action
        # ack (2) + close (1) = 3
        assert _build_ack_action(close=True) == 3

    def test_message_sets_bit_4(self):
        from zbbx_mcp.tools.problems import _build_ack_action
        # ack (2) + message (4) = 6
        assert _build_ack_action(message="hello") == 6

    def test_severity_sets_bit_8(self):
        from zbbx_mcp.tools.problems import _build_ack_action
        # ack (2) + severity (8) = 10
        assert _build_ack_action(severity=4) == 10

    def test_severity_out_of_range_is_ignored(self):
        from zbbx_mcp.tools.problems import _build_ack_action
        assert _build_ack_action(severity=-1) == 2
        assert _build_ack_action(severity=6) == 2
        assert _build_ack_action(severity=99) == 2

    def test_all_optional_flags_compose(self):
        from zbbx_mcp.tools.problems import _build_ack_action
        # ack(2) + close(1) + msg(4) + sev(8) = 15
        assert _build_ack_action(
            close=True, message="x", severity=3,
        ) == 15

    def test_unack_replaces_ack_bit(self):
        from zbbx_mcp.tools.problems import _build_ack_action
        # unack (16) replaces ack (2) — mutually exclusive
        assert _build_ack_action(unack=True) == 16

    def test_unack_can_combine_with_close_and_message(self):
        from zbbx_mcp.tools.problems import _build_ack_action
        # unack(16) + close(1) + msg(4) = 21
        assert _build_ack_action(unack=True, close=True, message="x") == 21

    def test_suppress_bit(self):
        from zbbx_mcp.tools.problems import _build_ack_action
        # ack(2) + suppress(32) — ADR 059
        assert _build_ack_action(suppress=True) == 34

    def test_unsuppress_bit(self):
        from zbbx_mcp.tools.problems import _build_ack_action
        # ack(2) + unsuppress(64)
        assert _build_ack_action(unsuppress=True) == 66

    def test_suppress_with_message_combo(self):
        from zbbx_mcp.tools.problems import _build_ack_action
        # ack(2) + msg(4) + suppress(32) = 38
        assert _build_ack_action(message="snooze", suppress=True) == 38

class TestBuildRankAction:
    """Pure-helper tests for _build_rank_action (ADR 060)."""

    def test_rank_as_symptom(self):
        from zbbx_mcp.tools.problems import _build_rank_action
        assert _build_rank_action() == 256

    def test_unrank_to_cause(self):
        from zbbx_mcp.tools.problems import _build_rank_action
        assert _build_rank_action(unrank=True) == 128

    def test_message_adds_bit_4(self):
        from zbbx_mcp.tools.problems import _build_rank_action
        assert _build_rank_action(message="correlated by subnet") == 260
        assert _build_rank_action(unrank=True, message="split") == 132

class TestSuppressUntilFromHours:
    """Pure-helper tests for _suppress_until_from_hours (ADR 059)."""

    NOW = 1_000_000

    def test_zero_means_no_suppression(self):
        from zbbx_mcp.tools.problems import _suppress_until_from_hours
        assert _suppress_until_from_hours(0, self.NOW) is None

    def test_positive_hours_to_epoch(self):
        from zbbx_mcp.tools.problems import _suppress_until_from_hours
        assert _suppress_until_from_hours(4, self.NOW) == self.NOW + 4 * 3600

    def test_fractional_hours(self):
        from zbbx_mcp.tools.problems import _suppress_until_from_hours
        assert _suppress_until_from_hours(0.5, self.NOW) == self.NOW + 1800

    def test_negative_means_indefinite_zero(self):
        from zbbx_mcp.tools.problems import _suppress_until_from_hours
        # Zabbix encodes "until the problem resolves" as suppress_until=0.
        assert _suppress_until_from_hours(-1, self.NOW) == 0

class TestCollapseDependentProblems:
    """Pure-helper tests for collapse_dependent_problems (#144, ADR 048)."""

    def test_drops_symptom_when_root_firing(self):
        from zbbx_mcp.data import collapse_dependent_problems
        problems = [
            {"eventid": "1", "objectid": "10", "name": "root"},
            {"eventid": "2", "objectid": "20", "name": "symptom"},
        ]
        kept, n = collapse_dependent_problems(problems, {"20": {"10"}})
        assert n == 1
        assert {p["objectid"] for p in kept} == {"10"}

    def test_keeps_symptom_when_root_not_firing(self):
        from zbbx_mcp.data import collapse_dependent_problems
        problems = [{"eventid": "2", "objectid": "20", "name": "symptom"}]
        kept, n = collapse_dependent_problems(problems, {"20": {"10"}})
        assert n == 0 and len(kept) == 1

    def test_no_dependencies_is_noop(self):
        from zbbx_mcp.data import collapse_dependent_problems
        problems = [{"eventid": "1", "objectid": "10"}, {"eventid": "2", "objectid": "11"}]
        kept, n = collapse_dependent_problems(problems, {})
        assert n == 0 and len(kept) == 2

    def test_collapse_false_is_noop(self):
        from zbbx_mcp.data import collapse_dependent_problems
        problems = [{"eventid": "1", "objectid": "10"}, {"eventid": "2", "objectid": "20"}]
        kept, n = collapse_dependent_problems(problems, {"20": {"10"}}, collapse=False)
        assert n == 0 and len(kept) == 2

    def test_chain_collapses_only_active_dependency(self):
        from zbbx_mcp.data import collapse_dependent_problems
        problems = [
            {"eventid": "1", "objectid": "10"},
            {"eventid": "2", "objectid": "20"},
            {"eventid": "3", "objectid": "30"},
        ]
        dep_map = {"30": {"20"}, "20": {"10"}}
        kept, n = collapse_dependent_problems(problems, dep_map)
        assert n == 2
        assert {p["objectid"] for p in kept} == {"10"}

    def test_missing_objectid_kept(self):
        from zbbx_mcp.data import collapse_dependent_problems
        kept, n = collapse_dependent_problems([{"eventid": "1"}], {"x": {"y"}})
        assert n == 0 and len(kept) == 1

class TestFormatSnoozeStatus:
    """Pure-helper tests for _format_snooze_status (ADR 071)."""

    NOW = 1_000_000

    def test_empty_and_none(self):
        from zbbx_mcp.tools.problems import _format_snooze_status
        assert _format_snooze_status([], self.NOW) == ""
        assert _format_snooze_status(None, self.NOW) == ""

    def test_maintenance_window(self):
        from zbbx_mcp.tools.problems import _format_snooze_status
        out = _format_snooze_status([{"maintenanceid": "42"}], self.NOW)
        assert "maintenance window (id 42)" in out

    def test_manual_snooze_until_resolve(self):
        from zbbx_mcp.tools.problems import _format_snooze_status
        out = _format_snooze_status(
            [{"maintenanceid": "0", "suppress_until": "0"}], self.NOW)
        assert out == "snoozed until the problem resolves"

    def test_manual_snooze_remaining_time(self):
        from zbbx_mcp.tools.problems import _format_snooze_status
        until = self.NOW + 2 * 3600 + 300  # 2h05m
        out = _format_snooze_status(
            [{"maintenanceid": "0", "suppress_until": str(until)}], self.NOW)
        assert "snoozed for 2h 05m more" in out

    def test_lapsed_snooze(self):
        from zbbx_mcp.tools.problems import _format_snooze_status
        out = _format_snooze_status(
            [{"maintenanceid": "0", "suppress_until": str(self.NOW - 60)}], self.NOW)
        assert "lapsed" in out

    def test_multiple_entries_joined(self):
        from zbbx_mcp.tools.problems import _format_snooze_status
        out = _format_snooze_status(
            [{"maintenanceid": "7"},
             {"maintenanceid": "0", "suppress_until": "0"}], self.NOW)
        assert ";" in out and "maintenance" in out and "resolves" in out

class TestProblemDetailWireContract:
    """ADR 071 — get_problem_detail requests suppress_until and renders
    rank + snooze. Wire-level per the ADR 068/070 lesson."""

    def _run(self, problem, users=None):
        from tests.wiretest import RecordingClient, run_tool
        from zbbx_mcp.tools import problems as problems_mod

        responses = {"problem.get": [problem]}
        if users is not None:
            responses["user.get"] = users
        client = RecordingClient(responses)
        out = run_tool(problems_mod, "get_problem_detail", client, problem_id="9")
        return client, out

    def _problem(self, **extra):
        base = {"eventid": "9", "name": "Service Down", "severity": "4",
                "clock": "1000", "acknowledged": "0", "suppressed": "0"}
        base.update(extra)
        return base

    def test_suppress_until_requested(self):
        client, _ = self._run(self._problem())
        pget = next(p for m, p in client.calls if m == "problem.get")
        assert pget.get("selectSuppressionData") == ["maintenanceid", "suppress_until"]

    def test_symptom_rank_rendered(self):
        _, out = self._run(self._problem(cause_eventid="777"))
        assert "symptom of cause event 777" in out

    def test_cause_event_renders_no_rank_line(self):
        _, out = self._run(self._problem(cause_eventid="0"))
        assert "symptom of cause" not in out

    def test_snooze_rendered(self):
        _, out = self._run(self._problem(
            suppressed="1",
            suppression_data=[{"maintenanceid": "0", "suppress_until": "0"}]))
        assert "snoozed until the problem resolves" in out


class TestProblemDetailAckAuthor:
    """ADR 077 — selectAcknowledges asked for "alias", a field that has never
    existed on an acknowledge object. Zabbix rejected the whole call with
    -32602, so get_problem_detail was dead on *every* problem."""

    LEGAL = {"acknowledgeid", "userid", "clock", "message", "action",
             "old_severity", "new_severity", "suppress_until", "taskid"}

    def _run(self, problem, users=None):
        from tests.wiretest import RecordingClient, run_tool
        from zbbx_mcp.tools import problems as problems_mod

        responses = {"problem.get": [problem]}
        if users is not None:
            responses["user.get"] = users
        client = RecordingClient(responses)
        out = run_tool(problems_mod, "get_problem_detail", client, problem_id="9")
        return client, out

    def _problem(self, **extra):
        base = {"eventid": "9", "name": "Service Down", "severity": "4",
                "clock": "1000", "acknowledged": "0", "suppressed": "0"}
        base.update(extra)
        return base

    def _ack(self):
        return [{"userid": "42", "clock": "1000", "message": "on it"}]

    def test_selectacknowledges_carries_only_legal_fields(self):
        client, _ = self._run(self._problem())
        sent = client.sent("problem.get")["selectAcknowledges"]
        assert "alias" not in sent           # the -32602 carrier
        assert set(sent) <= self.LEGAL

    def test_ack_author_resolved_to_username(self):
        client, out = self._run(
            self._problem(acknowledges=self._ack()),
            users=[{"userid": "42", "username": "ops-oncall"}],
        )
        assert client.sent("user.get")["userids"] == ["42"]
        assert "**ops-oncall**" in out and "on it" in out

    def test_ack_author_falls_back_to_userid(self):
        # A token without user.get rights must degrade to the raw id, not crash
        # and not render the old bare "?".
        _, out = self._run(self._problem(acknowledges=self._ack()), users=[])
        assert "**user 42**" in out
        assert "**?**" not in out

    def test_no_user_get_when_no_acks(self):
        client, _ = self._run(self._problem())
        assert not [m for m, _ in client.calls if m == "user.get"]

class TestFilterSuppressed:
    """Pure-helper tests for filter_suppressed (#143, ADR 044)."""

    def _probs(self):
        return [
            {"eventid": "1", "name": "real", "suppressed": "0"},
            {"eventid": "2", "name": "maint", "suppressed": "1"},
            {"eventid": "3", "name": "also-real"},  # field absent → not suppressed
        ]

    def test_default_excludes_suppressed(self):
        from zbbx_mcp.data import filter_suppressed
        out = filter_suppressed(self._probs())
        assert {p["eventid"] for p in out} == {"1", "3"}

    def test_include_keeps_all(self):
        from zbbx_mcp.data import filter_suppressed
        assert len(filter_suppressed(self._probs(), include_suppressed=True)) == 3

    def test_missing_field_treated_as_visible(self):
        from zbbx_mcp.data import filter_suppressed
        assert len(filter_suppressed([{"eventid": "9"}])) == 1

    def test_empty_input(self):
        from zbbx_mcp.data import filter_suppressed
        assert filter_suppressed([]) == []

    def test_returns_new_list_not_alias(self):
        from zbbx_mcp.data import filter_suppressed
        src = [{"eventid": "1", "suppressed": "0"}]
        out = filter_suppressed(src, include_suppressed=True)
        assert out == src and out is not src
