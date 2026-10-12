"""Wire tests for the problem tools not covered by the host-column suite:
timeline, resolved events, filters, problem detail and the write tools
(acknowledge / bulk / rank). Fixtures are synthetic."""

import time

import httpx
import pytest

from tests.wiretest import CaptureMCP, RecordingClient, StubResolver, run_tool
from zbbx_mcp.formatters import _ts
from zbbx_mcp.tools import WRITE_TOOLS, problems

NOW = int(time.time())


def _boom(_params):
    raise httpx.ConnectError("unreachable")


# ------------------------------------------------------------- event timeline

def test_timeline_with_recovery():
    def events(p):
        if p["eventids"] == ["100"]:
            return [{"eventid": "100", "name": "Agent down", "severity": "4",
                     "clock": "1700000000", "value": "1", "r_eventid": "101"}]
        return [{"eventid": "101", "clock": "1700003600", "value": "0"}]
    c = RecordingClient({"event.get": events})
    out = run_tool(problems, "get_problems", c, event_eventid="100")
    assert [p["eventids"] for m, p in c.calls] == [["100"], ["101"]]
    assert out.startswith("# Timeline: Agent down")
    assert "**Severity:** High" in out
    assert f"| {_ts('1700000000')} | PROBLEM | 100 |" in out
    assert f"| {_ts('1700003600')} | OK | 101 |" in out


def test_timeline_ongoing_and_missing():
    c = RecordingClient({"event.get": [{"eventid": "7", "name": "Disk", "severity": "2",
                                        "clock": "1700000000", "r_eventid": "0"}]})
    out = run_tool(problems, "get_problems", c, event_eventid="7")
    assert out.endswith("| *(ongoing)* | — | — |") and len(c.calls) == 1
    out = run_tool(problems, "get_problems", RecordingClient(), event_eventid="8")
    assert out == "Event '8' not found."


def test_bad_time_inputs_are_refused():
    out = run_tool(problems, "get_problems", RecordingClient(), time_from="soonish")
    assert out.startswith("Invalid time_from:")
    out = run_tool(problems, "get_problems", RecordingClient(), time_till="later")
    assert out.startswith("Invalid time_till:")


# ------------------------------------------------------------ resolved events

EVENTS = [
    {"eventid": "1", "name": "Agent down", "severity": "4", "clock": str(NOW - 600), "value": "1",
     "acknowledged": "1", "hosts": [{"hostid": "10", "host": "srv-aq9001"}]},
    {"eventid": "2", "name": "Agent down", "severity": "4", "clock": str(NOW - 300), "value": "0",
     "acknowledged": "0", "hosts": []},
    {"eventid": "3", "name": "Disk", "severity": "9", "clock": str(NOW - 100), "value": "5"},
]


def test_resolved_events_with_filters_and_retention_note():
    c = RecordingClient({
        "event.get": EVENTS,
        "host.get": [{"hostid": "10"}],
        "hostgroup.get": [{"groupid": "5"}],
        "housekeeping.get": {"hk_events_mode": "1", "hk_events_trigger": "7d"},
    })
    out = run_tool(problems, "get_problems", c, include_resolved=True, host="srv-aq9001",
                   group="app_free", severity_min=3, acknowledged="yes",
                   time_from="30d", time_till=str(NOW), max_results=3)
    p = c.sent("event.get")
    assert p["severities"] == [3, 4, 5] and p["acknowledged"] is True
    assert p["hostids"] == ["10"] and p["groupids"] == ["5"]
    assert p["time_till"] == NOW and abs(p["time_from"] - (int(time.time()) - 30 * 86400)) <= 5
    assert p["source"] == 0 and p["object"] == 0 and p["limit"] == 3
    assert out.startswith("**Found: 3 events** (showing first 3, more may exist)")
    assert "Event history is kept for 7d" in out
    assert "- **[High]** `srv-aq9001` · Agent down [PROBLEM] [ACK]" in out
    assert "- **[High]** Agent down [OK] —" in out
    assert "Disk [?]" in out


def test_resolved_events_host_only_group_only_and_unknown():
    c = RecordingClient({"event.get": EVENTS[:1], "host.get": [{"hostid": "10"}]})
    out = run_tool(problems, "get_problems", c, include_resolved=True, host="srv-aq9001",
                   acknowledged="no")
    assert c.sent("event.get")["hostids"] == ["10"] and c.sent("event.get")["acknowledged"] is False
    assert out.startswith("**Found: 1 events**\n")
    c = RecordingClient({"event.get": [], "hostgroup.get": [{"groupid": "5"}]})
    out = run_tool(problems, "get_problems", c, include_resolved=True, group="app_free")
    assert c.sent("event.get")["groupids"] == ["5"]
    assert out == "No events found."
    assert run_tool(problems, "get_problems", RecordingClient(), include_resolved=True,
                    host="srv-pn9001") == "Host 'srv-pn9001' not found."
    assert run_tool(problems, "get_problems", RecordingClient(), include_resolved=True,
                    group="app_none") == "Host group 'app_none' not found."


