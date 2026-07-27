"""Time-honest uptime + retention-coverage tests (ADR 075, tasks 168-170)."""

import time as _time

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp.tools import geo_health as geo_mod
from zbbx_mcp.uptime import compute_host_uptime, coverage_note, retention_too_short

HOUR = 3600
NOW = 1_000_000 * HOUR  # a round hour boundary
WINDOW = 30 * 24 * HOUR
START = NOW - WINDOW


class TestComputeHostUptime:
    def test_dead_host_one_sample_reads_near_zero(self):
        # The task-168 bug: up 1h 14d ago, dead since, no traffic → ~0%, not 100%.
        rows = [(NOW - 14 * 24 * HOUR, "1")]
        up, total = compute_host_uptime(rows, NOW, START, host_has_traffic=False)
        # first-seen → now, every COMPLETED hour counted. The in-progress
        # hour has no trend row yet, so it is unmeasured, not down (ADR 097).
        assert total == 14 * 24
        assert up == 1                        # only the one observed up hour
        assert up / total < 0.01

    def test_fully_up_host(self):
        rows = [(START + h * HOUR, "1") for h in range(0, 24 * 30 + 1)]
        up, total = compute_host_uptime(rows, NOW, START, host_has_traffic=False)
        assert up == total and total == 24 * 30 + 1

    def test_explicit_down_hours_count_down(self):
        rows = [(NOW - 3 * HOUR, "1"), (NOW - 2 * HOUR, "0"), (NOW - 1 * HOUR, "1")]
        up, total = compute_host_uptime(rows, NOW, START, host_has_traffic=False)
        # 3 completed hours observed, 2 up; the in-progress hour is excluded.
        assert total == 3 and up == 2

    def test_traffic_rescues_missing_hours(self):
        # Deprecated check: one old sample then silence, but real traffic → UP.
        rows = [(NOW - 10 * HOUR, "1")]
        up, total = compute_host_uptime(rows, NOW, START, host_has_traffic=True)
        assert up == total                    # every missing hour rescued by traffic

    def test_traffic_does_not_override_explicit_down(self):
        # Gap-free explicit downs (incl. the current hour) so the traffic gate
        # has no missing hour to rescue — explicit down must stay down.
        rows = [(NOW - 2 * HOUR, "0"), (NOW - 1 * HOUR, "0"), (NOW, "0")]
        up, total = compute_host_uptime(rows, NOW, START, host_has_traffic=True)
        assert up == 0 and total == 3         # explicit down beats traffic gate

    def test_no_samples_returns_zero_zero(self):
        assert compute_host_uptime([], NOW, START, host_has_traffic=True) == (0, 0)
        assert compute_host_uptime([], NOW, START, host_has_traffic=False) == (0, 0)

    def test_samples_before_window_ignored(self):
        rows = [(START - 5 * HOUR, "1"), (NOW - 1 * HOUR, "1")]
        up, total = compute_host_uptime(rows, NOW, START, host_has_traffic=False)
        assert total == 1                     # pre-window sample dropped

    def test_bad_values_skipped(self):
        rows = [(NOW - 1 * HOUR, "1"), ("bad", "1"), (NOW - 2 * HOUR, None)]
        up, total = compute_host_uptime(rows, NOW, START, host_has_traffic=False)
        assert total == 1 and up == 1


class TestCoverageNote:
    def test_short_coverage_warns(self):
        min_clock = NOW - 14 * 24 * HOUR       # 14d of a 30d request
        out = coverage_note(min_clock, NOW, WINDOW)
        assert "14.0d" in out and "30d" in out

    def test_adequate_coverage_silent(self):
        min_clock = NOW - 29 * 24 * HOUR       # ~29d of 30d → within 5%
        assert coverage_note(min_clock, NOW, WINDOW) == ""

    def test_no_data_silent(self):
        assert coverage_note(None, NOW, WINDOW) == ""
        assert coverage_note(0, NOW, WINDOW) == ""


class TestRetentionTooShort:
    def test_true_when_history_under_two_periods(self):
        # 14d history, 30d period → can't fill the prior 30d → True.
        assert retention_too_short(NOW - 14 * 24 * HOUR, NOW, WINDOW) is True

    def test_false_when_history_covers_both(self):
        assert retention_too_short(NOW - 61 * 24 * HOUR, NOW, WINDOW) is False

    def test_no_data_is_false(self):
        assert retention_too_short(None, NOW, WINDOW) is False


