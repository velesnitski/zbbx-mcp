"""Wire test for ``get_host_floods``. Fixtures are synthetic."""

import time

import httpx

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp.tools import floods

NOW = int(time.time())


def _boom(_params):
    raise httpx.ConnectError("unreachable")


def _p(eid, name, sev, age_s, trig, suppressed="0"):
    return {"eventid": eid, "name": name, "severity": str(sev), "clock": str(NOW - age_s),
            "suppressed": suppressed, "objectid": trig}


HOSTS = [
    {"hostid": "1", "host": "srv-aq9001"},
    {"hostid": "2", "host": "srv-aq9001 9002"},     # sub-host of 1
    {"hostid": "3", "host": "srv-bv9001"},
    {"hostid": "4", "host": "srv-hm9001"},
]
# host 1+2: 4 problems (one is a dependent symptom); host 3: 3 problems, old;
# host 4: 1 problem; one suppressed problem on host 4 is dropped.
PROBLEMS = [
    _p("101", "Agent down on srv-aq9001", 4, 7200, "t1"),
    _p("102", "Port 443 closed", 3, 3600, "t2"),
    _p("103", "Symptom of agent", 2, 3600, "t3"),          # depends on t1 → collapsed
    _p("104", "High load on srv-aq9001 9002", 5, 1800, "t4"),
    _p("105", "Disk full", 3, 3600, "t5"),
    _p("201", "Ping loss", 2, 10 * 86400, "t6"),
    _p("202", "Ping loss", 2, 9 * 86400, "t7"),
    _p("203", "Agent down", 4, 8 * 86400, "t8"),
    _p("301", "Disk full", 2, 60, "t9"),
    _p("302", "Planned", 4, 60, "t10", suppressed="1"),
]
EVENT_HOSTS = {"101": "1", "102": "1", "103": "1", "104": "2", "105": "2",
               "201": "3", "202": "3", "203": "3", "301": "4", "302": "4"}
NAMES = {h["hostid"]: h["host"] for h in HOSTS}


def _client(problems=PROBLEMS):
    def events(p):
        return [{"eventid": e, "hosts": [{"hostid": EVENT_HOSTS[e], "host": NAMES[EVENT_HOSTS[e]]}]}
                for e in p["eventids"] if e in EVENT_HOSTS]
    trigs = [{"triggerid": "t3", "dependencies": [{"triggerid": "t1"}]},
             {"triggerid": "t1", "dependencies": []}]
    return RecordingClient({"problem.get": problems, "trigger.get": trigs,
                            "event.get": events, "host.get": HOSTS})


def test_get_host_floods_folds_sub_hosts_and_collapses_symptoms():
    c = _client()
    out = run_tool(floods, "get_host_floods", c, min_problems=3)
    pp = c.sent("problem.get")
    assert pp["severities"] == [2, 3, 4, 5] and pp["recent"] is True and pp["limit"] == 5000
    tp = c.sent("trigger.get")
    assert tp["triggerids"] == sorted(f"t{i}" for i in range(1, 10))
    assert "302" not in c.sent("event.get")["eventids"]           # suppressed dropped
    assert "103" not in c.sent("event.get")["eventids"]           # symptom collapsed
    assert c.sent("host.get")["filter"] == {"status": "0"}
    assert out.startswith("**2 hosts with active flood** (≥3 simultaneous problems, severity ≥ 2)")
    row = next(line for line in out.splitlines() if line.startswith("| srv-aq9001"))
    assert row.startswith("| srv-aq9001 (+1 sub-hosts) | 4 | Disaster | ")
    assert "| 2h | " in row
    # host name is stripped from trigger names; samples are sorted and de-duplicated
    assert row.endswith("| Agent down; Disk full; High load; Port 443 closed |")
    assert "| srv-bv9001 | 3 | High |" in out and "| 10d | Agent down; Ping loss |" in out
    assert out.index("srv-aq9001") < out.index("srv-bv9001")
    assert "srv-hm9001" not in out


def test_get_host_floods_age_filter_cap_and_include_suppressed():
    out = run_tool(floods, "get_host_floods", _client(), min_problems=3, max_age_hours=24)
    assert out.startswith("**1 hosts with active flood**") and "srv-bv9001" not in out
    out = run_tool(floods, "get_host_floods", _client(), min_problems=1, max_results=1,
                   include_suppressed=True, collapse_dependent=False)
    assert out.startswith("**3 hosts with active flood**")
    assert "| srv-aq9001 (+1 sub-hosts) | 5 |" in out
    assert out.endswith("*2 more omitted*")


def test_get_host_floods_no_collapse_skips_trigger_lookup():
    c = _client()
    run_tool(floods, "get_host_floods", c, collapse_dependent=False)
    assert "trigger.get" not in [m for m, _ in c.calls]


def test_get_host_floods_below_threshold_reports_what_was_inspected():
    out = run_tool(floods, "get_host_floods", _client())
    assert out == ("No host floods (need ≥5 simultaneous problems on one host). "
                   "Inspected 8 problem records across 4 hosts.")


def test_get_host_floods_empty_paths():
    c = _client(problems=[])
    out = run_tool(floods, "get_host_floods", c, min_severity=4)
    assert out == "No active problems (severity >= 4)."
    assert [m for m, _ in c.calls] == ["problem.get"]
    # problems exist, but no event resolves to a host
    c = RecordingClient({"problem.get": [_p("9", "x", 3, 60, "")], "event.get": []})
    assert run_tool(floods, "get_host_floods", c) == "No host-bound active problems."


def test_get_host_floods_error():
    out = run_tool(floods, "get_host_floods", RecordingClient({"problem.get": _boom}))
    assert out == "Error: unreachable"
