"""Wire tests for ``get_cost_summary`` / ``get_cost_gaps`` /
``get_cost_efficiency``. Fixtures are synthetic; costs are round."""

import ipaddress
import time

import httpx
import pytest

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp import classify
from zbbx_mcp.tools import costs_summary

COST = "{$COST_MONTH}"
NOW = str(int(time.time()) - 60)


@pytest.fixture(autouse=True)
def _synthetic_env(monkeypatch):
    monkeypatch.setattr(classify, "_PRODUCT_MAP", {"app_free": ("App", "Free"),
                                                   "app_paid": ("App", "Premium")})
    monkeypatch.setattr(classify, "_EXTRA_PROVIDER_NETS", [
        ("Provider A", ipaddress.ip_network("192.0.2.0/24")),
        ("Provider B", ipaddress.ip_network("198.51.100.0/24")),
    ])


def _boom(_params):
    raise httpx.ConnectError("unreachable")


def _host(hid, name, group, ip=None):
    return {"hostid": hid, "host": name, "groups": [{"name": group}],
            "interfaces": [{"ip": ip}] if ip else []}


HOSTS = [
    _host("1", "srv-aq9001", "app_free", "192.0.2.1"),
    _host("2", "srv-aq9001 9002", "app_free", "192.0.2.2"),    # sub-host of 1
    _host("3", "srv-bv9001", "app_paid", "198.51.100.3"),
    _host("4", "srv-hm9001", "app_paid", "198.51.100.4"),     # no cost
    _host("5", "srv-tf9001", "app_free"),                      # no IP, no cost
]
MACROS = [
    {"hostid": "1", "value": "100"},
    {"hostid": "2", "value": "100"},          # duplicates the parent's bill → MAX
    {"hostid": "3", "value": "200"},
    {"hostid": "4", "value": "n/a"},          # unparsable → no cost
]


def _client(hosts=HOSTS, macros=MACROS, items=()):
    return RecordingClient({"host.get": hosts, "usermacro.get": macros, "item.get": list(items)})


# ------------------------------------------------------------ get_cost_summary

def test_get_cost_summary_folds_sub_hosts_and_groups():
    c = _client()
    out = run_tool(costs_summary, "get_cost_summary", c)
    assert c.sent("host.get")["filter"] == {"status": "0"}
    assert c.sent("usermacro.get")["filter"] == {"macro": COST}
    assert out.startswith("**Cost Summary: $300.00/month ($3,600.00/year)**")
    assert "Servers with cost: 2 | Without: 2" in out
    assert "| App / Premium | 1 | $200.00 | $2,400.00 |" in out
    assert "| App / Free | 1 | $100.00 | $1,200.00 |" in out
    assert out.index("App / Premium") < out.index("App / Free")
    assert "| Provider B | 1 | $200.00 |" in out and "| Provider A | 1 | $100.00 |" in out
    assert "Filtered" not in out


def test_get_cost_summary_redact_partial_drops_partially_costed_rows():
    out = run_tool(costs_summary, "get_cost_summary", _client(), redact_partial=True)
    # App/Premium has 2 boxes, 1 costed → dropped; Provider B likewise
    assert out.startswith("**Cost Summary: $0.00/month")
    assert "Servers with cost" not in out
    assert "App / Premium" not in out and "| Provider B |" not in out
    assert "| Provider A | 1 | $100.00 |" in out
    assert out.endswith("*Filtered to fully-attributed lines.*")


def test_get_cost_summary_empty_and_error():
    out = run_tool(costs_summary, "get_cost_summary", _client(hosts=[], macros=[]))
    assert out.startswith("**Cost Summary: $0.00/month ($0.00/year)**")
    assert "Servers with cost: 0 | Without: 0" in out
    out = run_tool(costs_summary, "get_cost_summary", RecordingClient({"host.get": _boom}))
    assert out == "Error: unreachable"


# --------------------------------------------------------------- get_cost_gaps

