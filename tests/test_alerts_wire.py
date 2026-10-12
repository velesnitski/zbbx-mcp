"""Wire tests for ``get_alerts`` / ``get_alert_summary``. Fixtures are synthetic."""

import time

import httpx

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp.formatters import _ts
from zbbx_mcp.tools import alerts

NOW = int(time.time())


def _boom(_params):
    raise httpx.ConnectError("unreachable")


ALERTS = [
    {"alertid": "1", "alerttype": "0", "clock": str(NOW - 60), "status": "1",
     "subject": "Disk full on srv-aq9001", "sendto": "ops@example.com"},
    {"alertid": "2", "alerttype": "1", "clock": str(NOW - 120), "status": "2",
     "subject": "Restart agent", "sendto": "srv-bv9001", "error": "timeout talking to agent"},
    {"alertid": "3", "alerttype": "9", "clock": str(NOW - 180), "status": "7",
     "subject": "Odd", "sendto": "x"},
]


def test_get_alerts_renders_rows():
    c = RecordingClient({"alert.get": ALERTS})
    out = run_tool(alerts, "get_alerts", c)
    p = c.sent("alert.get")
    assert p["limit"] == 50 and p["sortorder"] == "DESC"
    assert not {"hostids", "time_from", "time_till", "filter"} & set(p)
    assert out.startswith("**Found: 3 alerts**\n")
    assert ("- **[Sent]** Disk full on srv-aq9001\n  Message → ops@example.com | "
            f"{_ts(str(NOW - 60))} (alertid: 1)") in out
    assert "- **[Failed]** Restart agent\n  Remote command → srv-bv9001" in out
    assert "\n  Error: timeout talking to agent" in out
    assert "- **[?]** Odd\n  ? → x" in out


def test_get_alerts_filters_and_cap_header():
    c = RecordingClient({"alert.get": ALERTS[:2]})
    out = run_tool(alerts, "get_alerts", c, host_id="10", time_from="100", time_till="200",
                   status="sent", max_results=2)
    p = c.sent("alert.get")
    assert p["hostids"] == ["10"] and p["time_from"] == 100 and p["time_till"] == 200
    assert p["filter"] == {"status": "1"} and p["limit"] == 2
    assert out.startswith("**Found: 2 alerts** (showing last 2)")
    c = RecordingClient({"alert.get": []})
    assert run_tool(alerts, "get_alerts", c, status="failed") == "No alerts found."
    assert c.sent("alert.get")["filter"] == {"status": "2"}


def test_get_alerts_bad_timestamp_and_api_error():
    out = run_tool(alerts, "get_alerts", RecordingClient(), time_from="yesterday")
    assert out.startswith("Error querying Zabbix: invalid literal")
    out = run_tool(alerts, "get_alerts", RecordingClient({"alert.get": _boom}))
    assert out == "Error querying Zabbix: unreachable"


def _a(age_h, status, subject):
    return {"alertid": f"{age_h}{status}", "clock": str(NOW - int(age_h * 3600)),
            "status": status, "subject": subject, "alerttype": "0"}


SUMMARY = [
    _a(1, "1", "Disk full"), _a(2, "1", "Disk full"), _a(3, "2", "Ping loss"),
    _a(30, "1", "Disk full"), _a(31, "2", "Ping loss"), _a(32, "2", "Ping loss"),
    _a(33, "3", "Old"), _a(34, "3", "Old"),
]


def test_get_alert_summary_compares_periods():
    c = RecordingClient({"alert.get": SUMMARY})
    out = run_tool(alerts, "get_alert_summary", c)
    p = c.sent("alert.get")
    assert "clock" in p["output"]                          # ADR 096
    assert abs(p["time_from"] - (int(time.time()) - 48 * 3600)) <= 5
    assert out.startswith("**Alerts (last 24h): 3 (-2 vs prev 24h)**")
    assert "| Sent | 2 | 1 | +1 |" in out
    assert "| Failed | 1 | 2 | -1 |" in out
    assert "| New | 0 | 2 | -2 |" in out
    assert "| Disk full | 2 |" in out and "| Ping loss | 1 |" in out and "| Old |" not in out


def test_get_alert_summary_growth_arrow():
    data = [_a(1, "1", "A"), _a(2, "1", "A"), _a(30, "1", "A")]
    out = run_tool(alerts, "get_alert_summary", RecordingClient({"alert.get": data}))
    assert out.startswith("**Alerts (last 24h): 2 (+1 vs prev 24h)**")


def test_get_alert_summary_without_compare():
    c = RecordingClient({"alert.get": SUMMARY[:3]})
    out = run_tool(alerts, "get_alert_summary", c, hours=6, compare=False)
    assert abs(c.sent("alert.get")["time_from"] - (int(time.time()) - 6 * 3600)) <= 5
    assert out.startswith("**Alerts (last 6h): 3**")
    assert "| Status | Count |" in out and "| Sent | 2 |" in out and "| Failed | 1 |" in out
    assert "Previous" not in out


def test_get_alert_summary_empty_and_error():
    out = run_tool(alerts, "get_alert_summary", RecordingClient({"alert.get": SUMMARY[3:]}))
    assert out == "No alerts in the last 24 hours."
    out = run_tool(alerts, "get_alert_summary", RecordingClient({"alert.get": _boom}))
    assert out == "Error querying Zabbix: unreachable"
