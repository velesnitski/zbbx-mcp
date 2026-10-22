"""Wire tests for ``detect_traffic_anomalies``, ``detect_traffic_drops`` and
the remaining ``get_traffic_report`` paths. Fixtures are synthetic."""

import ipaddress
import time

import httpx
import pytest

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp import classify
from zbbx_mcp.tools import traffic

NOW = int(time.time())
LIVE = str(NOW - 60)
CONN = "conn.count"
H = 3600


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(classify, "_PRODUCT_MAP", {})
    monkeypatch.setattr(classify, "_EXTRA_PROVIDER_NETS",
                        [("Provider A", ipaddress.ip_network("192.0.2.0/24"))])
    monkeypatch.setattr(traffic, "KEY_CONNECTIONS", "")


def _boom(_params):
    raise httpx.ConnectError("unreachable")


def _host(hid, name, group="app_free", ip=None):
    return {"hostid": hid, "host": name, "groups": [{"name": group}],
            "interfaces": [{"ip": ip or f"192.0.2.{hid}"}]}


# --------------------------------------------------- detect_traffic_anomalies

AN_HOSTS = [
    _host("1", "srv-aq9001"), _host("2", "srv-aq9002"), _host("3", "srv-aq9003"),
    _host("4", "srv-aq9004"),                       # low traffic, has connections → CRITICAL
    _host("5", "srv-aq9005"),                       # zero traffic, CPU busy → HIGH
    _host("6", "srv-aq9006"),                       # low traffic only → MEDIUM
    _host("7", "srv-aq9007"),                       # never reported traffic
    _host("8", "srv-bv9001", "app_paid"),           # group with too few peers
    _host("9", "srv-aq9099-test"),                  # test host, excluded by default
]
AN_TRAFFIC = {"1": 100e6, "2": 100e6, "3": 100e6, "4": 5e6, "5": 0, "6": 10e6,
              "8": 100e6, "9": 1e6}
AN_IDLE = {"1": 50, "5": 20, "6": 90}
AN_CONNS = {"4": 120, "1": 300}



def _key(p):
    """The exact key filter, or "traffic" for the physical-NIC prefix search (ADR 154)."""
    return "traffic" if "search" in p else (p.get("filter") or {}).get("key_")

def _an_client(hosts=AN_HOSTS):
    def items(p):
        key = _key(p)
        ids = set(p["hostids"])
        if key == "system.cpu.util[,idle]":
            src = AN_IDLE
        elif key == CONN:
            src = AN_CONNS
        else:
            src = AN_TRAFFIC
        rows = [{"hostid": h, "key_": "net.if.in[eth0]", "lastvalue": str(v), "lastclock": LIVE}
                for h, v in src.items() if h in ids]
        if src is AN_TRAFFIC and "7" in ids:
            rows.append({"hostid": "7", "key_": "net.if.in[eth0]", "lastvalue": "0", "lastclock": "0"})
        return rows
    return RecordingClient({"host.get": hosts, "item.get": items})


def test_traffic_anomalies_grades_by_signal(monkeypatch):
    monkeypatch.setattr(traffic, "KEY_CONNECTIONS", CONN)
    c = _an_client()
    out = run_tool(traffic, "detect_traffic_anomalies", c)
    keys = [_key(p) for m, p in c.calls if m == "item.get"]
    assert CONN in keys and "system.cpu.util[,idle]" in keys and "traffic" in keys
    assert "9" not in c.sent("item.get")["hostids"]
    assert out.startswith("**3 traffic anomalies detected**")
    assert "### 🔴 CRITICAL (1)" in out
    assert ("- **srv-aq9004** (Provider A) — 5.00 Mbps, 120 conns\n"
            "  Group: app_free (median: 100.0 Mbps)\n"
            "  Reasons: Traffic 5% of group median (100.0 Mbps); 120 active connections but only 5.00 Mbps") in out
    assert "### 🟠 HIGH (1)" in out
    # a live zero is a reading (it used to vanish from the map, so this
    # signal could never fire)
    assert "- **srv-aq9005** (Provider A) — 0.00 Mbps, CPU 80.0%\n" in out
    assert "Reasons: Traffic 0% of group median (100.0 Mbps); Zero traffic but CPU at 80.0%" in out
    assert "### 🟡 MEDIUM (1)" in out
    assert "- **srv-aq9006** (Provider A) — 10.00 Mbps, CPU 10.0%" in out
    assert "srv-aq9007" not in out
    assert "| app_free | 5/7 | 100.0 | 3 |" in out
    assert "app_paid" not in out
    assert "srv-aq9099-test" in out                   # named in the excluded-test note