def test_get_cost_gaps_groups_missing_by_provider_and_product():
    hosts = HOSTS + [_host("6", "srv-hm9002", "app_paid", "198.51.100.6"),
                     _host("7", "srv-hm9003", "app_paid", "198.51.100.7"),
                     _host("8", "srv-hm9004", "app_paid", "198.51.100.8")]
    c = _client(hosts=hosts, macros=[{"hostid": "1"}, {"hostid": "3"}])
    out = run_tool(costs_summary, "get_cost_gaps", c)
    assert c.sent("usermacro.get")["output"] == ["hostid"]
    assert out.startswith("**Cost gaps: 5 without cost** (2/7 covered)")
    # four Provider B / App rows, sample capped at three hosts
    assert "| Provider B / App | 4 | srv-hm9001, srv-hm9002, srv-hm9003 |" in out
    assert out.endswith("*1 servers without IP (monitoring) — no cost expected*")


def test_get_cost_gaps_full_coverage_and_cap():
    hosts = [_host("1", "srv-aq9001", "app_free", "192.0.2.1"),
             _host("2", "srv-bv9001", "app_paid", "198.51.100.2")]
    out = run_tool(costs_summary, "get_cost_gaps", _client(hosts=hosts, macros=[]), max_results=1)
    assert out.startswith("**Cost gaps: 2 without cost** (0/2 covered)")
    assert out.count("| Provider ") == 2 and "Provider B" not in out and "without IP" not in out
    out = run_tool(costs_summary, "get_cost_gaps",
                   _client(hosts=hosts, macros=[{"hostid": "1"}, {"hostid": "2"}]))
    assert out.startswith("**Cost gaps: 0 without cost** (2/2 covered)")


def test_get_cost_gaps_error():
    out = run_tool(costs_summary, "get_cost_gaps", RecordingClient({"usermacro.get": _boom}))
    assert out == "Error: unreachable"


# --------------------------------------------------------- get_cost_efficiency

def _traffic(hid, bps, clock=NOW):
    return {"hostid": hid, "key_": "net.if.in[eth0]", "lastvalue": str(bps), "lastclock": clock}


def test_get_cost_efficiency_by_country_provider_and_waste():
    hosts = HOSTS + [_host("6", "srv-bv9002", "app_paid", "198.51.100.6")]
    macros = MACROS + [{"hostid": "6", "value": "100"}, {"hostid": "5", "value": "300"}]
    items = [
        _traffic("1", 500_000_000), _traffic("2", 500_000_000),   # 1 Gbps folded
        _traffic("3", 2_000_000_000),
        _traffic("6", 900_000_000, clock="0"),                    # never collected → idle
    ]
    c = _client(hosts=hosts, macros=macros, items=items)
    out = run_tool(costs_summary, "get_cost_efficiency", c)
    tp = c.sent("item.get")
    assert sorted(tp["hostids"]) == ["1", "2", "3", "5", "6"]
    assert out.startswith("**Cost Efficiency** (4 servers)")
    assert "| AQ | 1 | $100 | 1.0 | $100 |" in out
    assert "| BV | 2 | $300 | 2.0 | $150 |" in out
    assert "| TF | 1 | $300 | idle | N/A |" in out
    assert out.index("| TF |") < out.index("| BV |") < out.index("| AQ |")
    assert "| Provider B | 2 | $300 | 2.0 | $150 |" in out
    assert "| ? | 1 | $300 | idle | N/A |" in out
    assert "**Waste: 2 servers paying $400/mo with 0 traffic:**" in out
    assert "| srv-tf9001 | TF | ? | $300 |" in out
    assert "| srv-bv9002 | BV | Provider B | $100 |" in out


def test_get_cost_efficiency_waste_label_and_overflow():
    hosts = [_host("1", "srv-aq9001", "app_free", "192.0.2.1"),
             _host("2", "srv-aq9001 9002", "app_free", "192.0.2.2")]
    hosts += [_host(str(10 + i), f"srv-gs90{10 + i}", "app_free", f"192.0.2.{10 + i}") for i in range(10)]
    macros = [{"hostid": h["hostid"], "value": "100"} for h in hosts]
    out = run_tool(costs_summary, "get_cost_efficiency", _client(hosts=hosts, macros=macros))
    assert "**Waste: 11 servers paying $1,100/mo" in out
    assert "srv-aq9001 (+1 sub)" in out
    assert out.endswith("*+1 more*")


def test_get_cost_efficiency_empty_and_error():
    out = run_tool(costs_summary, "get_cost_efficiency", _client(hosts=[], macros=[]))
    assert out.startswith("**Cost Efficiency** (0 servers)") and "Waste" not in out
    out = run_tool(costs_summary, "get_cost_efficiency", RecordingClient({"host.get": _boom}))
    assert out == "Error: unreachable"