def test_resolved_events_host_and_group_not_found():
    c = RecordingClient({"hostgroup.get": [{"groupid": "5"}]})
    assert run_tool(problems, "get_problems", c, include_resolved=True, host="srv-pn9001",
                    group="app_free") == "Host 'srv-pn9001' not found."
    c = RecordingClient({"host.get": [{"hostid": "10"}]})
    assert run_tool(problems, "get_problems", c, include_resolved=True, host="srv-aq9001",
                    group="app_none") == "Host group 'app_none' not found."


def test_resolved_events_empty_window_still_carries_retention_note():
    c = RecordingClient({"event.get": [],
                         "housekeeping.get": {"hk_events_mode": "1", "hk_events_trigger": "1d"}})
    out = run_tool(problems, "get_problems", c, include_resolved=True, time_from="7d")
    assert out.startswith("No events found.\n\n_Event history is kept for 1d")


@pytest.mark.parametrize("hk", [
    {"hk_events_mode": "0", "hk_events_trigger": "1d"},     # housekeeping off
    {"hk_events_mode": "1", "hk_events_trigger": "soon"},   # unreadable period
    {"hk_events_mode": "1", "hk_events_trigger": "365d"},   # window inside retention
    [],                                                     # unexpected shape
])
def test_retention_note_absent_when_not_applicable(hk):
    c = RecordingClient({"event.get": [], "housekeeping.get": hk})
    out = run_tool(problems, "get_problems", c, include_resolved=True, time_from="7d")
    assert out == "No events found."


def test_retention_note_absent_when_housekeeping_unreadable():
    c = RecordingClient({"event.get": [], "housekeeping.get": _boom})
    assert run_tool(problems, "get_problems", c, include_resolved=True, time_from="7d") == "No events found."


# ------------------------------------------------------- get_problems filters

def test_active_problems_with_tags_ack_window_and_group():
    plist = [{"eventid": "1", "objectid": "t1", "name": "Disk", "severity": "3",
              "clock": str(NOW - 60), "acknowledged": "0", "suppressed": "0"}]
    c = RecordingClient({"problem.get": plist, "hostgroup.get": [{"groupid": "5"}],
                         "trigger.get": [{"triggerid": "t1", "hosts": [{"host": "srv-aq9001"}]}]})
    out = run_tool(problems, "get_problems", c, tags="role:edge", acknowledged="no",
                   time_from="1h", time_till=str(NOW), group="app_free", severity_min=3, max_results=1)
    p = c.sent("problem.get")
    assert p["tags"] == [{"tag": "role", "value": "edge", "operator": 0}]
    assert p["evaltype"] == 0 and p["acknowledged"] is False and p["groupids"] == ["5"]
    assert p["severities"] == [3, 4, 5] and p["time_till"] == NOW
    assert out.startswith("**Found: 1 problems** (showing first 1, more may exist)")
    c = RecordingClient({"problem.get": []})
    run_tool(problems, "get_problems", c, acknowledged="yes")
    assert c.sent("problem.get")["acknowledged"] is True and "severities" not in c.sent("problem.get")


def test_active_problems_host_and_group_paths():
    c = RecordingClient({"host.get": [{"hostid": "10"}], "hostgroup.get": [{"groupid": "5"}],
                         "problem.get": []})
    out = run_tool(problems, "get_problems", c, host="srv-aq9001", group="app_free")
    p = c.sent("problem.get")
    assert p["hostids"] == ["10"] and p["groupids"] == ["5"]
    assert out == "No problems found."
    c = RecordingClient({"host.get": [{"hostid": "10"}], "hostgroup.get": []})
    assert run_tool(problems, "get_problems", c, host="srv-aq9001", group="app_none") == \
        "Host group 'app_none' not found."
    assert run_tool(problems, "get_problems", RecordingClient(), host="srv-pn9001", group="x") == \
        "Host 'srv-pn9001' not found."
    assert run_tool(problems, "get_problems", RecordingClient(), group="app_none") == \
        "Host group 'app_none' not found."


