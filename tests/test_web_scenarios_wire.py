"""Wire tests for ``get_web_scenarios`` / ``get_web_scenario_status``.
Fixtures are synthetic; URLs are example.com/.net/.org only."""

import httpx

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp.tools import web_scenarios


def _boom(_params):
    raise httpx.ConnectError("unreachable")


def _ws(tid, name, hid, host, *urls, status="0", delay="1m"):
    return {"httptestid": tid, "name": name, "status": status, "delay": delay,
            "hosts": [{"hostid": hid, "host": host}],
            "steps": [{"name": f"s{i}", "url": u} for i, u in enumerate(urls)]}


SCENARIOS = [
    _ws("1", "Home", "10", "srv-aq9001", "https://example.com/", "https://example.com/a",
        "https://example.com/b", "https://example.com/c"),
    _ws("2", "Api", "11", "srv-bv9001", "https://example.net/api", status="1"),
    {"httptestid": "3", "name": "Orphan", "status": "0"},
]


def test_get_web_scenarios_lists_host_status_and_first_three_urls():
    c = RecordingClient({"httptest.get": SCENARIOS})
    out = run_tool(web_scenarios, "get_web_scenarios", c, max_results=3)
    p = c.sent("httptest.get")
    assert p["limit"] == 3 and "hostids" not in p and "search" not in p
    assert out.startswith("**3 web scenarios** (showing 3)")
    assert "**Home** [Enabled] — srv-aq9001\n  ID: 1 | Interval: 1m | Steps: 4 | URLs: " in out
    assert "https://example.com/b" in out and "https://example.com/c" not in out
    assert "**Api** [Disabled] — srv-bv9001" in out
    assert "**Orphan** [Enabled] — ?\n  ID: 3 | Interval: ? | Steps: 0 | URLs: " in out


def test_get_web_scenarios_filters_and_wraps_search():
    c = RecordingClient({"httptest.get": SCENARIOS[:1]})
    out = run_tool(web_scenarios, "get_web_scenarios", c, host_id="10", search="Home")
    p = c.sent("httptest.get")
    assert p["hostids"] == ["10"]
    assert p["search"] == {"name": "*Home*"} and p["searchWildcardsEnabled"] is True
    assert out.startswith("**1 web scenarios**\n") and "(showing" not in out
    c2 = RecordingClient({"httptest.get": []})
    assert run_tool(web_scenarios, "get_web_scenarios", c2, search="Ho*") == "No web scenarios found."
    assert c2.sent("httptest.get")["search"] == {"name": "Ho*"}


def test_get_web_scenarios_error():
    out = run_tool(web_scenarios, "get_web_scenarios", RecordingClient({"httptest.get": _boom}))
    assert out == "Error: unreachable"


STATUS_SCEN = [
    _ws("1", "Home", "10", "srv-aq9001", "https://example.com/"),
    _ws("2", "Api", "11", "srv-bv9001", "https://example.net/api", "https://example.net/v2"),
    _ws("3", "Docs", "12", "srv-hm9001", "https://example.org/"),
    {"httptestid": "4", "name": "Bare", "status": "0"},
]
ITEMS = [
    {"hostid": "10", "key_": "web.test.time[Home,s0,resp]", "lastvalue": "0.1234"},
    {"hostid": "10", "key_": "web.test.fail[Home]", "lastvalue": "0"},
    {"hostid": "11", "key_": "web.test.time[Api,s0,resp]", "lastvalue": "n/a"},   # unparsable
    {"hostid": "11", "key_": "web.test.fail[Api]", "lastvalue": "1"},
    {"hostid": "12", "key_": "web.test.fail[Docs]", "lastvalue": "2"},
]


def test_get_web_scenario_status_table_and_item_query():
    c = RecordingClient({"httptest.get": STATUS_SCEN, "item.get": ITEMS})
    out = run_tool(web_scenarios, "get_web_scenario_status", c)
    hp = c.sent("httptest.get")
    assert hp["filter"] == {"status": "0"} and "search" not in hp
    ip = c.sent("item.get")
    assert ip["hostids"] == ["10", "11", "12"]
    assert ip["search"] == {"key_": "*web.test*"} and ip["searchWildcardsEnabled"] is True
    assert ip["limit"] == 20
    assert out.startswith("**Web Scenario Health:** 4 scenarios, 2 failed")
    assert "| Home | srv-aq9001 | OK | 0.12s | https://example.com/ |" in out
    assert "| Api | srv-bv9001 | FAIL | N/A | https://example.net/api, https://example.net/v2 |" in out
    assert "| Docs | srv-hm9001 | FAIL | N/A |" in out
    assert "| Bare | ? | OK | N/A |  |" in out


def test_get_web_scenario_status_only_failed_and_truncation():
    c = RecordingClient({"httptest.get": STATUS_SCEN, "item.get": ITEMS})
    out = run_tool(web_scenarios, "get_web_scenario_status", c,
                   only_failed=True, max_results=1, search="a")
    assert c.sent("httptest.get")["search"] == {"name": "*a*"}
    assert "| Api |" in out and "| Docs |" not in out and "| Home |" not in out
    assert out.endswith("*1 more omitted*")


def test_get_web_scenario_status_empty_and_error():
    c = RecordingClient({"httptest.get": []})
    out = run_tool(web_scenarios, "get_web_scenario_status", c)
    assert out == "No enabled web scenarios found."
    assert [m for m, _ in c.calls] == ["httptest.get"]
    out = run_tool(web_scenarios, "get_web_scenario_status",
                   RecordingClient({"httptest.get": STATUS_SCEN, "item.get": _boom}))
    assert out == "Error: unreachable"