class TestPerHourTrafficGate:
    """Task 172 / ADR 081: the traffic gate is per hour, not window-wide."""

    def test_served_then_died_reads_half_not_full(self):
        # The task-172 pin: checks + traffic for week 1, NOTHING in week 2.
        # The window-wide boolean read this ~100% (early traffic rescued the
        # dead tail); the per-hour gate must read ~50%.
        first = NOW - 14 * 24 * HOUR
        rows = [(first + h * HOUR, "1") for h in range(0, 7 * 24)]
        hours = {(first + h * HOUR) // HOUR for h in range(0, 7 * 24)}
        up, total = compute_host_uptime(rows, NOW, START, host_has_traffic=hours)
        assert total == 14 * 24
        assert abs(up / total - 0.5) < 0.01
        # regression contrast: legacy bool inflates the same host to 100%
        up_b, total_b = compute_host_uptime(rows, NOW, START, host_has_traffic=True)
        assert up_b == total_b

    def test_rescues_only_hours_with_traffic(self):
        # One old sample, silent since; traffic in exactly 3 later hours.
        rows = [(NOW - 10 * HOUR, "1")]
        hours = {(NOW - 5 * HOUR) // HOUR, (NOW - 4 * HOUR) // HOUR,
                 (NOW - 3 * HOUR) // HOUR}
        up, total = compute_host_uptime(rows, NOW, START, host_has_traffic=hours)
        assert total == 10 and up == 1 + 3   # the sample + the 3 traffic hours

    def test_empty_set_means_no_rescue(self):
        rows = [(NOW - 10 * HOUR, "1")]
        up, total = compute_host_uptime(rows, NOW, START, host_has_traffic=set())
        assert total == 10 and up == 1

    def test_traffic_hour_does_not_override_explicit_down(self):
        rows = [(NOW - 1 * HOUR, "0"), (NOW, "0")]
        hours = {(NOW - 1 * HOUR) // HOUR, NOW // HOUR}
        up, total = compute_host_uptime(rows, NOW, START, host_has_traffic=hours)
        assert up == 0 and total == 2


class TestServiceUptimeReportUintFix:
    """Task 175 / ADR 092: get_service_uptime_report must read value_MAX for
    the 0/1 service checks, not the integer-truncated trends_uint value_avg.
    """

    def _client(self, now):
        # 24 consecutive up-hours where Zabbix stored value_avg=0 (a 59/60 hour
        # truncates to 0 in the bigint trends_uint column) but value_max=1.
        trend_rows = [
            {"itemid": "i1", "clock": str(now - h * 3600),
             "value_avg": "0", "value_max": "1"}
            for h in range(24)
        ]
        return RecordingClient({
            "host.get": [
                {"hostid": "1", "host": "edge-de1", "groups": [{"name": "edge"}]},
            ],
            "item.get": [
                {"itemid": "i1", "hostid": "1", "key_": "svc.check",
                 "state": "0", "lastclock": str(now)},
            ],
            "trend.get": trend_rows,
        })

    def test_near_perfect_uint_hours_read_up_not_down(self, monkeypatch):
        monkeypatch.setattr(geo_mod, "KEY_service_PRIMARY", "svc.check")
        monkeypatch.setattr(geo_mod, "KEY_service_SECONDARY", "")
        now = int(_time.time())
        out = run_tool(
            geo_mod, "get_service_uptime_report", self._client(now),
            only_problems=False, period="1d",
        )
        # value_max=1 every hour -> ~100% HEALTHY. Under the old value_avg=0
        # path this same host read 0.0% / DOWN (the 60x over-penalty).
        # value_max=1 every hour -> the host's row reads 100.0% / HEALTHY.
        # Under the old value_avg=0 path it read 0.0% / DOWN.
        assert "| edge-de1 | DE | 100.0% | N/A | HEALTHY |" in out

    def test_wire_requests_value_max_for_checks(self, monkeypatch):
        monkeypatch.setattr(geo_mod, "KEY_service_PRIMARY", "svc.check")
        monkeypatch.setattr(geo_mod, "KEY_service_SECONDARY", "")
        now = int(_time.time())
        client = self._client(now)
        run_tool(geo_mod, "get_service_uptime_report", client,
                 only_problems=False, period="1d")
        sent = client.sent("trend.get")
        assert "value_max" in sent["output"]


class TestTrafficHoursFromTrends:
    def test_threshold_and_bucketing(self):
        from zbbx_mcp.uptime import traffic_hours_from_trends
        rows = [(NOW - 2 * HOUR, "5000000"),   # 5 Mbps (bits divisor) -> counts
                (NOW - 1 * HOUR, "200000"),    # 0.2 Mbps -> below the bar
                (NOW, "1000000")]              # exactly 1 Mbps -> counts
        hours = traffic_hours_from_trends(rows, 1_000_000)
        assert hours == {(NOW - 2 * HOUR) // HOUR, NOW // HOUR}

    def test_any_nic_clears_the_bar_and_bad_rows_skipped(self):
        from zbbx_mcp.uptime import traffic_hours_from_trends
        rows = [(NOW, "0"), (NOW, "9000000"),          # idle + busy NIC same hour
                ("bad", "1"), (NOW - 1 * HOUR, None)]  # junk skipped
        assert traffic_hours_from_trends(rows, 1_000_000) == {NOW // HOUR}
