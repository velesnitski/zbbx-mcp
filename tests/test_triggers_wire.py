"""Wire tests for the trigger tools: exact ``trigger.*`` / ``event.get``
params, rollback journal entries for writes, and rendered output.
Fixtures are synthetic."""

import time

import httpx

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp.tools import triggers

NOW = int(time.time())


class JournalClient(RecordingClient):
    """RecordingClient; write tools journal into ``rollback`` via ``journal``."""


class FailingClient(JournalClient):
    async def call(self, method, params):
        self.calls.append((method, params))
        raise httpx.ConnectError("connection refused")


def _trig(tid, desc, prio="3", value="1", status="0", hosts=("srv-aq9001",), expr="", deps=None):
    t = {
        "triggerid": tid, "description": desc, "priority": prio, "value": value,
        "state": "0", "status": status, "lastchange": str(NOW - 600),
        "expression": expr, "hosts": [{"host": h} for h in hosts],
    }
    if deps is not None:
        t["dependencies"] = deps
    return t


TRIGGERS = [
    _trig("9001", "CPU high", prio="4"),
    _trig("9002", "Disk low", prio="2", value="0", hosts=("srv-bv9002", "srv-bv9003")),
    _trig("9003", "Agent silent", prio="3", status="1", hosts=("srv-hm9001",)),
]


# --- get_triggers ---------------------------------------------------------

class TestGetTriggers:
    def test_happy_path_params_and_rows(self):
        c = RecordingClient({"trigger.get": TRIGGERS})
        out = run_tool(triggers, "get_triggers", c)
        p = c.sent("trigger.get")
        assert p["selectHosts"] == ["host"]
        assert p["limit"] == 50 and p["expandDescription"] is True
        assert p["sortfield"] == "priority" and p["sortorder"] == "DESC"
        for key in ("hostids", "min_severity", "filter", "search", "groupids", "tags",
                    "selectDependencies"):
            assert key not in p
        assert out.startswith("**Found: 3 triggers**")
        assert "**[High]** CPU high [PROBLEM] — srv-aq9001" in out
        assert "Disk low [OK] — srv-bv9002, srv-bv9003" in out
        assert "Agent silent [PROBLEM] [DISABLED] — srv-hm9001" in out
        assert "triggerid: 9002" in out
        assert "more may exist" not in out

    def test_all_filters_reach_the_wire(self):
        c = RecordingClient({
            "hostgroup.get": [{"groupid": "11"}, {"groupid": "12"}],
            "trigger.get": TRIGGERS[:1],
        })
        out = run_tool(
            triggers, "get_triggers", c, host_id="9001", group="app_free",
            tags="env:prod,role", min_severity=3, only_problems=True,
            search="CPU*", max_results=1,
        )
        assert c.sent("hostgroup.get")["filter"] == {"name": ["app_free"]}
        p = c.sent("trigger.get")
        assert p["hostids"] == ["9001"]
        assert p["groupids"] == ["11", "12"]
        assert p["min_severity"] == 3
        assert p["only_true"] is True and p["filter"] == {"value": "1"}
        assert p["search"] == {"description": "CPU*"} and p["searchWildcardsEnabled"] is True
        assert p["evaltype"] == 0
        assert p["tags"] == [
            {"tag": "env", "value": "prod", "operator": 0},
            {"tag": "role", "value": "", "operator": 4},
        ]
        assert "(showing first 1, more may exist)" in out

    def test_unknown_group_short_circuits(self):
        c = RecordingClient({"hostgroup.get": []})
        out = run_tool(triggers, "get_triggers", c, group="app_paid")
        assert out == "Host group 'app_paid' not found."
        assert [m for m, _ in c.calls] == ["hostgroup.get"]

    def test_empty(self):
        c = RecordingClient({"trigger.get": []})
        assert run_tool(triggers, "get_triggers", c) == "No triggers found."

    def test_dependencies_listed_and_truncated(self):
        deps = [{"triggerid": str(9100 + i), "description": f"Parent {i}"} for i in range(7)]
        data = [_trig("9001", "Child", deps=deps), _trig("9002", "Lonely", deps=[])]
        c = RecordingClient({"trigger.get": data})
        out = run_tool(triggers, "get_triggers", c, with_dependencies=True)
        assert c.sent("trigger.get")["selectDependencies"] == ["triggerid", "description"]
        assert "depends on: Parent 0 (9100), Parent 1 (9101)" in out
        assert "Parent 4 (9104) (+2 more)" in out
        assert "Parent 5" not in out
        assert out.count("depends on:") == 1

    def test_api_error(self):
        out = run_tool(triggers, "get_triggers", FailingClient())
        assert out.startswith("Error querying Zabbix:") and "connection refused" in out


