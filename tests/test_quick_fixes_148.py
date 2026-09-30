"""ADR 148: three small honesty fixes found in use.

Fixtures are neutral: documentation addresses, uninhabited-territory codes,
example numbering in the reserved band.
"""

from __future__ import annotations

import asyncio
import time

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp.tools import analysis, dashboards, problems
from zbbx_mcp.tools.analysis import looks_like_csv_header, split_ip_list
from zbbx_mcp.tools.dashboards import fit_lines
from zbbx_mcp.tools.problems import event_retention_note, retention_seconds

NOW = int(time.time())


class TestIpListParsing:
    def test_any_separator_is_accepted(self):
        text = "192.0.2.1 192.0.2.2\t192.0.2.3,192.0.2.4;192.0.2.5\n192.0.2.6"
        assert split_ip_list(text) == [f"192.0.2.{i}" for i in range(1, 7)]

    def test_a_first_line_of_addresses_is_not_a_header(self):
        assert not looks_like_csv_header("192.0.2.1, 192.0.2.2")
        assert looks_like_csv_header("ip,price_monthly,name")
        assert not looks_like_csv_header("192.0.2.1")

    def test_classify_tool_reads_a_space_separated_list(self):
        out = run_tool(analysis, "classify_external_ips", RecordingClient(),
                       input_data="192.0.2.1 198.51.100.7 203.0.113.9")
        assert "No valid IPs" not in out, out
        assert "3" in out

    def test_audit_tool_reads_a_two_address_line(self):
        c = RecordingClient({"host.get": []})
        out = run_tool(analysis, "audit_external_ips", c, input_data="192.0.2.1, 192.0.2.2")
        assert "No valid IPs" not in out, out


class TestFitLines:
    def test_everything_fits_when_unlimited(self):
        lines = [f"- host{i}" for i in range(50)]
        assert fit_lines(lines, 5000, 0) == lines

    def test_a_cut_names_how_many_were_left_out(self):
        lines = [f"- srv-aq90{i:02d} [Enabled] (app_free)" for i in range(40)]
        out = fit_lines(lines, 100, 600)
        assert len(out) < len(lines)
        assert out[-1].startswith("- … and ") and "more host(s)" in out[-1]
        assert str(len(lines) - (len(out) - 1)) in out[-1]

    def test_pages_come_before_hosts_in_the_tool(self, monkeypatch):
        monkeypatch.setenv("ZABBIX_RESPONSE_BUDGET", "1500")
        hosts = [{"hostid": str(9000 + i), "host": f"srv-aq{9000 + i}", "name": "", "status": "0",
                  "groups": [{"name": "app_free"}]} for i in range(60)]
        dash = [{"dashboardid": "7", "name": "Board", "pages": [
            {"name": "P1", "widgets": [{"type": "graph", "name": "w", "fields": [
                {"type": "3", "name": "hostid", "value": h["hostid"]} for h in hosts]}]}]}]
        c = RecordingClient({"dashboard.get": dash, "host.get": hosts, "graph.get": []})
        out = run_tool(dashboards, "get_dashboard_detail", c, dashboard_id="7")
        assert out.index("## Pages") < out.index("## Referenced Hosts"), out
        assert "more host(s) not listed" in out
        assert len(out) <= 1500 + 400


class TestRetention:
    def test_periods_parse(self):
        assert retention_seconds("365d") == 365 * 86400
        assert retention_seconds("2w") == 14 * 86400
        assert retention_seconds("12h") == 12 * 3600
        assert retention_seconds("90") == 90
        assert retention_seconds("") is None and retention_seconds("soon") is None

    def test_note_only_when_the_window_predates_retention(self):
        c = RecordingClient({"housekeeping.get": {"hk_events_mode": "1", "hk_events_trigger": "30d"}})
        inside = asyncio.run(event_retention_note(c, NOW - 5 * 86400, NOW))
        beyond = asyncio.run(event_retention_note(c, NOW - 90 * 86400, NOW))
        assert inside == ""
        assert "kept for 30d" in beyond and "deleted by the housekeeper" in beyond

    def test_off_or_unreadable_housekeeping_says_nothing(self):
        off = RecordingClient({"housekeeping.get": {"hk_events_mode": "0", "hk_events_trigger": "30d"}})
        odd = RecordingClient({"housekeeping.get": {"hk_events_mode": "1", "hk_events_trigger": "forever"}})
        assert asyncio.run(event_retention_note(off, NOW - 90 * 86400, NOW)) == ""
        assert asyncio.run(event_retention_note(odd, NOW - 90 * 86400, NOW)) == ""

    def test_resolved_view_carries_the_note(self):
        c = RecordingClient({"housekeeping.get": {"hk_events_mode": "1", "hk_events_trigger": "30d"},
                             "event.get": []})
        out = run_tool(problems, "get_problems", c, include_resolved=True, time_from="120d")
        assert out.startswith("No events found.") and "deleted by the housekeeper" in out, out
