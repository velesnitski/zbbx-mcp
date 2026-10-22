"""ADR 154: the audit log honours the window it is given, or refuses it.

A datetime with seconds parsed to 0, and 0 meant "no window": a five-hour
question came back as the newest rows of the whole log, with no sign that the
window had been dropped.
"""

from __future__ import annotations

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp.tools import audit


def _client():
    return RecordingClient({"auditlog.get": [], "user.get": []})


def test_a_datetime_with_seconds_becomes_a_window():
    c = _client()
    run_tool(audit, "get_audit_log", c, time_from="2026-09-01 10:00:00", time_till="2026-09-01 14:30:00")
    p = c.sent("auditlog.get")
    assert p["time_from"] == 1788256800 and p["time_till"] == 1788273000


def test_relative_and_epoch_forms_still_work():
    c = _client()
    run_tool(audit, "get_audit_log", c, time_from="1788256800", time_till="24h")
    p = c.sent("auditlog.get")
    assert p["time_from"] == 1788256800 and isinstance(p["time_till"], int)


def test_an_unreadable_window_is_refused_not_dropped():
    c = _client()
    out = run_tool(audit, "get_audit_log", c, time_from="last tuesday")
    assert out.startswith("Invalid time_from:")
    assert not any(m == "auditlog.get" for m, _ in c.calls)
