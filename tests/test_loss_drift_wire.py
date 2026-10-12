"""Wire test for ``detect_loss_drift``. Fixtures are synthetic."""

import time

import httpx
import pytest

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp.tools import loss_drift

NOW = int(time.time())
DAY = 86400
LOSS = "icmppingloss"
RTT = "icmppingsec"


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setattr(loss_drift, "KEY_PING_LOSS", LOSS)
    monkeypatch.setattr(loss_drift, "KEY_PING_RTT", RTT)


def _boom(_params):
    raise httpx.ConnectError("unreachable")


HOSTS = [
    {"hostid": "1", "host": "srv-aq9001", "interfaces": [{"ip": "192.0.2.1"}]},     # new loss
    {"hostid": "2", "host": "srv-aq9002", "interfaces": [{"ip": "192.0.2.2"}]},     # loss + rtt
    {"hostid": "3", "host": "srv-bv9001", "interfaces": [{"ip": "198.51.100.3"}]},  # rtt only
    {"hostid": "4", "host": "srv-hm9001", "interfaces": [{"ip": "203.0.113.4"}]},   # steady
    {"hostid": "5", "host": "srv-tf9001", "interfaces": [{"ip": "203.0.113.5"}]},   # loss-up only
    {"hostid": "6", "host": "srv-gs9001", "interfaces": []},                        # no items
]
# itemid → (baseline, recent)
SERIES = {
    "l1": (0.0, 10.0), "r1": (40.0, 41.0),
    "l2": (2.0, 12.0), "r2": (40.0, 80.0),
    "l3": (0.0, 0.0), "r3": (20.0, 50.0),
    "l4": (1.0, 1.5), "r4": (30.0, 31.0),
    "l5": (5.0, 11.0),
}
ITEMS = [{"itemid": iid, "hostid": iid[1:], "key_": LOSS if iid[0] == "l" else RTT} for iid in SERIES]
ITEMS.append({"itemid": "x9", "hostid": "4", "key_": "other.key"})


def _trends():
    out = []
    for iid, (base, recent) in SERIES.items():
        out += [{"itemid": iid, "clock": str(NOW - 10 * DAY), "value_avg": str(base)},
                {"itemid": iid, "clock": str(NOW - 5 * DAY), "value_avg": str(base)},
                {"itemid": iid, "clock": str(NOW - DAY), "value_avg": str(recent)},
                {"itemid": iid, "clock": "bad", "value_avg": "1"}]
    return out


def _client(hosts=HOSTS, items=ITEMS):
    return RecordingClient({"host.get": hosts, "item.get": items, "trend.get": _trends()})


def test_detect_loss_drift_labels_and_orders_hosts():
    c = _client()
    out = run_tool(loss_drift, "detect_loss_drift", c)
    ip = c.sent("item.get")
    assert ip["filter"] == {"key_": [LOSS, RTT], "status": "0"}
    assert ip["hostids"] == ["1", "2", "3", "4", "5", "6"]
    tp = c.sent("trend.get")
    assert sorted(tp["itemids"]) == sorted(SERIES) and tp["limit"] == 9 * 14 * 24
    assert abs(tp["time_from"] - (int(time.time()) - 14 * DAY)) <= 5
    assert out.startswith("**4 hosts with network-quality drift** (window 14d, recent 2d)")
    rows = [line for line in out.splitlines() if line.startswith("| srv-")]
    assert [r.split(" | ")[0] for r in rows] == ["| srv-aq9001", "| srv-aq9002", "| srv-tf9001", "| srv-bv9001"]
    assert "| srv-aq9001 | 192.0.2.1 | new-loss | 0.0% → 10.0% (Δ +10.0) | 40ms → 41ms (Δ +2%) |" in out
    assert "| srv-aq9002 | 192.0.2.2 | loss-and-rtt | 2.0% → 12.0% (Δ +10.0) | 40ms → 80ms (Δ +100%) |" in out
    assert "| srv-tf9001 | 203.0.113.5 | loss-up | 5.0% → 11.0% (Δ +6.0) | — |" in out
    assert "| srv-bv9001 | 198.51.100.3 | rtt-up | 0.0% → 0.0% (Δ +0.0) | 20ms → 50ms (Δ +150%) |" in out
    assert "srv-hm9001" not in out and "srv-gs9001" not in out


def test_detect_loss_drift_rtt_only_country_and_cap(monkeypatch):
    monkeypatch.setattr(loss_drift, "KEY_PING_LOSS", "")
    c = _client()
    out = run_tool(loss_drift, "detect_loss_drift", c, country="aq", max_results=1,
                   rtt_step_pct=10.0)
    assert c.sent("item.get")["filter"]["key_"] == [RTT]
    assert c.sent("item.get")["hostids"] == ["1", "2"]
    # loss items are ignored when the loss key is unset; only aq9002's RTT drifted
    assert out.startswith("**1 hosts with network-quality drift**")
    assert "| srv-aq9002 | 192.0.2.2 | rtt-up | — | 40ms → 80ms (Δ +100%) |" in out


def test_detect_loss_drift_truncates():
    out = run_tool(loss_drift, "detect_loss_drift", _client(), max_results=2)
    assert out.endswith("*2 more omitted*")


def test_detect_loss_drift_no_drift_and_empty_paths(monkeypatch):
    out = run_tool(loss_drift, "detect_loss_drift", _client(hosts=HOSTS[3:4]))
    assert out == "No drift detected (loss step ≥ 5.0%, RTT step ≥ 50.0%) in 1 hosts."
    assert run_tool(loss_drift, "detect_loss_drift", _client(), country="pn") == "No matching hosts."
    out = run_tool(loss_drift, "detect_loss_drift", _client(items=[]))
    assert out == "No ping items found on the selected hosts."
    out = run_tool(loss_drift, "detect_loss_drift",
                   _client(items=[{"itemid": "x", "hostid": "1", "key_": "other.key"}]))
    assert out == "No ping items found on the selected hosts."
    monkeypatch.setattr(loss_drift, "KEY_PING_LOSS", "")
    monkeypatch.setattr(loss_drift, "KEY_PING_RTT", "")
    c = _client()
    out = run_tool(loss_drift, "detect_loss_drift", c)
    assert out.startswith("No ping items configured.") and c.calls == []


def test_detect_loss_drift_error():
    out = run_tool(loss_drift, "detect_loss_drift", RecordingClient({"host.get": _boom}))
    assert out == "Error: unreachable"