def test_traffic_anomalies_filters_and_include_test():
    out = run_tool(traffic, "detect_traffic_anomalies", _an_client(), include_test=True,
                   threshold_pct=4, country="aq", region="ALL", group="app_free")
    # the test host joins the peer set (and the median), and is not footnoted
    assert out.startswith("**2 traffic anomalies detected**")
    assert "- **srv-aq9099-test** (Provider A) — 1.00 Mbps\n" in out
    assert "Reasons: Traffic 2% of group median (55.0 Mbps)" in out
    assert "| app_free | 6/8 | 55.0 | 2 |" in out
    assert "excluded" not in out


def test_traffic_anomalies_nothing_to_compare():
    out = run_tool(traffic, "detect_traffic_anomalies", _an_client(), product="app_paid")
    assert out.startswith("No groups with enough peers for analysis.")
    hosts = AN_HOSTS[:3]
    out = run_tool(traffic, "detect_traffic_anomalies", _an_client(hosts=hosts))
    assert out.startswith("**No traffic anomalies detected.**")
    assert "| app_free | 3/3 | 100.0 | 0 |" in out


def test_traffic_anomalies_error():
    out = run_tool(traffic, "detect_traffic_anomalies", RecordingClient({"host.get": _boom}))
    assert out == "Error: unreachable"


# ------------------------------------------------------- detect_traffic_drops

# (baseline bps, recent bps) per host's carrier interface
DROP = {
    "1": (100e6, 5e6),       # collapse, CPU held up → block
    "2": (100e6, 5e6),       # collapse, CPU fell with it → low demand
    "3": (100e6, 90e6),      # healthy
    "4": (2e6, 0.1e6),       # baseline under the floor
    "5": (100e6, 5e6),       # agent down
    "6": (None, 5e6),        # only recent history → not examined
}
# (baseline idle %, recent idle %)
CPU_IDLE = {"1": (50, 50), "2": (50, 98), "5": (50, 50)}
DROP_HOSTS = [_host(h, f"srv-aq900{h}") for h in DROP] + [_host("7", "srv-aq9099-test")]


def _series(base, recent):
    rows = []
    for k in range(1, 7 * 24):
        clock = NOW - k * H + H // 2
        v = recent if k <= 6 else base
        if v is not None:
            rows.append((clock, v))
    return rows


def _drop_client(hosts=DROP_HOSTS, ping_down=("5",)):
    def items(p):
        ids = set(p["hostids"])
        if "search" in p:
            out = []
            for h in sorted(ids):
                if h in DROP:
                    out.append({"itemid": f"t{h}", "hostid": h, "key_": "net.if.in[eth0]",
                                "lastvalue": "5000000", "value_type": "3"})
                    out.append({"itemid": f"t{h}b", "hostid": h, "key_": "net.if.in[eth1]",
                                "lastvalue": "0", "value_type": "3"})
            return out
        key = p["filter"]["key_"]
        if key == "agent.ping":
            return [{"hostid": h, "lastvalue": "0" if h in ping_down else "1", "lastclock": LIVE}
                    for h in sorted(ids)]
        if key == "system.cpu.util[,idle]":
            return [{"itemid": f"c{h}", "hostid": h} for h in sorted(ids) if h in CPU_IDLE]
        return []

    def trends(p):
        out = []
        for iid in p["itemids"]:
            h = iid[1:].rstrip("b")
            if iid.startswith("c"):
                series = _series(*CPU_IDLE[h])
            elif iid.endswith("b"):
                continue
            else:
                series = _series(*DROP[h])
            out += [{"itemid": iid, "clock": str(c), "value_avg": str(v), "value_max": str(v)}
                    for c, v in series]
        return out
    return RecordingClient({"host.get": hosts, "item.get": items, "trend.get": trends})


