"""Wire tests for ``get_external_ip_history`` / ``get_recovery_score``.
Fixtures are synthetic: documentation-range addresses only."""

import json
import time
from datetime import datetime, timezone

import httpx

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp.tools import ip_history

NOW = int(time.time())
DAY = 86400


def _boom(_params):
    raise httpx.ConnectError("unreachable")


def _details(old, new):
    return json.dumps([["update", "interfaces.1.ip", old, new]])


HOSTS = [
    {"hostid": "1", "host": "srv-aq9001", "interfaces": [{"ip": "192.0.2.11"}]},
    {"hostid": "2", "host": "srv-aq9002", "interfaces": [{"ip": "192.0.2.12"}]},
    {"hostid": "3", "host": "srv-bv9001", "interfaces": [{"ip": "198.51.100.13"}]},
    {"hostid": "4", "host": "srv-hm9001", "interfaces": [{"ip": "203.0.113.14"}]},
]
ROT = {"1": NOW - 3 * DAY, "2": NOW - 2 * DAY, "3": NOW - 5 * DAY, "4": NOW - 1 * DAY}
AUDIT = {
    "1": [{"clock": str(ROT["1"]), "details": _details("192.0.2.1", "192.0.2.11")},
          {"clock": str(ROT["1"] + 60), "details": json.dumps({"name": ["update", "a", "b"]})}],
    "2": [{"clock": str(ROT["2"]), "details": _details("192.0.2.4", "192.0.2.12")}],
    "3": [{"clock": str(ROT["3"]), "details": json.dumps(
        {"interfaces.7.ip": ["update", "198.51.100.3", "198.51.100.13"]})}],
    "4": [{"clock": str(ROT["4"]), "details": _details("203.0.113.4", "203.0.113.14")}],
}
# (pre, post) average bps per host; None = no trend rows in that window
TRAFFIC = {"1": (100e6, 90e6), "2": (100e6, 50e6), "3": (100e6, 10e6), "4": (None, 80e6)}


def _client(hosts=HOSTS, batched=False):
    def auditlog(p):
        rid = p["filter"]["resourceid"]
        if isinstance(rid, list):
            return [dict(r, resourceid=h) for h in rid for r in AUDIT.get(h, [])]
        return AUDIT.get(rid, [])

    def items(p):
        hid = p["hostids"][0]
        return [{"itemid": f"i{hid}"}] if hid != "9" else []

    def trends(p):
        hid = p["itemids"][0][1:]
        rot = ROT[hid]
        pre, post = TRAFFIC[hid]
        v = pre if p["time_till"] == rot else post
        if v is None:
            return []
        return [{"clock": str(p["time_from"]), "value_avg": str(v)},
                {"clock": str(p["time_from"] + 3600), "value_avg": "junk"}]
    return RecordingClient({"host.get": hosts, "auditlog.get": auditlog,
                            "item.get": items, "trend.get": trends})


