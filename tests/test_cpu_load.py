"""CPU level, flat-run detection, and the verdict that depends on them.

The regression these guard against: `diagnose_host` returned
`healthy` / "No issues detected" for a host whose CPU was fully occupied by a
long-running process. The verdict consulted agent reachability, traffic against
baseline, IP rotation and open problems — and never CPU. A compute-bound
process moves almost no bytes, so the traffic arm read normal too.

Every check passed. None of them looked at the thing that was wrong.

Figures below are illustrative, not measurements.
"""

from zbbx_mcp.cpu_load import (
    FLAT_MIN_HOURS,
    cpu_pct_from_items,
    flat_run_hours,
    judge_cpu,
)
from zbbx_mcp.tools.diagnose import _classify_verdict


def _item(key, value, clock=1_760_000_000):
    return {"key_": key, "lastvalue": str(value), "lastclock": str(clock)}


class TestCpuFromItems:
    def test_direct_utilisation_key_is_used(self):
        assert cpu_pct_from_items([_item("system.cpu.util", "37.0")]) == 37.0

    def test_idle_key_is_inverted(self):
        # Only the idle counter exists: utilisation is its complement.
        assert cpu_pct_from_items([_item("system.cpu.util[,idle]", "63.0")]) == 37.0

    def test_direct_key_wins_over_idle(self):
        pct = cpu_pct_from_items([
            _item("system.cpu.util[,idle]", "63.0"),
            _item("system.cpu.util", "37.0"),
        ])
        assert pct == 37.0

    def test_never_collected_is_none_not_zero(self):
        # THE task-193 case. Zabbix marks a never-collected item with
        # lastclock=0. Read without checking the clock it renders as a real
        # measurement of zero — an idle host — when nothing is known at all.
        assert cpu_pct_from_items([_item("system.cpu.util", "0", clock=0)]) is None
        # And it must not be rescued by an equally-dead idle item.
        assert cpu_pct_from_items([
            _item("system.cpu.util", "0", clock=0),
            _item("system.cpu.util[,idle]", "0", clock=0),
        ]) is None

    def test_absent_or_unparseable_is_none(self):
        assert cpu_pct_from_items([]) is None
        assert cpu_pct_from_items([_item("agent.ping", "1")]) is None
        assert cpu_pct_from_items([_item("system.cpu.util", "")]) is None


class TestFlatRun:
    def test_a_flat_run_is_counted(self):
        # Hourly (min, max) within a hair of each other, most recent first.
        hourly = [(36.8, 37.2)] * 12
        assert flat_run_hours(hourly) == 12

    def test_varying_load_is_not_flat(self):
        # Real load breathes: several points of spread inside the hour.
        assert flat_run_hours([(20.0, 60.0)] * 12) == 0

    def test_the_run_stops_at_the_first_varying_hour(self):
        hourly = [(36.8, 37.2)] * 4 + [(10.0, 70.0)] + [(36.8, 37.2)] * 8
        assert flat_run_hours(hourly) == 4

    def test_an_idle_host_is_not_flagged(self):
        # Flatness below the floor is just an absence of work, not a finding.
        assert flat_run_hours([(0.4, 0.5)] * 24) == 0

    def test_a_gap_ends_the_run_rather_than_being_skipped(self):
        # Skipping a missing hour would splice two runs into one and report a
        # duration that never happened.
        hourly = [(36.8, 37.2)] * 3 + [(None, None)] + [(36.8, 37.2)] * 20
        assert flat_run_hours(hourly) == 3


class TestJudgeCpu:
    def test_unmeasured_is_its_own_answer(self):
        flag, note = judge_cpu(None)
        assert flag == "unmeasured"
        assert "NOT checked" in note

    def test_flat_outranks_busy(self):
        # A host at 95% that varies is doing work; one pinned flat is doing the
        # same thing over and over, which is the more specific statement.
        flag, note = judge_cpu(95.0, flat_hours=FLAT_MIN_HOURS)
        assert flag == "flat"
        assert "flat" in note.lower()

    def test_busy_without_flatness(self):
        assert judge_cpu(95.0, flat_hours=0)[0] == "busy"

    def test_a_short_flat_run_is_not_enough(self):
        assert judge_cpu(37.0, flat_hours=FLAT_MIN_HOURS - 1)[0] is None

    def test_ordinary_load_says_nothing(self):
        flag, note = judge_cpu(37.0, flat_hours=0)
        assert flag is None
        assert "37" in note


