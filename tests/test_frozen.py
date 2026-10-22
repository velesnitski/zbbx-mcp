"""Problems on disabled triggers or items ("frozen") are monitoring debt, not
outages: the pure helpers, then every current-state surface that counts open
problems. Fixtures are synthetic."""

import asyncio
import ipaddress
import time

import httpx
import pytest

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp import classify
from zbbx_mcp.frozen import frozen_note, frozen_trigger_ids, split_frozen
from zbbx_mcp.tools import correlation, diagnose, floods, health, problems, slack, triage

NOW = int(time.time())
NOTE = "sit on disabled triggers or items and cannot recover"


def _ids(client, probs):
    return asyncio.run(frozen_trigger_ids(client, probs))


def _boom(_params):
    raise httpx.ConnectError("unreachable")


# ------------------------------------------------------------------ pure core

class TestFrozenTriggerIds:
    PROBS = [{"objectid": t} for t in ("t1", "t2", "t3", "t4", "t5", "t6", "t1")]
    REPLY = [
        {"triggerid": "t1", "status": "1", "items": [{"itemid": "i1", "status": "0"}]},   # trigger off
        {"triggerid": "t2", "status": "0", "items": [{"itemid": "i2", "status": "1"},
                                                     {"itemid": "i3", "status": "1"}]},   # all items off
        {"triggerid": "t3", "status": "0", "items": [{"itemid": "i4", "status": "1"},
                                                     {"itemid": "i5", "status": "0"}]},   # one item on
        {"triggerid": "t4", "status": "0", "items": []},                                  # calculated
        {"triggerid": "t5", "status": "0", "items": [{"itemid": "i6", "status": "0"}]},
        # t6 absent from the reply
    ]

    def test_classification_and_exact_params(self):
        c = RecordingClient({"trigger.get": self.REPLY})
        assert _ids(c, self.PROBS) == {"t1", "t2", "t6"}
        assert c.calls == [("trigger.get", {
            "triggerids": ["t1", "t2", "t3", "t4", "t5", "t6"],
            "output": ["triggerid", "status"],
            "selectItems": ["itemid", "status"],
        })]

    def test_no_items_trigger_is_live(self):
        c = RecordingClient({"trigger.get": [{"triggerid": "t4", "status": "0"}]})
        assert _ids(c, [{"objectid": "t4"}]) == set()

    def test_absent_trigger_is_frozen(self):
        c = RecordingClient({"trigger.get": [{"triggerid": "t5", "status": "0", "items": []}]})
        assert _ids(c, [{"objectid": "t5"}, {"objectid": "t9"}]) == {"t9"}

    def test_empty_reply_cannot_judge(self):
        assert _ids(RecordingClient({"trigger.get": []}), [{"objectid": "t1"}]) is None

    def test_failure_cannot_judge(self):
        assert _ids(RecordingClient({"trigger.get": _boom}), [{"objectid": "t1"}]) is None

    def test_no_trigger_ids_makes_no_call(self):
        c = RecordingClient()
        assert _ids(c, [{"eventid": "1"}, {"objectid": ""}]) == set()
        assert c.calls == []


class TestSplitAndNote:
    PROBS = [{"eventid": "1", "objectid": "a"}, {"eventid": "2", "objectid": "b"}]

    def test_split(self):
        live, frozen = split_frozen(self.PROBS, {"b"})
        assert [p["eventid"] for p in live] == ["1"] and [p["eventid"] for p in frozen] == ["2"]

    def test_none_keeps_everything_live(self):
        assert split_frozen(self.PROBS, None) == (self.PROBS, [])
        assert split_frozen(self.PROBS, set()) == (self.PROBS, [])

    def test_note(self):
        assert frozen_note(0) == ""
        assert frozen_note(2) == ("2 problem(s) sit on disabled triggers or items and cannot "
                                  "recover — excluded as monitoring debt, not outages.")


# ----------------------------------------------------------------- wire tests
# tF is a disabled trigger, tL a live one. `fail=True` breaks only the frozen
# lookup, so every other call keeps working and the counts must not move.

def _trig(hosts=None, fail=False):
    def respond(p):
        if "selectItems" in p:
            if fail:
                raise httpx.ConnectError("unreachable")
            return [{"triggerid": t, "status": "1" if t.startswith("tF") else "0",
                     "items": [{"itemid": "i" + t, "status": "0"}]} for t in p["triggerids"]]
        if "selectHosts" in p:
            return [{"triggerid": t, "hosts": (hosts or {}).get(t, [])} for t in p["triggerids"]]
        return [{"triggerid": t, "dependencies": []} for t in p["triggerids"]]
    return respond


def _p(eid, trig, sev=4, age=600, name=None):
    return {"eventid": eid, "objectid": trig, "name": name or f"Check {eid}",
            "severity": str(sev), "clock": str(NOW - age), "suppressed": "0", "acknowledged": "0"}


PROBS = [_p("1", "tL1"), _p("2", "tF1"), _p("3", "tF2")]