def _when(clock):
    return datetime.fromtimestamp(clock, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def test_get_external_ip_history_scores_each_rotation():
    c = _client()
    out = run_tool(ip_history, "get_external_ip_history", c)
    ap = c.sent("auditlog.get")
    assert ap["filter"] == {"resourcetype": 4, "action": 1, "resourceid": "1"}
    assert abs(ap["time_from"] - (int(time.time()) - 30 * DAY)) <= 5
    tp = c.sent("trend.get")
    assert tp["itemids"] == ["i1"] and tp["time_from"] == ROT["1"] - DAY and tp["time_till"] == ROT["1"]
    assert c.sent("item.get")["filter"]["status"] == "0"
    assert out.startswith("**4 external-IP rotations** (last 30d, 4 hosts inspected)")
    assert f"| srv-aq9001 | {_when(ROT['1'])} | 192.0.2.1 | 192.0.2.11 | 100.0 | 90.0 | recovered |" in out
    assert "| 192.0.2.4 | 192.0.2.12 | 100.0 | 50.0 | partial |" in out
    assert "| 198.51.100.3 | 198.51.100.13 | 100.0 | 10.0 | still-down |" in out
    assert "| 203.0.113.4 | 203.0.113.14 | — | 80.0 | n/a |" in out
    assert out.endswith("*Outcomes: 1 recovered, 1 partial, 1 still-down, 1 n/a (rate: 33%).*")


def test_get_external_ip_history_skips_unclocked_audit_rows(monkeypatch):
    monkeypatch.setitem(AUDIT, "3", AUDIT["3"] + [
        {"clock": "0", "details": _details("198.51.100.5", "198.51.100.6")}])
    out = run_tool(ip_history, "get_external_ip_history", _client(), host="srv-bv9001")
    assert out.startswith("**1 external-IP rotations**") and "198.51.100.6" not in out


def test_get_external_ip_history_host_and_country_filters():
    c = _client()
    out = run_tool(ip_history, "get_external_ip_history", c, host="srv-bv9001")
    assert out.startswith("**1 external-IP rotations** (last 30d, 1 hosts inspected)")
    out = run_tool(ip_history, "get_external_ip_history", _client(), country="aq", max_hosts=1)
    assert "(last 30d, 1 hosts inspected)" in out and "srv-aq9001" in out and "srv-aq9002" not in out


def test_get_external_ip_history_no_rotations_and_no_hosts():
    hosts = [{"hostid": "5", "host": "srv-tf9001", "interfaces": []}]
    out = run_tool(ip_history, "get_external_ip_history", _client(hosts=hosts), window_days=7)
    assert out == "No external-IP rotations found in the last 7d (1 hosts inspected)."
    out = run_tool(ip_history, "get_external_ip_history", _client(), host="srv-gs9001")
    assert out == "No matching hosts."


def test_get_external_ip_history_missing_items_gives_na_and_error():
    c = _client()
    c._responses["item.get"] = []
    out = run_tool(ip_history, "get_external_ip_history", c, host="srv-aq9001")
    assert "| — | — | n/a |" in out and "(rate: —)" in out
    assert "trend.get" not in [m for m, _ in c.calls]
    out = run_tool(ip_history, "get_external_ip_history", RecordingClient({"host.get": _boom}))
    assert out == "Error: unreachable"


def test_get_recovery_score_batches_audit_query():
    c = _client()
    out = run_tool(ip_history, "get_recovery_score", c, window_days=10)
    ap = c.sent("auditlog.get")
    assert ap["filter"]["resourceid"] == ["1", "2", "3", "4"] and ap["limit"] == 5000
    assert [m for m, _ in c.calls].count("auditlog.get") == 1
    assert out.startswith("**Recovery KPI** (last 10d, 4 hosts)")
    assert "- **Total rotations:** 4" in out
    assert "- **Recovered:** 1\n- **Partial:** 1\n- **Still-down:** 1\n- **N/A (insufficient trend):** 1" in out
    assert "- **Recovery rate:** 33.3% (recovered / determined-outcome)" in out


def test_get_recovery_score_country_filter_empty_and_error():
    out = run_tool(ip_history, "get_recovery_score", _client(), country="bv")
    assert "(last 7d, 1 hosts)" in out and "- **Still-down:** 1" in out
    assert run_tool(ip_history, "get_recovery_score", _client(), country="pn") == "No matching hosts."
    c = _client()
    c._responses["auditlog.get"] = [{"clock": "x", "details": _details("192.0.2.1", "192.0.2.2"),
                                     "resourceid": "1"}]
    out = run_tool(ip_history, "get_recovery_score", c)
    assert out == "No IP rotations found in the last 7d (4 hosts inspected)."
    out = run_tool(ip_history, "get_recovery_score", RecordingClient({"host.get": _boom}))
    assert out == "Error: unreachable"
