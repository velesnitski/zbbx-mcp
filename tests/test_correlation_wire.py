"""Wire tests for ``get_idle_relays`` / ``get_outage_clusters``.
Fixtures are synthetic: documentation-range addresses only."""

import ipaddress
import time
from datetime import datetime, timezone

import httpx
import pytest

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp import classify
from zbbx_mcp.tools import correlation

NOW = int(time.time())
LIVE = str(NOW - 60)


@pytest.fixture(autouse=True)
def _providers(monkeypatch):
    monkeypatch.setattr(classify, "_EXTRA_PROVIDER_NETS", [
        ("Provider A", ipaddress.ip_network("192.0.2.0/24")),
        ("Provider B", ipaddress.ip_network("198.51.100.0/24")),
        ("Provider B", ipaddress.ip_network("203.0.113.0/24")),
    ])


def _boom(_params):
    raise httpx.ConnectError("unreachable")


# ------------------------------------------------------------- get_idle_relays

RELAY_HOSTS = [
    {"hostid": "1", "host": "srv-aq9001", "interfaces": [{"ip": "192.0.2.1"}]},     # not forwarding
    {"hostid": "2", "host": "srv-aq9002", "interfaces": [{"ip": "192.0.2.2"}]},     # NAT-mode: out ≈ in
    {"hostid": "3", "host": "srv-bv9001", "interfaces": [{"ip": "198.51.100.3"}]},  # tunnel busy
    {"hostid": "4", "host": "srv-hm9001", "interfaces": [{"ip": "203.0.113.4"}]},   # not forwarding, smaller
    {"hostid": "5", "host": "srv-tf9001", "interfaces": []},                         # no tunnels at all
]


def _it(hid, key, bps, clock=LIVE):
    return {"hostid": hid, "key_": key, "lastvalue": str(bps), "lastclock": clock}


IN_ITEMS = [
    _it("1", "net.if.in[eth0]", 5_000_000), _it("1", "net.if.in[tun0]", 0),
    _it("1", "net.if.in[tun1]", 0), _it("1", "net.if.in[lo]", 900),
    _it("1", "net.if.in[docker0]", 900), _it("1", "net.if.in[br-abc]", 900),
    _it("1", "net.if.in[enp3s0]", 1_000_000),                 # regex-physical
    _it("2", "net.if.in[eth0]", 5_000_000), _it("2", "net.if.in[tun0]", 0),
    _it("3", "net.if.in[eth0]", 5_000_000), _it("3", "net.if.in[tun0]", 4_000_000),
    _it("4", "net.if.in[eth0]", 2_000_000), _it("4", "net.if.in[tun0]", 0),
    _it("4", "net.if.in[tun9]", 7_000_000, clock="0"),        # stale: not zero, not counted
    _it("5", "net.if.in[eth0]", 9_000_000),
    _it("", "net.if.in[tun0]", 0), {"hostid": "1", "key_": "system.cpu.load"},
]
OUT_ITEMS = [
    _it("1", "net.if.out[eth0]", 100_000),
    _it("2", "net.if.out[eth0]", 4_900_000),
    _it("4", "net.if.out[eth0]", 0), _it("4", "net.if.out[tun0]", 50),
]


def _relay_client(hosts=RELAY_HOSTS):
    def items(p):
        src = IN_ITEMS if p["search"]["key_"] == "net.if.in[" else OUT_ITEMS
        return [i for i in src if i.get("hostid") in p["hostids"] or not i.get("hostid")]
    return RecordingClient({"host.get": hosts, "item.get": items})


