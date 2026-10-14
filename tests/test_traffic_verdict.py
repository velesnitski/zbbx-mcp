"""ADR 152: a traffic-drop alert is judged by traffic, not by the problem table.

Fixtures are neutral: uninhabited-territory codes, reserved numbering,
documentation addresses. Series are synthetic minute samples in Mbps.
"""

from __future__ import annotations

import time

import pytest

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp.tools import triage
from zbbx_mcp.traffic_verdict import (
    Evidence,
    Shape,
    classify_shape,
    is_traffic_drop_alert,
    traffic_triage,
    usual_for_hour,
)

NOW = 1_800_000_000 - (1_800_000_000 % 3600) + 1800   # half past an hour, fixed


def minutes(start_min_ago: int, values: list[float], now: int = NOW) -> list[tuple[int, float]]:
    """One sample per minute, the first one ``start_min_ago`` minutes ago."""
    t0 = now - start_min_ago * 60
    return [(t0 + i * 60, v) for i, v in enumerate(values)]


class TestAlertClass:
    @pytest.mark.parametrize("text", [
        "Warning: sharp traffic drop on srv-aq9001 (eth0)",
        "High: traffic drop on srv-aq9001",
        "net.if.in[eth0] below baseline on srv-aq9001",
    ])
    def test_traffic_drop_texts_match(self, text):
        assert is_traffic_drop_alert(text)

    def test_other_triggers_do_not(self):
        assert not is_traffic_drop_alert("Zabbix agent is not available on srv-aq9001")

    def test_a_deployment_pattern_can_replace_the_default(self, monkeypatch):
        monkeypatch.setenv("ZABBIX_TRAFFIC_DROP_PATTERN", r"carrier\s+lost")
        assert is_traffic_drop_alert("carrier lost on srv-aq9001")
        assert not is_traffic_drop_alert("traffic drop on srv-aq9001")

    def test_a_broken_pattern_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setenv("ZABBIX_TRAFFIC_DROP_PATTERN", "([")
        assert is_traffic_drop_alert("traffic drop on srv-aq9001")


class TestUsualForHour:
    def test_median_of_the_hour_and_its_neighbours(self):
        hourly = [(d * 86400 + h * 3600, 100.0 + h) for d in range(7) for h in (9, 10, 11)]
        assert usual_for_hour(hourly, 10) == 110.0

    def test_too_little_history_is_none(self):
        assert usual_for_hour([(10 * 3600, 5.0)] * 3, 10) is None


class TestShape:
    def test_cliff(self):
        s = classify_shape(minutes(120, [100.0] * 60 + [2.0] * 60), 100.0, NOW)
        assert s.kind == "cliff" and s.drop_ts == NOW - 60 * 60

    def test_decay(self):
        ramp = [100 - i * 1.5 for i in range(60)] + [8.0] * 60
        s = classify_shape(minutes(120, ramp), 100.0, NOW)
        assert s.kind == "decay", s

    def test_maintenance_gap_at_the_drop(self):
        before = minutes(120, [100.0] * 50)
        after = minutes(55, [3.0] * 55)        # 15-minute hole, then low
        s = classify_shape(before + after, 100.0, NOW)
        assert s.kind == "maintenance" and "samples missing" in s.reason

    def test_periodic_when_troughs_recovered_and_this_one_is_short(self):
        cycle = [100.0] * 60 + [1.0] * 30
        series = cycle * 4 + [100.0] * 60 + [1.0] * 20
        s = classify_shape(minutes(len(series), series), 100.0, NOW)
        assert s.kind == "periodic", s

    def test_a_long_trough_after_a_periodic_history_is_not_periodic(self):
        # Two short troughs recovered, then the node stays down for hours:
        # the history must not excuse the current outage.
        series = ([100.0] * 60 + [1.0] * 30) * 2 + [100.0] * 60 + [1.0] * 300
        s = classify_shape(minutes(len(series), series), 100.0, NOW)
        assert s.kind in ("cliff", "decay"), s

    def test_no_drop(self):
        assert classify_shape(minutes(60, [90.0] * 60), 100.0, NOW).kind == "none"

    def test_no_usual_is_unknown(self):
        assert classify_shape(minutes(60, [1.0] * 60), None, NOW).kind == "unknown"