def test_traffic_drops_classifies_each_host():
    c = _drop_client()
    out = run_tool(traffic, "detect_traffic_drops", c, seasonal=False)
    search = next(p for m, p in c.calls if m == "item.get" and "search" in p)
    assert sorted(search["hostids"]) == ["1", "2", "3", "4", "5", "6"]
    tr = [p for m, p in c.calls if m == "trend.get"]
    assert sorted(tr[0]["itemids"]) == sorted([f"t{h}" for h in DROP] + [f"t{h}b" for h in DROP])
    assert tr[0]["limit"] == 12 * 7 * 24
    assert sorted(tr[1]["itemids"]) == ["c1", "c2"]           # corroboration: candidates only
    assert out.startswith("**1 likely blocks** (0 sustained, 1 acute/immediate; analyzed 5, 1 were low-demand not blocked)")
    assert "| srv-aq9001 | Provider A | ACUTE | 70% | 5.0 → 100.0 Mbps | **95%** |" in out
    assert "srv-aq9002 |" not in out and "srv-aq9005 |" not in out
    assert "_1 host(s) had no usable trend data and were NOT examined: srv-aq9006." in out
    assert "srv-aq9099-test" in out


def test_traffic_drops_seasonal_escalates_to_sustained():
    out = run_tool(traffic, "detect_traffic_drops", _drop_client(), country="aq", region="ALL",
                   group="app_free", product="app_free")
    assert "| srv-aq9001 | Provider A | SUSTAINED | 95% |" in out


def test_traffic_drops_nothing_flagged_reports_counts():
    out = run_tool(traffic, "detect_traffic_drops", _drop_client(hosts=DROP_HOSTS[2:]),
                   seasonal=False, include_test=True)
    n = 3
    assert out.startswith(f"No blocks detected (analyzed {n} servers; 1 healthy/diurnal, "
                          "0 low-demand-not-blocked, 1 agent-down (not a traffic block), "
                          "2 skipped for insufficient/low baseline).")
    assert "srv-aq9006" in out


def test_traffic_drops_connections_corroborate(monkeypatch):
    monkeypatch.setattr(traffic, "KEY_CONNECTIONS", CONN)
    c = _drop_client(hosts=DROP_HOSTS[:1])
    out = run_tool(traffic, "detect_traffic_drops", c, seasonal=False)
    keys = [p["filter"]["key_"] for m, p in c.calls if m == "item.get" and "search" not in p]
    assert keys == ["agent.ping", "system.cpu.util[,idle]", CONN]
    assert "| srv-aq9001 |" in out


def test_traffic_drops_empty_paths_and_error():
    out = run_tool(traffic, "detect_traffic_drops", _drop_client(), group="app_none")
    assert out.startswith("No servers match the filter.")
    c = _drop_client(hosts=[_host("8", "srv-bv9001")])
    assert run_tool(traffic, "detect_traffic_drops", c) == "No traffic items found."
    out = run_tool(traffic, "detect_traffic_drops", RecordingClient({"host.get": _boom}))
    assert out == "Error: unreachable"


# --------------------------------------------------------- get_traffic_report

REP_HOSTS = [
    _host("1", "srv-aq9001", "app_free"),
    _host("2", "srv-aq9001 9002", "app_free"),          # sub-host: folds into 1
    _host("3", "srv-bv9001", "app_paid"),
    _host("4", "srv-hm9001", "app_paid"),
]
REP_TRAFFIC = {"1": 100e6, "2": 50e6, "3": 400e6, "4": 10e6}
REP_CONNS = {"1": 100, "2": 50, "3": 100}


