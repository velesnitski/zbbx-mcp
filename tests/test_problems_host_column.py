"""ADR 146: a problem is listed with the host it belongs to, and asking about
a sub-host asks about its machine.

``problem.get`` cannot select hosts, so a group listing of twelve problems
carried no host at all and could not be reconciled with any per-host view.
A VIP sub-host (``parent label``) owns only its per-address checks; the
agent, CPU and memory triggers live on the parent. Fixtures are neutral.
"""

from __future__ import annotations

import time

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp.tools import problems
from zbbx_mcp.tools.problems import compound_parent

NOW = int(time.time())

HOSTS = [
    {"hostid": "9001", "host": "srv-aq9001"},
    {"hostid": "9002", "host": "srv-aq9001 aq9002"},   # VIP sub-host of 9001
    {"hostid": "9003", "host": "srv-bv9003"},
]
PROBLEMS = [
    {"eventid": "1", "objectid": "t1", "name": "agent unreachable", "severity": "4", "clock": str(NOW - 600),
     "acknowledged": "0", "suppressed": "0", "_hostids": ["9001"]},
    {"eventid": "2", "objectid": "t2", "name": "service check failed", "severity": "3", "clock": str(NOW - 300),
     "acknowledged": "0", "suppressed": "0", "_hostids": ["9002"]},
    {"eventid": "3", "objectid": "t3", "name": "disk low", "severity": "2", "clock": str(NOW - 100),
     "acknowledged": "1", "suppressed": "0", "_hostids": ["9003"]},
]
TRIGGER_HOSTS = {"t1": ["9001"], "t2": ["9002"], "t3": ["9003"]}


def _client():
    by_id = {h["hostid"]: h for h in HOSTS}

    def host_get(p):
        if "filter" in p:
            return [h for h in HOSTS if h["host"] in p["filter"]["host"]]
        if "search" in p:
            return []
        return HOSTS

    def problem_get(p):
        hids = p.get("hostids")
        rows = [dict(x) for x in PROBLEMS if not hids or any(h in hids for h in x["_hostids"])]
        for r in rows:
            r.pop("_hostids")
        return rows

    def trigger_get(p):
        return [{"triggerid": t, "hosts": [{"hostid": h, "host": by_id[h]["host"]} for h in TRIGGER_HOSTS[t]]}
                for t in p["triggerids"]]

    def event_get(p):
        hids = p.get("hostids")
        rows = []
        for x in PROBLEMS:
            if hids and not any(h in hids for h in x["_hostids"]):
                continue
            rows.append({"eventid": x["eventid"], "name": x["name"], "severity": x["severity"],
                         "clock": x["clock"], "value": "1", "acknowledged": x["acknowledged"],
                         "hosts": [{"hostid": h, "host": by_id[h]["host"]} for h in x["_hostids"]]})
        return rows

    return RecordingClient({"host.get": host_get, "problem.get": problem_get,
                            "trigger.get": trigger_get, "hostgroup.get": [{"groupid": "1"}],
                            "event.get": event_get})


class TestCompoundParent:
    def test_parent_of_a_sub_host(self):
        assert compound_parent("srv-aq9001 aq9002") == "srv-aq9001"

    def test_plain_names_have_none(self):
        assert compound_parent("srv-aq9001") is None
        assert compound_parent("  ") is None
        assert compound_parent("a b c") is None


class TestHostColumn:
    def test_every_problem_names_its_host(self):
        c = _client()
        out = run_tool(problems, "get_problems", c, group="app_free")
        assert "`srv-aq9001` · agent unreachable" in out, out
        assert "`srv-aq9001 aq9002` · service check failed" in out
        assert "`srv-bv9003` · disk low [ACK]" in out

    def test_one_trigger_lookup_over_distinct_ids(self):
        c = _client()
        run_tool(problems, "get_problems", c)
        tg = c.sent("trigger.get")
        assert sorted(tg["triggerids"]) == ["t1", "t2", "t3"]
        assert tg["selectHosts"] == ["hostid", "host"]
        assert "objectid" in c.sent("problem.get")["output"]

    def test_a_sub_host_query_includes_its_parent_and_says_so(self):
        c = _client()
        out = run_tool(problems, "get_problems", c, host="srv-aq9001 aq9002")
        assert sorted(c.sent("problem.get")["hostids"]) == ["9001", "9002"]
        assert "is a sub-host; problems of its parent `srv-aq9001` are included" in out
        assert "agent unreachable" in out and "service check failed" in out
        assert "disk low" not in out

    def test_a_plain_host_query_is_unchanged(self):
        c = _client()
        out = run_tool(problems, "get_problems", c, host="srv-bv9003")
        assert c.sent("problem.get")["hostids"] == ["9003"]
        assert "sub-host" not in out and "disk low" in out

    def test_unknown_host_is_still_refused(self):
        out = run_tool(problems, "get_problems", _client(), host="srv-hm9009")
        assert out == "Host 'srv-hm9009' not found."

    def test_resolved_view_names_hosts_too(self):
        c = _client()
        out = run_tool(problems, "get_problems", c, include_resolved=True, time_from="24h")
        assert c.sent("event.get")["selectHosts"] == ["hostid", "host"]
        assert "`srv-bv9003` · disk low [PROBLEM] [ACK]" in out, out