# --- get_anomaly_triggers -------------------------------------------------

class TestAnomalyTriggers:
    DATA = [
        _trig("9001", "Traffic anomaly", expr="anomalystl(/srv-aq9001/net.if.in[eth0],1h)>3"),
        _trig("9002", "Plain threshold", expr="last(/srv-aq9001/system.cpu.load)>5"),
        _trig("9003", "D" * 60, value="0", hosts=("srv-bv9002",),
              expr="FORECAST(/srv-bv9002/vfs.fs.size,1h,1d)<0 or baselinedev(/x/y,1h:now/h,\"d\",3)>2"),
    ]

    def test_active_only_filters_by_function(self):
        c = RecordingClient({"trigger.get": self.DATA})
        out = run_tool(triggers, "get_anomaly_triggers", c)
        p = c.sent("trigger.get")
        assert p["filter"] == {"value": "1"} and p["only_true"] is True
        assert p["expandExpression"] is True and p["limit"] == 1000
        assert "**2 anomaly-function trigger(s)** (active only)" in out
        assert "| Traffic anomaly | srv-aq9001 | `anomalystl` | PROBLEM | High |" not in out
        assert "| Traffic anomaly | srv-aq9001 | `anomalystl` | PROBLEM | Average |" in out
        assert "Plain threshold" not in out
        assert ("D" * 47 + "...") in out and ("D" * 48) not in out
        assert "`baselinedev`, `forecast`" in out
        assert "| OK |" in out

    def test_all_states_and_max_results_cap(self):
        c = RecordingClient({"trigger.get": self.DATA})
        out = run_tool(triggers, "get_anomaly_triggers", c, only_active=False, max_results=1)
        p = c.sent("trigger.get")
        assert "filter" not in p and "only_true" not in p
        assert "**1 anomaly-function trigger(s)** (all states)" in out
        assert "D" * 47 not in out

    def test_no_match_and_empty(self):
        c = RecordingClient({"trigger.get": self.DATA[1:2]})
        out = run_tool(triggers, "get_anomaly_triggers", c)
        assert out.startswith("No anomaly-function triggers found currently active.")
        assert "`trendstl()`" in out
        out2 = run_tool(triggers, "get_anomaly_triggers", RecordingClient({"trigger.get": self.DATA[1:2]}),
                        only_active=False)
        assert out2.startswith("No anomaly-function triggers found.")
        assert run_tool(triggers, "get_anomaly_triggers", RecordingClient()) == \
            "No triggers available to inspect."

    def test_api_error(self):
        out = run_tool(triggers, "get_anomaly_triggers", FailingClient())
        assert out.startswith("Error querying Zabbix:")


# --- create / update / delete --------------------------------------------

class TestTriggerWrites:
    def test_create_payload_and_journal(self):
        c = JournalClient({"trigger.create": {"triggerids": ["9501"]}})
        out = run_tool(triggers, "create_trigger", c, description="CPU high",
                       expression="avg(/srv-aq9001/system.cpu.load,5m)>80",
                       priority=4, comments="page on-call")
        assert out == "Trigger created. ID: 9501"
        assert c.sent("trigger.create") == {
            "description": "CPU high",
            "expression": "avg(/srv-aq9001/system.cpu.load,5m)>80",
            "priority": 4,
            "comments": "page on-call",
        }
        assert c.rollback == [("create", "trigger", "9501", "Created trigger 'CPU high'")]

    def test_create_minimal_payload(self):
        c = JournalClient({"trigger.create": {"triggerids": ["9502"]}})
        run_tool(triggers, "create_trigger", c, description="d", expression="e")
        assert c.sent("trigger.create") == {"description": "d", "expression": "e", "priority": 0}

    def test_create_api_error_records_nothing(self):
        c = FailingClient()
        out = run_tool(triggers, "create_trigger", c, description="d", expression="e")
        assert out.startswith("Error creating trigger:")
        assert c.rollback == []

    def test_update_full_payload_snapshot_first(self):
        c = JournalClient()
        out = run_tool(triggers, "update_trigger", c, trigger_id="9001",
                       description="CPU very high", priority=5, status=1, comments="c")
        assert out == "Trigger 9001 updated."
        assert c.rollback == [("update", "trigger", "9001", "Updated trigger 9001")]
        assert c.sent("trigger.update") == {
            "triggerid": "9001", "description": "CPU very high",
            "priority": 5, "status": 1, "comments": "c",
        }

    def test_update_skips_unset_fields(self):
        c = JournalClient()
        run_tool(triggers, "update_trigger", c, trigger_id="9001", priority=0, status=0)
        assert c.sent("trigger.update") == {"triggerid": "9001", "priority": 0, "status": 0}

    def test_update_api_error(self):
        out = run_tool(triggers, "update_trigger", FailingClient(), trigger_id="9001")
        assert out.startswith("Error updating trigger:")

    def test_delete_payload_and_journal(self):
        c = JournalClient()
        out = run_tool(triggers, "delete_trigger", c, trigger_id="9003")
        assert out == "Trigger 9003 deleted."
        assert c.sent("trigger.delete") == ["9003"]
        assert c.rollback == [("delete", "trigger", "9003", "Deleted trigger 9003")]

    def test_delete_api_error(self):
        out = run_tool(triggers, "delete_trigger", FailingClient(), trigger_id="9003")
        assert out.startswith("Error deleting trigger:")