def test_sub_host_with_no_problems_names_the_parent():
    def host_get(p):
        return [{"hostid": "11" if p["filter"]["host"] == ["srv-aq9001 9002"] else "10"}]
    c = RecordingClient({"host.get": host_get, "problem.get": []})
    out = run_tool(problems, "get_problems", c, host="srv-aq9001 9002")
    assert c.sent("problem.get")["hostids"] == ["11", "10"]
    assert out == "No problems found on `srv-aq9001 9002` or its parent `srv-aq9001`."


def test_active_problems_error():
    out = run_tool(problems, "get_problems", RecordingClient({"problem.get": _boom}))
    assert out == "Error querying Zabbix: unreachable"


# ---------------------------------------------------------- get_problem_detail

def test_problem_detail_full():
    detail = [{
        "eventid": "55", "name": "Agent down", "severity": "5", "clock": "1700000000",
        "acknowledged": "1", "suppressed": "1",
        "suppression_data": [{"maintenanceid": "0", "suppress_until": "0"},
                             {"maintenanceid": "9", "suppress_until": "0"},
                             {"maintenanceid": "0", "suppress_until": str(NOW + 7200 + 300)},
                             {"maintenanceid": "0", "suppress_until": str(NOW - 10)},
                             {"maintenanceid": "0", "suppress_until": "x"}],
        "cause_eventid": "50",
        "tags": [{"tag": "role", "value": "edge"}],
        "acknowledges": [{"userid": "3", "clock": "1700000100", "message": "looking"},
                         {"userid": "4", "clock": "1700000200", "message": "fixed"},
                         {"userid": "", "clock": "1700000300", "message": "?"}],
    }]
    c = RecordingClient({"problem.get": detail, "user.get": [{"userid": "3", "username": "ops-a"}]})
    out = run_tool(problems, "get_problem_detail", c, problem_id="55")
    p = c.sent("problem.get")
    assert p["selectAcknowledges"] == ["userid", "clock", "message", "action"]
    assert c.sent("user.get")["userids"] == ["3", "4"]
    assert out.startswith("# Problem: Agent down")
    assert "**Severity:** Disaster" in out and "**Acknowledged:** Yes" in out and "**Suppressed:** Yes" in out
    assert ("**Suppression:** snoozed until the problem resolves; maintenance window (id 9); "
            "snoozed for 2h 0") in out
    assert "snooze lapsed (pending unsuppression)" in out
    assert "**Rank:** symptom of cause event 50" in out
    assert "## Tags\n- role: edge" in out
    assert "## Acknowledgements (3)" in out
    assert f"- **ops-a** ({_ts('1700000100')}): looking" in out
    assert "- **user 4** (" in out and "- **?** (" in out


def test_problem_detail_minimal_and_user_lookup_denied():
    c = RecordingClient({"problem.get": [{"eventid": "56", "name": "Disk", "severity": "2",
                                          "acknowledges": [{"userid": "3", "clock": "0", "message": "m"}]}],
                         "user.get": _boom})
    out = run_tool(problems, "get_problem_detail", c, event_id="56")
    assert "**Acknowledged:** No" in out and "**Suppression:**" not in out and "**Rank:**" not in out
    assert "- **user 3** (" in out


def test_problem_detail_guards():
    assert run_tool(problems, "get_problem_detail", RecordingClient()) == \
        "Either problem_id or event_id is required."
    assert run_tool(problems, "get_problem_detail", RecordingClient(), event_id="9") == \
        "Problem with eventid '9' not found."
    out = run_tool(problems, "get_problem_detail", RecordingClient({"problem.get": _boom}), problem_id="9")
    assert out == "Error querying Zabbix: unreachable"


# ------------------------------------------------------------- write tools

def test_acknowledge_problem_payload_and_summary():
    c = RecordingClient({"event.acknowledge": {"eventids": ["9"]}})
    out = run_tool(problems, "acknowledge_problem", c, event_id="9", message="on it",
                   close=True, severity=4, suppress_hours=2)
    p = c.sent("event.acknowledge")
    assert p["eventids"] == ["9"] and p["message"] == "on it" and p["severity"] == 4
    assert p["action"] == 2 | 1 | 4 | 8 | 32
    assert abs(p["suppress_until"] - (int(time.time()) + 7200)) <= 5
    assert out == ("Problem 9 acknowledged. Marked for closing. Severity set to 4. "
                   "Snoozed for 2h. Message: on it")
    assert c.rollback == []                 # acknowledgements are not journalled