def test_get_idle_relays_flags_only_non_forwarding_hosts():
    c = _relay_client()
    out = run_tool(correlation, "get_idle_relays", c)
    keys = [p["search"]["key_"] for m, p in c.calls if m == "item.get"]
    assert keys == ["net.if.in[", "net.if.out["]
    assert c.sent("item.get")["hostids"] == ["1", "2", "3", "4", "5"]
    assert out.startswith("**2 relays not forwarding** (physical in ≥ 100 kbps")
    assert "| srv-aq9001 | 192.0.2.1 | 6000.0 | 100.0 | 2 | tun0, tun1 |" in out
    assert "| srv-hm9001 | 203.0.113.4 | 2000.0 | 0.0 | 1 | tun0 |" in out
    assert out.index("srv-aq9001") < out.index("srv-hm9001")
    for healthy in ("srv-aq9002", "srv-bv9001", "srv-tf9001"):
        assert healthy not in out


def test_get_idle_relays_threshold_country_and_cap():
    out = run_tool(correlation, "get_idle_relays", _relay_client(), min_mgmt_kbps=3000)
    assert out.startswith("**1 relays not forwarding**") and "srv-hm9001" not in out
    out = run_tool(correlation, "get_idle_relays", _relay_client(), max_results=1)
    assert out.endswith("*1 more omitted*")
    c = _relay_client()
    out = run_tool(correlation, "get_idle_relays", c, country="hm")
    assert c.sent("item.get")["hostids"] == ["4"]
    assert "srv-hm9001" in out


def test_get_idle_relays_none_found_and_empty():
    out = run_tool(correlation, "get_idle_relays", _relay_client(hosts=RELAY_HOSTS[1:3]))
    assert out.startswith("No forwarding failures found (2 hosts inspected).")
    assert "NAT-mode and excluded." in out
    assert run_tool(correlation, "get_idle_relays", _relay_client(), country="pn") == "No hosts found."
    assert run_tool(correlation, "get_idle_relays", RecordingClient({"host.get": _boom})) == "Error: unreachable"


# --------------------------------------------------------- get_outage_clusters

def _p(eid, name, sev, age_s, suppressed="0"):
    return {"eventid": eid, "name": name, "severity": str(sev),
            "clock": str(NOW - age_s), "suppressed": suppressed}


CL_HOSTS = [
    {"hostid": "1", "host": "srv-aq9001", "interfaces": [{"ip": "192.0.2.1"}], "groups": [{"name": "app_free"}]},
    {"hostid": "2", "host": "srv-aq9002", "interfaces": [{"ip": "192.0.2.2"}], "groups": [{"name": "app_free"}]},
    {"hostid": "3", "host": "srv-aq9003", "interfaces": [{"ip": "192.0.2.3"}], "groups": [{"name": "app_free"}]},
    {"hostid": "4", "host": "srv-aq9003 9004", "interfaces": [{"ip": "192.0.2.4"}], "groups": []},
    {"hostid": "5", "host": "srv-bv9001", "interfaces": [{"ip": "198.51.100.5"}], "groups": [{"name": "app_paid"}]},
    {"hostid": "6", "host": "srv-hm9001", "interfaces": [{"ip": "203.0.113.6"}], "groups": [{"name": "app_paid"}]},
    {"hostid": "7", "host": "srv-tf9001", "interfaces": [], "groups": [{"name": "app_paid"}]},
]
CL_PROBLEMS = [
    _p("1", "Agent down on srv-aq9001", 4, 600),
    _p("2", "Agent down on srv-aq9002", 4, 700),
    _p("3", "Ping loss", 3, 800),
    _p("4", "Agent down on srv-aq9003 9004", 5, 900),   # sub-host of aq9003
    _p("5", "Port closed", 3, 650),
    _p("6", "Port closed", 3, 660),
    _p("7", "No IP", 3, 670),
    _p("8", "Planned", 4, 600, suppressed="1"),
]
EVENT_HOST = {"1": "1", "2": "2", "3": "3", "4": "4", "5": "5", "6": "6", "7": "7", "8": "1"}