class TestGetProblems:
    def _c(self, fail=False):
        return RecordingClient({"problem.get": PROBS, "trigger.get": _trig(fail=fail)})

    def test_frozen_excluded_and_named(self):
        c = self._c()
        out = run_tool(problems, "get_problems", c)
        assert "objectid" in c.sent("problem.get")["output"]
        assert out.startswith("**Found: 1 problems**\n_2 problem(s) " + NOTE)
        assert "Check 1" in out and "Check 2" not in out

    def test_include_frozen_lists_them_marked(self):
        out = run_tool(problems, "get_problems", self._c(), include_frozen=True)
        assert out.startswith("**Found: 3 problems**")
        assert "Check 2 [frozen]" in out and "Check 3 [frozen]" in out and "Check 1 [frozen]" not in out

    def test_lookup_failure_changes_nothing(self):
        out = run_tool(problems, "get_problems", self._c(fail=True))
        assert out.startswith("**Found: 3 problems**\n\n") and NOTE not in out

    def test_only_frozen_reads_empty_with_note(self):
        c = RecordingClient({"problem.get": PROBS[1:], "trigger.get": _trig()})
        assert run_tool(problems, "get_problems", c).startswith("No problems found.\n\n_2 problem(s)")


def _health_client(probs=PROBS, fail=False):
    def events(p):
        return [{"eventid": e, "hosts": [{"host": f"srv-aq{9000 + int(e)}"}]} for e in p["eventids"]]
    return RecordingClient({"problem.get": probs, "trigger.get": _trig(fail=fail), "event.get": events})


class TestActiveProblems:
    def test_counts_exclude_frozen(self):
        c = _health_client()
        out = run_tool(health, "get_active_problems", c)
        assert out.startswith("**1 active problems**\n\n_2 problem(s) " + NOTE)
        assert "- **High:** 1" in out
        assert c.sent("event.get")["eventids"] == ["1"]

    def test_lookup_failure_changes_nothing(self):
        out = run_tool(health, "get_active_problems", _health_client(fail=True))
        assert out.startswith("**3 active problems**") and "- **High:** 3" in out and NOTE not in out

    def test_only_frozen(self):
        out = run_tool(health, "get_active_problems", _health_client(PROBS[1:]))
        assert out.startswith("No active problems (severity >= 2).\n2 problem(s) " + NOTE)


class TestAgeBuckets:
    def test_counts_exclude_frozen(self):
        c = _health_client()
        out = run_tool(health, "get_problem_age_buckets", c)
        assert "objectid" in c.sent("problem.get")["output"]
        assert "(1 active, severity ≥ 0)" in out and "| High | 1 | 0 | 0 | 0 | 1 |" in out
        assert out.endswith("_2 problem(s) " + NOTE + " — excluded as monitoring debt, not outages._")

    def test_lookup_failure_changes_nothing(self):
        out = run_tool(health, "get_problem_age_buckets", _health_client(fail=True))
        assert "(3 active" in out and NOTE not in out


class TestDiagnoseHost:
    """A domain-mode host (no agent, no traffic items): the verdict rests on
    open problems alone, so a frozen one must not make it degraded."""

    def _c(self, probs, fail=False):
        rec = {"hostid": "10", "host": "web-aq9001", "name": "web-aq9001", "status": "0",
               "interfaces": [{"ip": "192.0.2.10"}], "groups": [{"name": "app_free"}]}
        return RecordingClient({"host.get": [rec], "item.get": [], "problem.get": probs,
                                "trigger.get": _trig(fail=fail)})

    def test_frozen_only_reads_healthy_with_note(self):
        c = self._c(PROBS[1:])
        out = run_tool(diagnose, "diagnose_host", c, host="web-aq9001")
        assert "objectid" in c.sent("problem.get")["output"]
        assert "**Verdict:** `healthy`" in out
        assert "### Active problems (0)\n- _2 problem(s) " + NOTE in out

    def test_live_problem_still_degrades(self):
        out = run_tool(diagnose, "diagnose_host", self._c(PROBS), host="web-aq9001")
        assert "**Verdict:** `degraded`" in out and "1 active problem(s)" in out
        assert "### Active problems (1)" in out

    def test_lookup_failure_changes_nothing(self):
        out = run_tool(diagnose, "diagnose_host", self._c(PROBS[1:], fail=True), host="web-aq9001")
        assert "**Verdict:** `degraded`" in out and "### Active problems (2)" in out and NOTE not in out


class TestTriage:
    def _c(self, probs, fail=False):
        host = {"hostid": "1", "host": "srv-aq9001", "name": "srv-aq9001"}
        hosts = {t: [{"hostid": "1"}] for t in ("tL1", "tF1", "tF2")}
        return RecordingClient({"host.get": [host], "problem.get": probs,
                                "trigger.get": _trig(hosts=hosts, fail=fail)})

    def test_frozen_only_is_recovered_with_note(self):
        out = run_tool(triage, "triage_slack_alert", self._c(PROBS[1:]),
                       text="Disaster: Check 2 on srv-aq9001")
        assert "**recovered**" in out and "_2 problem(s) " + NOTE in out
        assert "Check 2" not in out.split("**recovered**")[1]

    def test_mixed_counts_live_only(self):
        out = run_tool(triage, "triage_slack_alert", self._c(PROBS), text="Check 1 on srv-aq9001")
        assert "**real_now**" in out and "1 problem(s)" in out and "_2 problem(s) " + NOTE in out

    def test_lookup_failure_changes_nothing(self):
        out = run_tool(triage, "triage_slack_alert", self._c(PROBS[1:], fail=True),
                       text="Check 2 on srv-aq9001")
        assert "**real_now**" in out and "2 problem(s)" in out and NOTE not in out


