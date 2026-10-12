"""Wire tests for ``get_services`` / ``get_sla``. Fixtures are synthetic."""

import httpx

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp.tools import services


def _boom(_params):
    raise httpx.ConnectError("unreachable")


SERVICES = [
    {"serviceid": "11", "name": "Edge AQ", "status": "-1", "algorithm": "1",
     "children": [{"serviceid": "12"}, {"serviceid": "13"}], "parents": [],
     "description": "Edge tier for AQ"},
    {"serviceid": "12", "name": "Edge BV", "status": "4", "algorithm": "0",
     "children": [], "parents": [{"serviceid": "11"}]},
    {"serviceid": "13", "name": "Edge HM", "status": "9", "algorithm": "7"},
]


def test_get_services_renders_status_algorithm_and_counts():
    c = RecordingClient({"service.get": SERVICES})
    out = run_tool(services, "get_services", c)
    p = c.sent("service.get")
    assert p["limit"] == 50 and p["sortfield"] == "sortorder"
    assert "search" not in p
    assert "**Found: 3 services**" in out
    assert "**Edge AQ** [Status: OK]" in out
    assert "Algorithm: Most critical of children | Children: 2 | Parents: 0 (id: 11)" in out
    assert "\n  Edge tier for AQ" in out
    assert "**Edge BV** [Status: High]" in out and "Set status to OK" in out
    # Unknown status / algorithm codes fall back to "?"
    assert "**Edge HM** [Status: ?]\n  Algorithm: ? |" in out


def test_get_services_search_sets_wildcard_filter():
    c = RecordingClient({"service.get": []})
    out = run_tool(services, "get_services", c, search="Edge*", max_results=5)
    p = c.sent("service.get")
    assert p["search"] == {"name": "Edge*"} and p["searchWildcardsEnabled"] is True
    assert p["limit"] == 5
    assert out == "No services found."


def test_get_services_api_error():
    out = run_tool(services, "get_services", RecordingClient({"service.get": _boom}))
    assert out.startswith("Error querying Zabbix:") and "unreachable" in out


SLAS = [
    {"slaid": "1", "name": "Core", "slo": "99.9", "period": "2", "status": "1",
     "service_tags": [{"tag": "tier", "value": "Premium"}, {"tag": "app", "value": "app_paid"}]},
    {"slaid": "2", "name": "Free", "slo": "99", "period": "0", "status": "0"},
    {"slaid": "3", "name": "Odd", "period": "9"},
]


def test_get_sla_renders_period_status_and_tags():
    c = RecordingClient({"sla.get": SLAS})
    out = run_tool(services, "get_sla", c)
    assert c.sent("sla.get")["sortfield"] == "name"
    assert "**Found: 3 SLAs**" in out
    assert "**Core** — SLO: 99.9% (Monthly) [Enabled]" in out
    assert "Tags: tier=Premium, app=app_paid | id: 1" in out
    assert "**Free** — SLO: 99% (Daily) [Disabled]" in out and "Tags: none | id: 2" in out
    assert "**Odd** — SLO: ?% (?) [Disabled]" in out


def test_get_sla_search_and_empty():
    c = RecordingClient({"sla.get": []})
    out = run_tool(services, "get_sla", c, search="Co*")
    assert c.sent("sla.get")["search"] == {"name": "Co*"}
    assert out == "No SLAs found."


def test_get_sla_api_error():
    out = run_tool(services, "get_sla", RecordingClient({"sla.get": _boom}))
    assert out.startswith("Error querying Zabbix:")
