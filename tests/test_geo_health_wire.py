"""Wire tests for ``get_regional_density_map``, ``get_latency_estimate`` and
``get_servers_by_ping``. Fixtures are synthetic.

The fixture countries are uninhabited territories, which carry no capital
coordinates; synthetic ones are injected for the latency tools so distances
are exact and checkable.
"""

import ipaddress
import time

import httpx
import pytest

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp import classify
from zbbx_mcp.tools import geo_health

LIVE = str(int(time.time()) - 60)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(classify, "_PRODUCT_MAP", {})
    monkeypatch.setattr(classify, "_EXTRA_DC_NETS", [
        ("Provider A", "City AQ", ipaddress.ip_network("192.0.2.0/25")),
        ("Provider A", "Various", ipaddress.ip_network("192.0.2.128/25")),
    ])
    monkeypatch.setattr(classify, "_EXTRA_PROVIDER_NETS",
                        [("Provider B", ipaddress.ip_network("198.51.100.0/24"))])
    # Synthetic coordinates: AQ at the origin, BV 1° east, HM 10° east (equator).
    monkeypatch.setitem(geo_health.CAPITAL_COORDS, "AQ", (0.0, 0.0))
    monkeypatch.setitem(geo_health.CAPITAL_COORDS, "BV", (0.0, 1.0))
    monkeypatch.setitem(geo_health.CAPITAL_COORDS, "HM", (0.0, 10.0))


def _boom(_params):
    raise httpx.ConnectError("unreachable")


def _host(hid, name, group, ip=None):
    return {"hostid": hid, "host": name, "groups": [{"name": group}],
            "interfaces": [{"ip": ip}] if ip else []}


HOSTS = [
    _host("1", "srv-aq9001", "app_free", "192.0.2.1"),
    _host("2", "srv-aq9002", "app_paid", "192.0.2.200"),
    _host("3", "srv-bv9001", "app_free", "198.51.100.3"),
    _host("4", "srv-hm9001", "app_paid", "203.0.113.4"),
    _host("5", "srv-hm9002", "app_free"),
    _host("6", "srv-tf9001", "app_free", "192.0.2.6"),   # TF: no coordinates
]
TRAFFIC = {"1": 600e6, "2": 400e6, "3": 2500e6, "4": 100e6}
IDLE = {"1": 80, "2": 60, "3": 10}


def _client(hosts=HOSTS):
    def items(p):
        ids = set(p["hostids"])
        if (p.get("filter") or {}).get("key_") == "system.cpu.util[,idle]":
            return [{"hostid": h, "lastvalue": str(v), "lastclock": LIVE}
                    for h, v in IDLE.items() if h in ids]
        return [{"hostid": h, "key_": "net.if.in[eth0]", "lastvalue": str(v), "lastclock": LIVE}
                for h, v in TRAFFIC.items() if h in ids]
    return RecordingClient({"host.get": hosts, "item.get": items})


# ---------------------------------------------------- get_regional_density_map

def test_density_map_default_region_keeps_every_country():
    out = run_tool(geo_health, "get_regional_density_map", _client())
    assert out.startswith("**Density Map** (4 countries)")
    rows = [line for line in out.splitlines() if line.startswith("| ") and "Country" not in line
            and not line.startswith("|--")]
    assert [r.split(" | ")[0] for r in rows] == ["| BV **!**", "| AQ", "| HM", "| TF **!**"]
    assert "| BV **!** | 1 | 2.5 | 90.0% | Other |" in out
    # "Various" is not a city, so the provider is listed instead
    assert "| AQ | 2 | 1.0 | 30.0% | City AQ, Provider A |" in out
    assert "| HM | 2 | 0.1 | 0% | Other |" in out
    assert "**No redundancy (1 server):** BV, TF" in out