def _flood_client(fail=False):
    probs = [_p(str(i), "tL" if i < 3 else f"tF{i}") for i in range(6)]

    def events(p):
        return [{"eventid": e, "hosts": [{"hostid": "1", "host": "srv-aq9001"}]} for e in p["eventids"]]
    return RecordingClient({"problem.get": probs, "trigger.get": _trig(fail=fail), "event.get": events,
                            "host.get": [{"hostid": "1", "host": "srv-aq9001"}]})


class TestHostFloods:
    def test_frozen_do_not_make_a_flood(self):
        out = run_tool(floods, "get_host_floods", _flood_client(), min_problems=4)
        assert out.startswith("No host floods") and "Inspected 3 problem records" in out
        assert out.endswith("_3 problem(s) " + NOTE + " — excluded as monitoring debt, not outages._")

    def test_lookup_failure_changes_nothing(self):
        out = run_tool(floods, "get_host_floods", _flood_client(fail=True), min_problems=4)
        assert out.startswith("**1 hosts with active flood**") and "| srv-aq9001 | 6 |" in out
        assert NOTE not in out


@pytest.fixture
def _providers(monkeypatch):
    monkeypatch.setattr(classify, "_EXTRA_PROVIDER_NETS", [
        ("Provider A", ipaddress.ip_network("192.0.2.0/24"))])


def _cluster_client(fail=False):
    probs = [_p(str(i), "tF" + str(i) if i >= 3 else "tL" + str(i)) for i in range(1, 6)]
    hosts = [{"hostid": str(i), "host": f"srv-aq{9000 + i}", "interfaces": [{"ip": f"192.0.2.{i}"}],
              "groups": [{"name": "app_free"}]} for i in range(1, 6)]

    def events(p):
        return [{"eventid": e, "hosts": [{"hostid": e}]} for e in p["eventids"]]
    return RecordingClient({"problem.get": probs, "trigger.get": _trig(fail=fail), "event.get": events,
                            "host.get": lambda p: [h for h in hosts if h["hostid"] in p["hostids"]]})


@pytest.mark.usefixtures("_providers")
class TestOutageClusters:
    def test_frozen_do_not_make_a_cluster(self):
        c = _cluster_client()
        out = run_tool(correlation, "get_outage_clusters", c)
        assert "objectid" in c.sent("problem.get")["output"]
        assert c.sent("event.get")["eventids"] == ["1", "2"]
        assert out.startswith("No outage clusters") and "_3 problem(s) " + NOTE in out

    def test_lookup_failure_changes_nothing(self):
        out = run_tool(correlation, "get_outage_clusters", _cluster_client(fail=True))
        assert out.startswith("**1 outage clusters**") and "- **Hosts:** 5" in out and NOTE not in out


class _FakePost:
    def __init__(self):
        self.posts = []

    def __call__(self, timeout=None):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        self.posts.append(json)
        return httpx.Response(200, text="ok")


class TestSlackReport:
    @pytest.fixture
    def fake(self, monkeypatch):
        monkeypatch.setenv(slack.SLACK_WEBHOOK_ENV, "https://example.com/hook")
        monkeypatch.setenv("ZABBIX_PRODUCT_MAP", "")
        monkeypatch.setattr(classify, "_PRODUCT_MAP", None)
        f = _FakePost()
        monkeypatch.setattr(slack.httpx, "AsyncClient", f)
        return f

    def _c(self, fail=False):
        host = {"hostid": "1", "host": "srv-aq9001", "status": "0",
                "groups": [{"name": "app_free"}], "interfaces": [{"ip": "192.0.2.1"}]}
        return RecordingClient({"host.get": [host], "problem.get": PROBS, "trigger.get": _trig(fail=fail)})

    def test_counts_exclude_frozen(self, fake):
        c = self._c()
        out = run_tool(slack, "send_slack_report", c, include_high_cpu=False)
        assert out == "Report sent to Slack: 1 servers, 1 problems"
        assert "objectid" in c.sent("problem.get")["output"]
        blocks = fake.posts[0]["blocks"]
        assert any("*Active Problems (1):*" in (b.get("text") or {}).get("text", "") for b in blocks)
        assert any(b["type"] == "context" and NOTE in b["elements"][0]["text"] for b in blocks)

    def test_lookup_failure_changes_nothing(self, fake):
        out = run_tool(slack, "send_slack_report", self._c(fail=True), include_high_cpu=False)
        assert out == "Report sent to Slack: 1 servers, 3 problems"
        assert NOTE not in str(fake.posts[0])