# --- get_trigger_timeline -------------------------------------------------

def _ev(eid, clock, value, ack="0"):
    return {"eventid": eid, "clock": str(clock), "value": value, "acknowledged": ack}


class TestTriggerTimeline:
    TRIG = [{"triggerid": "9001", "description": "CPU high", "priority": "4",
             "hosts": [{"host": "srv-aq9001"}]}]
    EVENTS = [
        _ev("e1", NOW - 3000, "1"),
        _ev("e2", NOW - 2000, "1", ack="1"),   # repeat: not a transition
        _ev("e3", NOW - 1000, "0"),
        _ev("e4", NOW - 500, "1"),
    ]

    def test_happy_path(self):
        c = RecordingClient({"trigger.get": self.TRIG, "event.get": self.EVENTS})
        out = run_tool(triggers, "get_trigger_timeline", c, trigger_id="9001", hours=2)
        tp = c.sent("trigger.get")
        assert tp["triggerids"] == ["9001"] and tp["expandDescription"] is True
        ep = c.sent("event.get")
        assert ep["objectids"] == ["9001"] and ep["source"] == 0 and ep["object"] == 0
        assert ep["time_till"] - ep["time_from"] == 2 * 3600
        assert ep["limit"] == 100 and ep["sortorder"] == ["ASC"]
        assert "# Timeline: CPU high" in out
        assert "**Hosts:** srv-aq9001" in out
        assert "**Severity:** High" in out
        assert "**Transitions:** 3" in out
        assert "| PROBLEM | e2 | yes |" in out and "| OK | e3 | no |" in out
        assert "limit" not in out.split("| e4 |")[1]

    def test_explicit_window_and_limit_note(self):
        c = RecordingClient({"trigger.get": self.TRIG, "event.get": self.EVENTS[:2]})
        out = run_tool(triggers, "get_trigger_timeline", c, trigger_id="9001",
                       time_from="1700000000", time_till="1700086400", max_results=2)
        ep = c.sent("event.get")
        assert ep["time_from"] == 1700000000 and ep["time_till"] == 1700086400
        assert "*limit 2 reached" in out

    def test_bad_times(self):
        c = RecordingClient()
        assert run_tool(triggers, "get_trigger_timeline", c, trigger_id="1",
                        time_from="not-a-time").startswith("Invalid time_from:")
        assert run_tool(triggers, "get_trigger_timeline", c, trigger_id="1",
                        time_till="not-a-time").startswith("Invalid time_till:")
        assert c.calls == []

    def test_unknown_trigger_and_no_events(self):
        c = RecordingClient({"trigger.get": []})
        assert run_tool(triggers, "get_trigger_timeline", c, trigger_id="404") == \
            "Trigger '404' not found."
        c2 = RecordingClient({"trigger.get": self.TRIG, "event.get": []})
        out = run_tool(triggers, "get_trigger_timeline", c2, trigger_id="9001")
        assert out.startswith("No transitions for trigger 'CPU high' in window (")

    def test_api_error(self):
        out = run_tool(triggers, "get_trigger_timeline", FailingClient(), trigger_id="9001")
        assert out.startswith("Error:") and "connection refused" in out