def test_acknowledge_problem_unack_indefinite_and_unsuppress():
    c = RecordingClient()
    out = run_tool(problems, "acknowledge_problem", c, event_id="9", unack=True, suppress_hours=-1)
    p = c.sent("event.acknowledge")
    assert p == {"eventids": ["9"], "action": 16 | 32, "message": "", "suppress_until": 0}
    assert out == "Problem 9 unacknowledged. Snoozed until the problem resolves."
    c = RecordingClient()
    out = run_tool(problems, "acknowledge_problem", c, event_id="9", unsuppress=True)
    assert c.sent("event.acknowledge")["action"] == 2 | 64
    assert out == "Problem 9 acknowledged. Suppression lifted."


def test_acknowledge_problem_guard_and_error():
    c = RecordingClient()
    out = run_tool(problems, "acknowledge_problem", c, event_id="9", suppress_hours=1, unsuppress=True)
    assert out == "suppress_hours and unsuppress are mutually exclusive." and c.calls == []
    out = run_tool(problems, "acknowledge_problem", RecordingClient({"event.acknowledge": _boom}), event_id="9")
    assert out == "Error querying Zabbix: unreachable"


def test_bulk_acknowledge_payload_and_summary():
    c = RecordingClient()
    out = run_tool(problems, "bulk_acknowledge", c, event_ids="1, 2 3", message="wave",
                   close=True, severity=2, suppress_hours=-1)
    p = c.sent("event.acknowledge")
    assert p == {"eventids": ["1", "2", "3"], "action": 2 | 1 | 4 | 8 | 32,
                 "message": "wave", "severity": 2, "suppress_until": 0}
    assert out == ("Acknowledged 3 event(s). Marked for closing. Severity set to 2. "
                   "Snoozed until each resolves. Message: wave")


def test_bulk_acknowledge_variants_guards_and_error():
    c = RecordingClient()
    out = run_tool(problems, "bulk_acknowledge", c, event_ids="4", unack=True, suppress_hours=1.5)
    assert c.sent("event.acknowledge")["action"] == 16 | 32
    assert out == "Unacknowledged 1 event(s). Snoozed for 1.5h."
    c = RecordingClient()
    assert run_tool(problems, "bulk_acknowledge", c, event_ids="4", unsuppress=True) == \
        "Acknowledged 1 event(s). Suppression lifted."
    assert run_tool(problems, "bulk_acknowledge", RecordingClient(), event_ids=" , ") == "No event IDs provided."
    assert run_tool(problems, "bulk_acknowledge", RecordingClient(), event_ids="1",
                    suppress_hours=1, unsuppress=True) == "suppress_hours and unsuppress are mutually exclusive."
    out = run_tool(problems, "bulk_acknowledge", RecordingClient({"event.acknowledge": _boom}), event_ids="1")
    assert out == "Error querying Zabbix: unreachable"


def test_rank_problem_cause_payloads():
    c = RecordingClient()
    out = run_tool(problems, "rank_problem_cause", c, symptom_event_ids="2,3",
                   cause_event_id="1", message="same wave")
    assert c.sent("event.acknowledge") == {"eventids": ["2", "3"], "action": 256 | 4,
                                           "message": "same wave", "cause_eventid": "1"}
    assert out == "Ranked 2 event(s) as symptoms of cause 1."
    c = RecordingClient()
    out = run_tool(problems, "rank_problem_cause", c, symptom_event_ids="2", unrank=True)
    assert c.sent("event.acknowledge") == {"eventids": ["2"], "action": 128}
    assert out == "Ranked 1 event(s) back to independent cause."


def test_rank_problem_cause_guards_and_error():
    r = RecordingClient()
    assert run_tool(problems, "rank_problem_cause", r, symptom_event_ids="") == "No symptom event IDs provided."
    assert run_tool(problems, "rank_problem_cause", r, symptom_event_ids="2") == \
        "cause_event_id is required (or set unrank=True)."
    assert run_tool(problems, "rank_problem_cause", r, symptom_event_ids="2", cause_event_id="1",
                    unrank=True) == "unrank does not take a cause_event_id."
    assert r.calls == []
    out = run_tool(problems, "rank_problem_cause", RecordingClient({"event.acknowledge": _boom}),
                   symptom_event_ids="2", cause_event_id="1")
    assert out == "Error querying Zabbix: unreachable"


def test_read_only_mode_does_not_register_write_tools():
    mcp = CaptureMCP()
    problems.register(mcp, StubResolver(RecordingClient()), skip=set(WRITE_TOOLS))
    assert set(mcp.fns) == {"get_problems", "get_problem_detail"}