class TestVerdictNoLongerLies:
    """The regression, at the level the tool actually reports."""

    HEALTHY_ON_EVERY_OTHER_AXIS = dict(
        mode="server",
        agent_ping_val=1,
        agent_ping_age_min=0.3,
        traffic_baseline_mbps=0.1,
        traffic_recent_mbps=0.1,
        open_problems=0,
        https_down=False,
        https_age_h=None,
    )

    def test_flat_cpu_is_not_healthy(self):
        # Agent up, traffic normal, zero problems — and CPU pinned flat. This
        # exact combination returned `healthy` / "No issues detected".
        flag, note = judge_cpu(37.0, flat_hours=12)
        verdict, action = _classify_verdict(
            **self.HEALTHY_ON_EVERY_OTHER_AXIS, cpu_flag=flag, cpu_note=note
        )
        assert verdict == "degraded", "a host pinned flat must not read healthy"
        assert "flat" in action.lower()
        assert "12h" in action

    def test_a_busy_host_is_not_healthy(self):
        flag, note = judge_cpu(97.0)
        verdict, _ = _classify_verdict(
            **self.HEALTHY_ON_EVERY_OTHER_AXIS, cpu_flag=flag, cpu_note=note
        )
        assert verdict == "degraded"

    def test_healthy_names_the_checks_that_ran(self):
        # "No issues detected" meant "none of the four things I looked at" and
        # was read as "nothing is wrong". The scope has to be in the sentence.
        flag, note = judge_cpu(4.0)
        verdict, action = _classify_verdict(
            **self.HEALTHY_ON_EVERY_OTHER_AXIS, cpu_flag=flag, cpu_note=note
        )
        assert verdict == "healthy"
        for named in ("agent", "traffic", "problems"):
            assert named in action.lower(), f"{named!r} not named in: {action}"

    def test_unmeasured_cpu_is_disclosed_in_a_healthy_verdict(self):
        # A host with no CPU item is not a host with acceptable CPU.
        flag, note = judge_cpu(None)
        verdict, action = _classify_verdict(
            **self.HEALTHY_ON_EVERY_OTHER_AXIS, cpu_flag=flag, cpu_note=note
        )
        assert verdict == "healthy"
        assert "NOT checked" in action, f"silent about unmeasured CPU: {action}"

    def test_cpu_does_not_mask_real_problems(self):
        facts = dict(self.HEALTHY_ON_EVERY_OTHER_AXIS)
        facts["open_problems"] = 3
        flag, note = judge_cpu(37.0, flat_hours=12)
        verdict, action = _classify_verdict(**facts, cpu_flag=flag, cpu_note=note)
        assert verdict == "degraded"
        assert "3 active problem" in action

    def test_an_agent_down_host_still_outranks_cpu(self):
        # CPU must not demote a harder failure to `degraded`.
        facts = dict(self.HEALTHY_ON_EVERY_OTHER_AXIS)
        facts["agent_ping_val"] = 0
        facts["traffic_baseline_mbps"] = None
        facts["traffic_recent_mbps"] = None
        flag, note = judge_cpu(37.0, flat_hours=12)
        verdict, _ = _classify_verdict(**facts, cpu_flag=flag, cpu_note=note)
        assert verdict == "down"

    def test_the_old_behaviour_would_fail_this_suite(self):
        # Non-vacuity: with no CPU signal supplied — which is what the caller
        # passed before this change — the verdict is still `healthy`. That is
        # the bug, pinned, so the tests above cannot pass by accident.
        verdict, _ = _classify_verdict(**self.HEALTHY_ON_EVERY_OTHER_AXIS)
        assert verdict == "healthy"
