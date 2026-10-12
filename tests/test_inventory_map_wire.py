"""Wire tests for ``get_server_map`` / ``get_product_summary`` /
``generate_product_map``. Fixtures are synthetic."""

import ipaddress
import json

import httpx
import pytest

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp import classify
from zbbx_mcp.tools import inventory_map


@pytest.fixture(autouse=True)
def _synthetic_env(monkeypatch):
    monkeypatch.setattr(classify, "_PRODUCT_MAP", {"app_free": ("App", "Free"),
                                                   "app_paid": ("App", "Premium")})
    monkeypatch.setattr(classify, "_EXTRA_PROVIDER_NETS",
                        [("Provider B", ipaddress.ip_network("198.51.100.0/24"))])
    monkeypatch.setattr(classify, "_EXTRA_DC_NETS",
                        [("Provider A", "City AQ", ipaddress.ip_network("192.0.2.0/24"))])


def _boom(_params):
    raise httpx.ConnectError("unreachable")


def _host(hid, name, group, *ips, status="0"):
    return {"hostid": hid, "host": name, "name": name, "status": status,
            "groups": [{"groupid": "g" + group, "name": group}],
            "interfaces": [{"ip": ip, "type": "1"} for ip in ips]}


HOSTS = [
    _host("1", "srv-aq9001", "app_free", "127.0.0.1", "192.0.2.1"),
    _host("2", "srv-bv9001", "app_free", "198.51.100.2"),
    _host("3", "srv-hm9001", "app_paid", "203.0.113.3"),
    _host("4", "srv-aq9002", "app_paid"),                         # no interface
    _host("5", "srv-tf9001", "app_free", "192.0.2.5", status="1"),  # disabled
    _host("6", "srv-gs9001", "Templates", "192.0.2.6"),            # Unknown product
]


def test_get_server_map_tree_with_provider_and_city():
    c = RecordingClient({"host.get": HOSTS})
    out = run_tool(inventory_map, "get_server_map", c)
    assert c.sent("host.get")["selectInterfaces"] == ["ip", "type"]
    assert out.startswith("**Server Map: 5 servers**")
    assert "## App (4 servers)" in out
    assert "### Free (2)" in out and "### Premium (2)" in out
    assert "## Unknown (1 servers)" in out
    # 127.0.0.1 is skipped in favour of the real address
    assert "| srv-aq9001 | 192.0.2.1 | Provider A | City AQ | app_free |" in out
    assert "| srv-bv9001 | 198.51.100.2 | Provider B |  | app_free |" in out
    assert "| srv-hm9001 | 203.0.113.3 | Other |  | app_paid |" in out
    assert "| srv-aq9002 |  |  |  | app_paid |" in out
    assert "srv-tf9001" not in out


def test_get_server_map_filters():
    c = RecordingClient({"host.get": HOSTS})
    out = run_tool(inventory_map, "get_server_map", c, product="app", tier="free",
                   country="aq", only_enabled=False)
    assert "**Server Map: 1 servers**" in out
    assert "srv-aq9001" in out and "srv-tf9001" not in out and "srv-bv9001" not in out
    c = RecordingClient({"host.get": HOSTS})
    out = run_tool(inventory_map, "get_server_map", c, only_enabled=False, country="tf")
    assert "srv-tf9001" in out


def test_get_server_map_no_match_and_error():
    c = RecordingClient({"host.get": HOSTS})
    assert run_tool(inventory_map, "get_server_map", c, country="pn") == "No servers match the filters."
    out = run_tool(inventory_map, "get_server_map", RecordingClient({"host.get": _boom}))
    assert out == "Error building server map: unreachable"


def test_get_product_summary_counts_free_and_paid():
    c = RecordingClient({"host.get": HOSTS})
    out = run_tool(inventory_map, "get_product_summary", c)
    assert c.sent("host.get")["selectGroups"] == ["name"]
    assert out.startswith("**Product Summary: 6 servers (5 enabled)**")
    assert "| App | Free | 3 | 2 | Free |" in out
    assert "| App | Premium | 2 | 2 | Paid |" in out
    assert "| Unknown | Unknown | 1 | 1 | Paid |" in out


def test_get_product_summary_empty_and_error():
    out = run_tool(inventory_map, "get_product_summary", RecordingClient())
    assert out.startswith("**Product Summary: 0 servers (0 enabled)**")
    out = run_tool(inventory_map, "get_product_summary", RecordingClient({"host.get": _boom}))
    assert out == "Error: unreachable"


def test_generate_product_map_skips_templates_and_empty_groups():
    groups = [
        {"groupid": "1", "name": "app_free", "hosts": [{"hostid": "1"}]},
        {"groupid": "2", "name": "app_paid", "hosts": [{"hostid": "2"}, {"hostid": "3"}]},
        {"groupid": "3", "name": "Templates", "hosts": [{"hostid": "4"}]},
        {"groupid": "4", "name": "empty", "hosts": []},
    ]
    c = RecordingClient({"hostgroup.get": groups})
    out = run_tool(inventory_map, "generate_product_map", c)
    assert c.sent("hostgroup.get")["selectHosts"] == ["hostid"]
    assert out.startswith("**Starter product_map.json** (2 groups)")
    body = out.split("```json\n")[1].split("\n```")[0]
    parsed = json.loads(body)
    assert parsed == {"app_paid": ["app_paid", "Default"], "app_free": ["app_free", "Default"]}
    assert list(parsed) == ["app_paid", "app_free"]           # largest group first
    assert "ZABBIX_PRODUCT_MAP=" in out


def test_generate_product_map_error():
    out = run_tool(inventory_map, "generate_product_map", RecordingClient({"hostgroup.get": _boom}))
    assert out == "Error: unreachable"