def _cluster_client(problems=CL_PROBLEMS):
    def events(p):
        return [{"eventid": e, "hosts": [{"hostid": EVENT_HOST[e]}]} for e in p["eventids"]]

    def hosts(p):
        return [h for h in CL_HOSTS if h["hostid"] in p["hostids"]]
    return RecordingClient({"problem.get": problems, "event.get": events, "host.get": hosts})


def test_get_outage_clusters_auto_picks_narrowest_level():
    c = _cluster_client()
    out = run_tool(correlation, "get_outage_clusters", c)
    pp = c.sent("problem.get")
    assert pp["sortfield"] == "eventid" and pp["severities"] == [3, 4, 5] and pp["limit"] == 2000
    assert "8" not in c.sent("event.get")["eventids"]
    assert c.sent("host.get")["hostids"] == ["1", "2", "3", "4", "5", "6", "7"]
    assert out.startswith("**1 outage clusters** (window 30m, ≥3 hosts, by subnet24 (auto))")
    assert "### Cluster 1 — 192.0.2.0/24" in out
    assert "- **Hosts:** 3 (srv-aq9001, srv-aq9002, srv-aq9003)" in out
    t0 = datetime.fromtimestamp(NOW - 900, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    t1 = datetime.fromtimestamp(NOW - 600, timezone.utc).strftime("%H:%M")
    assert f"- **When:** {t0} → {t1} (started 15m ago, 4 events)" in out
    assert "- **Max severity:** Disaster" in out
    # the sub-host's own name is stripped from its problem (no "9004" residue)
    assert "- **Problems:** Agent down; Ping loss" in out


def test_get_outage_clusters_provider_and_hostgroup_levels():
    out = run_tool(correlation, "get_outage_clusters", _cluster_client(), group_by="provider", min_hosts=2)
    assert "by provider" in out
    assert "### Cluster 1 — Provider A" in out and "### Cluster 2 — Provider B" in out
    assert "- **Hosts:** 2 (srv-bv9001, srv-hm9001)" in out
    out = run_tool(correlation, "get_outage_clusters", _cluster_client(), group_by="hostgroup",
                   min_hosts=2, max_clusters=1)
    assert out.startswith("**2 outage clusters**") and "by hostgroup" in out
    assert "### Cluster 1 — app_free" in out
    assert out.endswith("*1 more clusters omitted*")


def test_get_outage_clusters_subnet16_and_legacy_alias():
    out = run_tool(correlation, "get_outage_clusters", _cluster_client(), group_by="subnet16")
    sixteen = str(ipaddress.ip_network("192.0.2.0/16", strict=False))
    assert f"### Cluster 1 — {sixteen}" in out
    out = run_tool(correlation, "get_outage_clusters", _cluster_client(), group_by="subnet")
    assert "by subnet24" in out and "(auto)" not in out


def test_get_outage_clusters_no_cluster_and_age_filter():
    out = run_tool(correlation, "get_outage_clusters", _cluster_client(), min_hosts=5)
    assert out == "No outage clusters (window=30m, min_hosts=5, group_by=auto)."
    old = [_p("1", "x", 4, 5 * 3600)]
    c = _cluster_client(problems=old)
    assert run_tool(correlation, "get_outage_clusters", c, max_age_hours=1) == \
        "No active problems (severity >= 3)."
    assert [m for m, _ in c.calls] == ["problem.get"]


def test_get_outage_clusters_invalid_group_and_unbound_problems():
    c = _cluster_client()
    out = run_tool(correlation, "get_outage_clusters", c, group_by="rack")
    assert out.startswith("Invalid group_by: 'rack'.") and c.calls == []
    c = RecordingClient({"problem.get": [_p("9", "x", 4, 60)], "event.get": [{"eventid": "9"}]})
    assert run_tool(correlation, "get_outage_clusters", c) == "No host-bound problems found."
    out = run_tool(correlation, "get_outage_clusters", RecordingClient({"problem.get": _boom}))
    assert out == "Error: unreachable"