class TestVerdict:
    def test_recovered_needs_half_of_usual_and_quotes_numbers(self):
        v, a = traffic_triage(Evidence(60.0, 100.0, Shape("none", "")), False, NOW)
        assert v == "recovered" and "60 Mbps = 60 %" in a and "100 Mbps usual" in a

    def test_closed_trigger_on_a_low_node_is_still_down(self):
        ev = Evidence(5.0, 100.0, Shape("cliff", "under 5 % of usual within 3 min", NOW - 3600))
        v, a = traffic_triage(ev, False, NOW)
        assert v == "still_down"
        assert a.startswith("trigger closed; last 2 h 5.0 Mbps = 5 % of the 100 Mbps usual")
        assert "down 60 min" in a and "[cliff:" in a

    def test_no_history_is_never_recovered(self):
        v, a = traffic_triage(Evidence(None, None, Shape("unknown", "")), False, NOW)
        assert v == "unmeasured" and "cannot be confirmed" in a

    def test_periodic_and_maintenance_have_their_own_advice(self):
        p = traffic_triage(Evidence(2.0, 100.0, Shape("periodic", "x", NOW - 600)), True, NOW)
        m = traffic_triage(Evidence(2.0, 100.0, Shape("maintenance", "y", NOW - 600)), True, NOW)
        assert p[0] == "periodic" and "rotation and firewall" in p[1] and "trigger still open" in p[1]
        assert m[0] == "maintenance" and "Re-check in 30 min" in m[1]


# --- wire: the tool ---------------------------------------------------------

def _client(recent_mbps: float, problems: list[dict]):
    now = int(time.time())
    hour = now - now % 3600
    week = [{"itemid": "i1", "clock": str(hour - d * 86400 + o * 3600), "value_avg": str(100_000_000)}
            for d in range(2, 8) for o in (-1, 0, 1)]
    hist = [{"clock": str(now - 7200 + i * 60), "value": str(int(recent_mbps * 1_000_000))}
            for i in range(120)]
    hosts = [{"hostid": "9001", "host": "srv-aq9001", "name": "srv-aq9001", "hostgroups": []}]

    def item_get(p):
        return [{"itemid": "i1", "hostid": "9001", "key_": "net.if.in[eth0]", "value_type": "3"}]

    return RecordingClient({
        "host.get": hosts, "problem.get": problems, "trigger.get": [],
        "item.get": item_get, "trend.get": week, "history.get": hist,
    })


class TestTool:
    def test_a_closed_trigger_over_a_dead_node_is_not_resolved(self):
        out = run_tool(triage, "triage_slack_alert", _client(3.0, []),
                       text="RESOLVED: sharp traffic drop on srv-aq9001 (eth0)")
        assert "verdict from the carrier interface" in out
        assert "**still_down**" in out and "recovered" not in out.split("**srv-aq9001**")[1]
        assert "3.0 Mbps = 3 % of the 100 Mbps usual" in out

    def test_a_closed_trigger_with_traffic_back_is_recovered_with_numbers(self):
        out = run_tool(triage, "triage_slack_alert", _client(80.0, []),
                       text="RESOLVED: traffic drop on srv-aq9001")
        assert "**recovered**" in out and "80 Mbps = 80 %" in out

    def test_other_trigger_classes_keep_the_problem_table_verdict(self):
        c = _client(3.0, [])
        out = run_tool(triage, "triage_slack_alert", c, text="RESOLVED: agent unavailable on srv-aq9001")
        assert "**recovered**" in out and "carrier interface" not in out
        assert not any(m == "history.get" for m, _ in c.calls)
