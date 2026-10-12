"""``get_proxies`` wire test: the exact ``proxy.get`` request, the per-proxy
rendering (mode, host count, version compatibility, TLS) and the empty /
error paths. Fixtures are synthetic."""

import httpx

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp.tools import proxies
from zbbx_mcp.tools.proxies import format_proxy_compat

PROXIES = [
    {"proxyid": "9001", "name": "proxy-aq9001", "operating_mode": "0",
     "description": "", "lastaccess": "1700000000", "tls_connect": "1",
     "version": "7.0.4", "compatibility": "1",
     "hosts": [{"hostid": "1"}, {"hostid": "2"}, {"hostid": "3"}]},
    {"proxyid": "9002", "name": "proxy-bv9002", "operating_mode": "1",
     "description": "edge collector " + "x" * 200, "lastaccess": "1700000600",
     "tls_connect": "4", "version": "6.4.1", "compatibility": "2", "hosts": []},
    {"proxyid": "9003", "name": "proxy-hm9003", "operating_mode": "1",
     "lastaccess": "0", "tls_connect": "1", "version": "5.0.0",
     "compatibility": "3", "hosts": [{"hostid": "4"}]},
    {"proxyid": "9004", "name": "proxy-tf9004", "operating_mode": "9",
     "lastaccess": "1700001200", "version": "0", "compatibility": "0"},
]


def test_request_uses_the_standard_proxy_method_and_7x_fields():
    c = RecordingClient({"proxy.get": PROXIES})
    run_tool(proxies, "get_proxies", c)
    # Only one call, and it is the real API method (ADR 056), not a renamed one.
    assert [m for m, _ in c.calls] == ["proxy.get"]
    assert c.sent("proxy.get") == {
        "output": ["proxyid", "name", "operating_mode", "description",
                   "lastaccess", "tls_connect", "tls_accept",
                   "version", "compatibility"],
        "selectHosts": ["hostid"],
        "sortfield": "name",
    }


def test_renders_mode_hosts_compat_and_tls():
    out = run_tool(proxies, "get_proxies", RecordingClient({"proxy.get": PROXIES}))
    assert out.startswith("**Found: 4 proxies**")
    assert "- **proxy-aq9001** v7.0.4 (Active, 3 hosts)\n  Last seen: 2023-11-14 22:13 UTC | proxyid: 9001" in out
    # Outdated + passive + TLS + description truncated to 100 chars.
    assert "- **proxy-bv9002** v6.4.1 ⚠ OUTDATED (Passive, 0 hosts) [TLS]" in out
    assert "  edge collector " + "x" * 85 + "\n" in out
    assert "x" * 86 not in out
    assert "- **proxy-hm9003** v5.0.0 ✗ UNSUPPORTED (Passive, 1 hosts)" in out
    # Unknown mode is echoed raw; unknown version gets no annotation.
    assert "- **proxy-tf9004** (9, 0 hosts)" in out
    # The never-seen proxy renders the epoch literally formatted.
    assert "Last seen: 1970-01-01 00:00 UTC | proxyid: 9003" in out


def test_no_proxies():
    assert run_tool(proxies, "get_proxies", RecordingClient()) == "No proxies found."


def test_api_error_is_reported_not_raised():
    def boom(_p):
        raise httpx.ConnectError("connection refused")

    out = run_tool(proxies, "get_proxies", RecordingClient({"proxy.get": boom}))
    assert out == "Error querying Zabbix: connection refused"


def test_format_proxy_compat():
    assert format_proxy_compat("1", "7.0.0") == " v7.0.0"
    assert format_proxy_compat("2", "") == " ⚠ OUTDATED"
    assert format_proxy_compat("0", "0") == ""
