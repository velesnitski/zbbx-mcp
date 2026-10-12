"""Wire tests for ``build_provider_overrides`` / ``build_datacenter_overrides``.

No reverse DNS is performed: ``socket.gethostbyaddr`` is monkeypatched. The
tools drop non-global addresses in production; the RFC 5737 documentation
ranges ADR 119 mandates are non-global, so ``group_blocks`` is wrapped with
``routable_only=False`` here — the flag exists for exactly this.
"""

import functools
import ipaddress
import json
import socket

import httpx
import pytest

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp import classify
from zbbx_mcp.tools import bootstrap

PTRS = {
    "192.0.2.0/24": "host1.aqx1.example.net",
    "198.51.100.0/24": "",                      # no PTR
}


def _wide(addr, prefix=16):
    """The /16 a documentation address falls in (derived, never hard-coded)."""
    return ipaddress.ip_network(f"{addr}/{prefix}", strict=False)


def _ptr_for(ip):
    a = ipaddress.ip_address(ip)
    for net, name in PTRS.items():
        n = ipaddress.ip_network(net)
        if a in n or a in _wide(n.network_address):
            return name
    return ""


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("ZBBX_FILE_ROOTS", str(tmp_path))
    monkeypatch.setitem(bootstrap.DC_CODES, "aqx", "Base Camp, AQ")    # synthetic facility code
    monkeypatch.setattr(bootstrap, "group_blocks",
                        functools.partial(bootstrap.group_blocks, routable_only=False))
    lookups = []

    def gethostbyaddr(ip):
        lookups.append(ip)
        name = _ptr_for(ip)
        if not name:
            raise socket.herror("no PTR")
        return (name, [], [ip])
    monkeypatch.setattr(bootstrap.socket, "gethostbyaddr", gethostbyaddr)
    monkeypatch.setattr(classify, "_EXTRA_PROVIDER_NETS",
                        [("Provider A", ipaddress.ip_network("203.0.113.0/24"))])
    monkeypatch.setattr(classify, "_EXTRA_DC_NETS",
                        [("Provider A", "City AQ", ipaddress.ip_network("203.0.113.0/24"))])
    return lookups


def _boom(_params):
    raise httpx.ConnectError("unreachable")


def _host(name, *ips):
    return {"host": name, "interfaces": [{"ip": ip} for ip in ips]}


HOSTS = [
    _host("srv-aq9001", "192.0.2.10", "127.0.0.1"),
    _host("srv-aq9002", "192.0.2.11"),
    _host("srv-aq9003", "192.0.2.12"),
    _host("srv-bv9001", "198.51.100.10"),
    _host("srv-bv9002", "198.51.100.11", " "),
    _host("srv-hm9001", "203.0.113.10"),          # already resolved by the table
    _host("srv-hm9002", "203.0.113.11"),
    _host("srv-tf9001", "not-an-ip"),
    _host("srv-gs9001"),
]


def test_build_provider_overrides_drafts_blocks(tmp_path, _env):
    out_path = tmp_path / "draft" / "providers.json"
    c = RecordingClient({"host.get": HOSTS})
    out = run_tool(bootstrap, "build_provider_overrides", c, output_path=str(out_path))
    p = c.sent("host.get")
    assert p == {"output": ["host"], "selectInterfaces": ["ip"], "filter": {"status": "0"}}
    a16, b16 = _wide("192.0.2.10"), _wide("198.51.100.10")
    # one PTR lookup per block, on the block's first address
    assert sorted(_env) == sorted([str(a16.network_address + 1), str(b16.network_address + 1)])
    draft = json.loads(out_path.read_text())
    assert draft == {
        "aqx1.example.net": [str(a16)],   # operator tail = last three labels
        "RENAME-ME-02 (2 hosts: srv-bv9001, srv-bv9002)": [str(b16)],
    }
    assert out.startswith(f"Wrote {out_path}")
    assert "- 5 unresolved addresses in 2 /16 blocks" in out
    assert "- 1 of 2 entries named from reverse DNS" in out


def test_build_provider_overrides_min_hosts_leaves_nothing(tmp_path):
    out_path = tmp_path / "p.json"
    out = run_tool(bootstrap, "build_provider_overrides", RecordingClient({"host.get": HOSTS}),
                   output_path=str(out_path), min_hosts=4)
    assert out == "Every address already resolves to a provider — nothing to add."
    assert not out_path.exists()


def test_build_provider_overrides_confined_and_api_error(tmp_path):
    c = RecordingClient({"host.get": HOSTS})
    out = run_tool(bootstrap, "build_provider_overrides", c, output_path="/usr/zbbx-draft.json")
    assert out.startswith("Error: Output path '/usr/zbbx-draft.json' is not in the allowed roots")
    assert c.calls == []
    out = run_tool(bootstrap, "build_provider_overrides", RecordingClient({"host.get": _boom}),
                   output_path=str(tmp_path / "p.json"))
    assert out == "Error: unreachable"


def test_build_datacenter_overrides_drafts_cities(tmp_path, _env):
    out_path = tmp_path / "dc.json"
    c = RecordingClient({"host.get": HOSTS})
    out = run_tool(bootstrap, "build_datacenter_overrides", c, output_path=str(out_path))
    draft = json.loads(out_path.read_text())
    # 203.0.113.0/24 already has a city → excluded
    assert draft == {"Other": [["192.0.2.0/24", "Base Camp, AQ"],
                               ["198.51.100.0/24", "UNKNOWN (no PTR)"]]}
    assert out.startswith(f"Wrote {out_path}")
    assert "- 2 /24 blocks with no city" in out
    assert "- 1 got one from reverse DNS" in out


def test_build_datacenter_overrides_unknown_ptr_kept_in_placeholder(tmp_path, monkeypatch):
    monkeypatch.setitem(PTRS, "192.0.2.0/24", "plain.example.org")
    out_path = tmp_path / "dc.json"
    run_tool(bootstrap, "build_datacenter_overrides", RecordingClient({"host.get": HOSTS[:3]}),
             output_path=str(out_path))
    assert json.loads(out_path.read_text()) == {
        "Other": [["192.0.2.0/24", "UNKNOWN (plain.example.org)"]]}


def test_build_datacenter_overrides_nothing_to_add_and_error(tmp_path):
    hosts = [_host("srv-hm9001", "203.0.113.10"), _host("srv-hm9002", "203.0.113.11")]
    out = run_tool(bootstrap, "build_datacenter_overrides", RecordingClient({"host.get": hosts}),
                   output_path=str(tmp_path / "dc.json"))
    assert out == "Every address already resolves to a city — nothing to add."
    out = run_tool(bootstrap, "build_datacenter_overrides", RecordingClient({"host.get": _boom}),
                   output_path=str(tmp_path / "dc.json"))
    assert out == "Error: unreachable"


def test_group_blocks_production_default_drops_documentation_ranges():
    # The unwrapped helper (what production uses) treats RFC 5737 as non-global.
    real = bootstrap.group_blocks.func
    assert real([("srv-aq9001", "192.0.2.10")], 16, False) == {}


def test_read_only_mode_does_not_register_the_drafting_tools():
    from tests.wiretest import CaptureMCP, StubResolver
    from zbbx_mcp.tools import WRITE_TOOLS
    mcp = CaptureMCP()
    bootstrap.register(mcp, StubResolver(RecordingClient()), skip=set(WRITE_TOOLS))
    assert mcp.fns == {}