def _rep_client():
    def items(p):
        src = REP_CONNS if _key(p) == CONN else REP_TRAFFIC
        return [{"hostid": h, "key_": "net.if.in[eth0]", "lastvalue": str(v), "lastclock": LIVE}
                for h, v in src.items()]
    return RecordingClient({"host.get": REP_HOSTS, "item.get": items})


def test_traffic_report_folds_sub_hosts_and_sorts_by_bw(monkeypatch):
    monkeypatch.setattr(traffic, "KEY_CONNECTIONS", CONN)
    out = run_tool(traffic, "get_traffic_report", _rep_client(), sort_by="bw_per_client")
    assert out.startswith("**Traffic Report (3 servers, sorted by bw_per_client)**")
    rows = [line for line in out.splitlines() if line.startswith("| srv-")]
    assert rows[0] == "| srv-bv9001 | app_paid/Default | Provider A | 400.0 Mbps | 100 | 4000 Kbps |"
    assert rows[1] == "| srv-aq9001 | app_free/Default | Provider A | 150.0 Mbps | 150 | 1000 Kbps |"
    assert rows[2] == "| srv-hm9001 | app_paid/Default | Provider A | 10.0 Mbps | – | – |"
    assert "Connections shown as – on 1 of 3 server(s)" in out


def test_traffic_report_group_tier_filters_and_no_conn_key():
    out = run_tool(traffic, "get_traffic_report", _rep_client(), group="APP_PAID", tier="Default")
    assert out.startswith("**Traffic Report (2 servers, sorted by traffic)**")
    assert "srv-aq9001" not in out
    # with no connections key every row is unmeasured, and says so
    assert "Connections shown as – on 2 of 2 server(s)" in out
    out = run_tool(traffic, "get_traffic_report", _rep_client(), tier="Premium")
    assert out.startswith("No servers match the filters.")
    assert "tier labels present: Default" in out


def test_traffic_anomalies_statistical_outlier_and_scope_filters():
    hosts = [_host(str(i), f"srv-aq90{10 + i}") for i in range(1, 10)] + [_host("10", "srv-aq9030")]
    vals = {str(i): 100e6 for i in range(1, 10)} | {"10": 30e6}

    def items(p):
        if _key(p) == "system.cpu.util[,idle]":
            return []
        return [{"hostid": h, "key_": "net.if.in[eth0]", "lastvalue": str(v), "lastclock": LIVE}
                for h, v in vals.items()]
    c = RecordingClient({"host.get": hosts, "item.get": items})
    out = run_tool(traffic, "detect_traffic_anomalies", c)
    # above the 20%-of-median line, but more than 2σ under the mean
    assert "- **srv-aq9030** (Provider A) — 30.00 Mbps\n" in out
    assert "Reasons: Statistical outlier (>2σ below mean)" in out
    for kw in ({"country": "bv"}, {"region": "APAC"}):
        out = run_tool(traffic, "detect_traffic_anomalies", RecordingClient({"host.get": hosts, "item.get": items}), **kw)
        assert out.startswith("No groups with enough peers for analysis.")


@pytest.mark.parametrize("kw", [{"product": "app_paid"}, {"country": "bv"}, {"region": "APAC"}])
def test_traffic_drops_scope_filters_exclude_everything(kw):
    c = _drop_client()
    out = run_tool(traffic, "detect_traffic_drops", c, **kw)
    assert out.startswith("No servers match the filter.")
    assert [m for m, _ in c.calls] == ["host.get"]


def test_traffic_report_group_filter_excludes_other_groups():
    c = _rep_client()
    out = run_tool(traffic, "get_traffic_report", c, group="app_none")
    assert out.startswith("No servers match the filters.")
    assert "product labels present: app_free, app_paid" in out
    assert [m for m, _ in c.calls] == ["host.get"]            # no traffic read
