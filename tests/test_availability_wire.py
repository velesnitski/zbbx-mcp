"""Wire tests for ``get_host_availability`` and ``get_recent_changes``.

Drive the real tool functions through RecordingClient and assert the exact
API params sent and the rendered text. Fixtures are synthetic.
"""

import time

import httpx
import pytest

import zbbx_mcp.classify as classify
from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp.tools import availability

NOW = int(time.time())


@pytest.fixture(autouse=True)
def _no_product_map(monkeypatch):
    monkeypatch.delenv("ZABBIX_PRODUCT_MAP", raising=False)
    monkeypatch.setattr(classify, "_PRODUCT_MAP", None)


def _host(hid, name, ip, group="app_free"):
    return {"hostid": hid, "host": name, "name": name, "status": "0",
            "groups": [{"name": group}], "interfaces": [{"ip": ip}]}


HOSTS = [
    _host("1", "srv-aq9001", "198.51.100.1"),
    _host("2", "srv-bv9002", "198.51.100.2", "app_paid"),
    _host("3", "srv-hm9003", "198.51.100.3"),
    _host("4", "srv-tf9004", "198.51.100.4"),
]

# agent: 1=OK host 1, 2=DOWN host 2, unknown host 3; host 4 agent OK but SNMP DOWN
AVAIL = [
    {"hostid": "1", "interfaces": [{"available": "1", "type": "1"}]},
    {"hostid": "2", "interfaces": [{"available": "2", "type": "1"}]},
    {"hostid": "3", "interfaces": [{"available": "0", "type": "1"}]},
    {"hostid": "4", "interfaces": [{"available": "1", "type": "1"},
                                   {"available": "2", "type": "2"}]},
]


def _avail_client(avail=AVAIL, hosts=HOSTS):
    def host_get(p):
        return avail if p["output"] == ["hostid"] else hosts
    return RecordingClient({"host.get": host_get})


class TestHostAvailability:
    def test_unavailable_default_lists_agent_or_snmp_down(self):
        c = _avail_client()
        out = run_tool(availability, "get_host_availability", c)
        assert "(2 hosts, filter: unavailable)" in out
        assert "| srv-bv9002 | BV | app_paid | 198.51.100.2 | DOWN | - |" in out
        assert "| srv-tf9004 | TF | app_free | 198.51.100.4 | OK | DOWN |" in out
        assert "srv-aq9001" not in out
        # first call is the inventory fetch, second the availability fetch
        first, second = (p for m, p in c.calls if m == "host.get")
        assert first["filter"] == {"status": "0"}
        assert first["selectInterfaces"] == ["ip"]
        assert second["selectInterfaces"] == ["available", "type"]

    def test_available_filter_and_limit(self):
        out = run_tool(availability, "get_host_availability", _avail_client(),
                       status="available", limit=1)
        assert "(1 hosts, filter: available)" in out
        assert "| srv-aq9001 | AQ | app_free | 198.51.100.1 | OK | - |" in out
        assert "srv-tf9004" not in out  # cut by limit

    def test_all_shows_unknown_agent(self):
        out = run_tool(availability, "get_host_availability", _avail_client(), status="all")
        assert "(4 hosts, filter: all)" in out
        assert "| srv-hm9003 | HM | app_free | 198.51.100.3 | ? | - |" in out

    def test_empty(self):
        out = run_tool(availability, "get_host_availability", _avail_client(avail=[]))
        assert out == "No unavailable hosts found."

    def test_malformed_availability_degrades_to_unknown(self):
        # A non-numeric 'available' raises ValueError inside the inner try;
        # the map falls back to empty, every host renders as unknown.
        bad = [{"hostid": "1", "interfaces": [{"available": "x", "type": "1"}]}]
        out = run_tool(availability, "get_host_availability", _avail_client(avail=bad),
                       status="all")
        assert "| srv-aq9001 | AQ | app_free | 198.51.100.1 | ? | - |" in out

    def test_api_error(self):
        def boom(p):
            raise httpx.ConnectError("unreachable")
        out = run_tool(availability, "get_host_availability", RecordingClient({"host.get": boom}))
        assert out == "Error: unreachable"


def _problem(eid, name, sev, trig, ack="0", suppressed="0", clock=NOW - 600):
    return {"eventid": eid, "name": name, "severity": sev, "clock": str(clock),
            "acknowledged": ack, "suppressed": suppressed, "objectid": trig}


PROBLEMS = [
    _problem("10", "High CPU", "4", "t1", ack="1"),
    _problem("11", "Disk full", "5", "t2"),
    _problem("12", "Ping loss", "4", "t3"),
    _problem("13", "Planned", "3", "t1", suppressed="1"),
]
RESOLVED = [
    {"eventid": "20", "name": "Agent down", "clock": str(NOW - 900), "value": "0",
     "severity": "3", "hosts": [{"host": "srv-bv9002"}]},
    {"eventid": "21", "name": "Orphan", "clock": str(NOW - 950), "value": "0", "severity": "2"},
]
TRIGS = [
    {"triggerid": "t1", "hosts": [{"host": "srv-aq9001"}]},
    {"triggerid": "t2", "hosts": [{"host": "srv-hm9003"}]},
    # t3 unknown to trigger.get → host "?"
]


class TestRecentChanges:
    def _client(self, problems=PROBLEMS, events=RESOLVED):
        return RecordingClient({"problem.get": problems, "event.get": events,
                                "trigger.get": TRIGS})

    def test_happy_path(self):
        c = self._client()
        out = run_tool(availability, "get_recent_changes", c, hours=6, limit=10)
        assert "**Recent Changes** (last 6h)" in out
        assert "### New Problems (3)" in out          # suppressed one dropped
        assert "| High | srv-aq9001 | High CPU | Yes |" in out
        assert "| Disaster | srv-hm9003 | Disk full | No |" in out
        assert "| High | ? | Ping loss | No |" in out
        assert "Planned" not in out
        assert "### Resolved (2)" in out
        assert "| srv-bv9002 | Agent down |" in out
        assert "| ? | Orphan |" in out
        assert "**Summary:** 3 new, 2 resolved. High: 2, Disaster: 1" in out
        pg = c.sent("problem.get")
        assert "selectHosts" not in pg               # ADR 070
        assert pg["limit"] == 10 and pg["recent"] is True
        assert int(pg["time_from"]) == pytest.approx(int(time.time()) - 6 * 3600, abs=5)
        eg = c.sent("event.get")
        assert eg["limit"] == 20 and eg["value"] == "0"
        assert c.sent("trigger.get")["triggerids"] == ["t1", "t2", "t3"]

    def test_include_suppressed(self):
        out = run_tool(availability, "get_recent_changes", self._client(),
                       include_suppressed=True)
        assert "### New Problems (4)" in out
        assert "Planned" in out

    def test_empty(self):
        c = self._client(problems=[], events=[])
        out = run_tool(availability, "get_recent_changes", c)
        assert "### New Problems: None" in out
        assert "### Resolved: None" in out
        assert "**Summary:** 0 new, 0 resolved." in out
        assert not any(m == "trigger.get" for m, _ in c.calls)

    def test_api_error(self):
        def boom(p):
            raise ValueError("bad params")
        c = RecordingClient({"problem.get": boom})
        assert run_tool(availability, "get_recent_changes", c) == "Error: bad params"