def test_density_map_filters_threshold_and_cap():
    out = run_tool(geo_health, "get_regional_density_map", _client(), country="aq")
    assert out.startswith("**Density Map** (1 countries)") and "| AQ |" in out
    out = run_tool(geo_health, "get_regional_density_map", _client(), min_traffic_mbps=500)
    assert "(2 countries)" in out and "| HM |" not in out
    out = run_tool(geo_health, "get_regional_density_map", _client(), max_results=1)
    assert out.endswith("*3 more countries omitted*")


def test_density_map_empty_and_error():
    out = run_tool(geo_health, "get_regional_density_map", _client(), region="APAC")
    assert out == "No servers match the filters."
    out = run_tool(geo_health, "get_regional_density_map", RecordingClient({"host.get": _boom}))
    assert out == "Error: unreachable"


# -------------------------------------------------------- get_latency_estimate

def test_latency_estimate_ranks_countries_by_distance():
    c = _client()
    out = run_tool(geo_health, "get_latency_estimate", c, client_country="aq")
    assert c.sent("host.get")["filter"] == {"status": "0"}
    assert out.startswith("**Nearest servers for clients in AQ**")
    assert "| AQ * | 2 | 0 |" in out
    assert "| BV | 1 | 111 |" in out
    assert "| HM | 2 | 1,112 |" in out
    assert "TF" not in out and "No servers in" not in out


def test_latency_estimate_product_filter_and_no_local_servers():
    out = run_tool(geo_health, "get_latency_estimate", _client(), client_country="BV",
                   product="app_paid", max_results=1)
    assert "| AQ | 1 | 111 |" in out and "| HM |" not in out
    assert out.endswith("**No servers in BV.** Nearest: AQ (111 km)")


def test_latency_estimate_guards_and_error():
    assert run_tool(geo_health, "get_latency_estimate", _client()).startswith("client_country is required")
    assert run_tool(geo_health, "get_latency_estimate", _client(), client_country="TF") == \
        "Unknown country 'TF'. No coordinates available."
    out = run_tool(geo_health, "get_latency_estimate", _client(), client_country="AQ", product="app_none")
    assert out == "No servers found."
    out = run_tool(geo_health, "get_latency_estimate", RecordingClient({"host.get": _boom}),
                   client_country="AQ")
    assert out == "Error: unreachable"


# --------------------------------------------------------- get_servers_by_ping

def test_servers_by_ping_lists_hosts_with_traffic():
    out = run_tool(geo_health, "get_servers_by_ping", _client(), client_country="aq")
    assert out.startswith("**Nearest servers for AQ** (5 total)")
    assert "| srv-aq9001 | AQ | 0 km | ~0 ms | 600.0 |" in out
    assert "| srv-bv9001 | BV | 111 km | ~1 ms | 2500.0 |" in out
    assert "| srv-hm9002 | HM | 1,112 km | ~7 ms | 0 |" in out
    assert "srv-tf9001" not in out


def test_servers_by_ping_product_filter_and_cap():
    out = run_tool(geo_health, "get_servers_by_ping", _client(), client_country="HM",
                   product="app_paid", max_results=1)
    assert out.startswith("**Nearest servers for HM** (2 total)")
    assert "| srv-hm9001 | HM | 0 km |" in out and "srv-aq9002" not in out
    assert out.endswith("*1 more omitted*")


def test_servers_by_ping_guards_and_error():
    assert run_tool(geo_health, "get_servers_by_ping", _client()) == \
        "client_country is required (2-letter code)."
    assert run_tool(geo_health, "get_servers_by_ping", _client(), client_country="TF") == "Unknown country 'TF'."
    out = run_tool(geo_health, "get_servers_by_ping", _client(), client_country="AQ", product="app_none")
    assert out == "No servers found."
    out = run_tool(geo_health, "get_servers_by_ping", RecordingClient({"host.get": _boom}),
                   client_country="AQ")
    assert out == "Error: unreachable"


def test_region_all_covers_codes_outside_the_named_regions():
    from zbbx_mcp.country import REGION_MAP, countries_for_region
    named = {cc for codes in REGION_MAP.values() for cc in codes}
    assert "AQ" not in named                       # the gap the default call fell into
    assert named | {"AQ", "BV"} <= countries_for_region("ALL")
