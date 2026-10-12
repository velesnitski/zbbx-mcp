"""Wire tests for ``get_at_risk_hosts`` and ``get_disruption_blast_radius``.

Real tool functions driven through RecordingClient; exact API params and the
rendered ranking are asserted. Fixtures are synthetic.
"""

import asyncio
import time

import httpx
import pytest

import zbbx_mcp.classify as classify
from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp.tools import risk

NOW = int(time.time())
DAY = 86400
LOSS_KEY = "ping.loss.check"
RTT_KEY = "ping.rtt.check"
CONN_KEY = "conn.count.check"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("ZABBIX_PRODUCT_MAP", raising=False)
    monkeypatch.setattr(classify, "_PRODUCT_MAP", None)
    monkeypatch.setattr(risk, "KEY_PING_LOSS", LOSS_KEY)
    monkeypatch.setattr(risk, "KEY_PING_RTT", RTT_KEY)
    monkeypatch.setattr(risk, "KEY_CONNECTIONS", CONN_KEY)


def _host(hid, name, ip, group="app_free"):
    return {"hostid": hid, "host": name, "groups": [{"name": group}],
            "interfaces": [{"ip": ip}]}


def _ipchange(hid, clock, old="198.51.100.200", new="198.51.100.201"):
    return {"resourceid": hid, "clock": str(clock),
            "details": f'[["update", "interfaces.1.ip", "{old}", "{new}"]]'}


HOSTS = [
    _host("1", "srv-aq9001", "198.51.100.1"),
    _host("2", "srv-aq9002", "198.51.100.2"),
    _host("3", "srv-hm9003", "203.0.113.3"),
    _host("4", "srv-tf9004", "192.0.2.4"),
    _host("5", "srv-aq9005", "198.51.100.5"),
    _host("6", "srv-aq9006-test", "198.51.100.6"),
]
AUDIT = [
    _ipchange("1", NOW - 2 * DAY),
    _ipchange("5", NOW - 1 * DAY),
    {"resourceid": "2", "clock": str(NOW), "details": '[["update", "host.name", "a", "b"]]'},
    _ipchange("4", "x"),                      # unparseable clock — skipped
]
ITEMS = [
    {"itemid": "L3", "hostid": "3", "key_": LOSS_KEY},
    {"itemid": "R3", "hostid": "3", "key_": RTT_KEY},
    {"itemid": "L4", "hostid": "4", "key_": LOSS_KEY},
]
TRENDS = [
    {"itemid": "L3", "clock": str(NOW - 5 * DAY), "value_avg": "0"},
    {"itemid": "L3", "clock": str(NOW - 3600), "value_avg": "10"},
    {"itemid": "R3", "clock": str(NOW - 5 * DAY), "value_avg": "50"},
    {"itemid": "R3", "clock": str(NOW - 3600), "value_avg": "50"},
    {"itemid": "L4", "clock": str(NOW - 5 * DAY), "value_avg": "0"},
    {"itemid": "L4", "clock": str(NOW - 3600), "value_avg": "0"},   # ok — not ranked
]


def _risk_client(hosts=HOSTS, audit=AUDIT, items=ITEMS, trends=TRENDS):
    return RecordingClient({"host.get": hosts, "auditlog.get": audit,
                            "item.get": items, "trend.get": trends})


class TestAtRiskHosts:
    def test_ranking_and_wire(self):
        c = _risk_client()
        out = run_tool(risk, "get_at_risk_hosts", c, top=3)
        assert "**Top 3 at-risk hosts** (4 ranked, window 7d, ping 14d)" in out
        lines = [ln for ln in out.splitlines() if ln.startswith("| ") and "Host" not in ln]
        assert lines[0] == "| 1 | srv-hm9003 | 5.26 | 0 | new-loss | — |"
        assert lines[1] == "| 2 | srv-aq9002 | 3.90 | 2 | n/a | — |"
        assert lines[2] == "| 3 | srv-aq9001 | 1.59 | 1 | n/a | 2 |"
        assert "*1 more omitted*" in out
        assert "1 test host(s) excluded: srv-aq9006-test" in out
        assert "srv-tf9004" not in out                # drift ok, no peers
        a = c.sent("auditlog.get")
        assert a["filter"] == {"resourcetype": 4, "action": 1,
                               "resourceid": ["1", "2", "3", "4", "5"]}
        assert a["time_from"] == pytest.approx(int(time.time()) - 7 * DAY, abs=5)
        assert c.sent("item.get")["filter"] == {"key_": [LOSS_KEY, RTT_KEY], "status": "0"}
        t = c.sent("trend.get")
        assert t["itemids"] == ["L3", "L4", "R3"]
        assert t["limit"] == 3 * 14 * 24

    def test_country_filter(self):
        out = run_tool(risk, "get_at_risk_hosts", _risk_client(), country="hm")
        assert "(1 ranked" in out and "srv-hm9003" in out
        assert "srv-aq9002" not in out

    def test_include_test_keeps_test_host(self):
        c = _risk_client()
        out = run_tool(risk, "get_at_risk_hosts", c, include_test=True)
        assert "test host(s) excluded" not in out
        assert "srv-aq9006-test" in out              # now a peer with churn
        assert "6" in c.sent("auditlog.get")["filter"]["resourceid"]

    def test_empty_after_filter(self):
        out = run_tool(risk, "get_at_risk_hosts", _risk_client(), country="PN")
        assert out.startswith("No matching hosts.")
        assert "srv-aq9006-test" in out              # exclusion still named

    def test_nothing_ranked_without_signal(self, monkeypatch):
        monkeypatch.setattr(risk, "KEY_PING_LOSS", "")
        monkeypatch.setattr(risk, "KEY_PING_RTT", "")
        c = _risk_client(audit=[])
        out = run_tool(risk, "get_at_risk_hosts", c)
        assert out.startswith("No at-risk hosts ranked > 0 (5 hosts inspected).")
        assert not any(m in ("item.get", "trend.get") for m, _ in c.calls)

    def test_no_items_skips_trend_fetch(self):
        c = _risk_client(items=[])
        out = run_tool(risk, "get_at_risk_hosts", c)
        assert not any(m == "trend.get" for m, _ in c.calls)
        assert "srv-hm9003" not in out               # no drift without items

    def test_api_error(self):
        def boom(p):
            raise httpx.ConnectError("down")
        out = run_tool(risk, "get_at_risk_hosts", RecordingClient({"host.get": boom}))
        assert out == "Error: down"


ANCHOR = NOW - 7200
COHORT = [
    _host("1", "srv-aq9001", "198.51.100.1"),
    _host("2", "srv-aq9002", "198.51.100.2"),
    _host("3", "srv-aq9003", "198.51.100.3"),
    _host("4", "srv-aq9004", "198.51.100.4"),
    _host("7", "srv-aq9007", "198.51.100.7"),
    _host("5", "srv-aq9005", "198.51.100.5", "app_paid"),   # other product
    _host("6", "srv-bv9006", "198.51.100.6"),               # other country
]
CONN_ITEMS = [
    {"itemid": "c2", "hostid": "2", "value_type": "3"},
    {"itemid": "c3", "hostid": "3", "value_type": "0"},
    {"itemid": "c4", "hostid": "4", "value_type": "3"},
    {"itemid": "c7", "hostid": "7", "value_type": "3"},     # no history → n/a
]
PRE = {"c2": 100, "c3": 100, "c4": 100}
POST = {"c2": 150, "c3": 50, "c4": 105}


def _history(p):
    table = PRE if p["time_till"] == ANCHOR else POST
    return [{"itemid": i, "value": str(table[i])} for i in p["itemids"] if i in table]


def _blast_client(hosts=COHORT, items=CONN_ITEMS):
    return RecordingClient({"host.get": hosts, "item.get": items, "history.get": _history})


class TestBlastRadius:
    def test_happy_path(self):
        c = _blast_client()
        out = run_tool(risk, "get_disruption_blast_radius", c, host="srv-aq9001",
                       drop_clock=ANCHOR, window_min=60)
        assert "cohort app_free/Default/AQ, 4 peers)" in out
        assert "Summary: 1 draining, 1 absorbing, 1 stable, 1 n/a." in out
        rows = [ln for ln in out.splitlines() if ln.startswith("| srv-")]
        assert rows == [
            "| srv-aq9003 | draining | -50% | 100 | 50 |",
            "| srv-aq9002 | absorbing | +50% | 100 | 150 |",
            "| srv-aq9004 | stable | +5% | 100 | 105 |",
            "| srv-aq9007 | n/a | — | — | — |",
        ]
        assert c.sent("item.get")["hostids"] == ["2", "3", "4", "7"]
        assert c.sent("item.get")["filter"] == {"key_": CONN_KEY, "status": "0"}
        hist = [p for m, p in c.calls if m == "history.get"]
        # one call per value type per window, ADR 097
        assert sorted((p["history"], p["time_from"], tuple(p["itemids"])) for p in hist) == sorted([
            (3, ANCHOR - 3600, ("c2", "c4", "c7")), (0, ANCHOR - 3600, ("c3",)),
            (3, ANCHOR, ("c2", "c4", "c7")), (0, ANCHOR, ("c3",)),
        ])
        post = [p for p in hist if p["time_from"] == ANCHOR]
        assert all(p["time_till"] == ANCHOR + 3600 for p in post)

    def test_default_anchor_clamps_post_window_to_now(self):
        c = _blast_client()
        run_tool(risk, "get_disruption_blast_radius", c, host="srv-aq9001", window_min=120)
        hist = [p for m, p in c.calls if m == "history.get"]
        anchor = min(p["time_till"] for p in hist)          # pre window ends at the anchor
        assert anchor == pytest.approx(int(time.time()) - 3600, abs=5)    # default: now - 1h
        post = [p for p in hist if p["time_from"] == anchor]
        # anchor + 120 min would be in the future: clamped to now
        assert all(p["time_till"] == pytest.approx(int(time.time()), abs=5) for p in post)

    def test_argument_and_config_guards(self, monkeypatch):
        assert run_tool(risk, "get_disruption_blast_radius", _blast_client(), host="") == \
            "Argument `host` is required."
        monkeypatch.setattr(risk, "KEY_CONNECTIONS", "")
        c = _blast_client()
        out = run_tool(risk, "get_disruption_blast_radius", c, host="srv-aq9001")
        assert out.startswith("Not configured.")
        assert c.calls == []

    def test_host_not_found(self):
        out = run_tool(risk, "get_disruption_blast_radius", _blast_client(), host="srv-pn9999")
        assert out == "Host not found: srv-pn9999"

    def test_no_cohort(self):
        out = run_tool(risk, "get_disruption_blast_radius", _blast_client(), host="srv-bv9006")
        assert out == "No cohort peers for srv-bv9006 (product=app_free, tier=Default, country=BV)."

    def test_no_items(self):
        out = run_tool(risk, "get_disruption_blast_radius", _blast_client(items=[]),
                       host="srv-aq9001")
        assert out == "No connection-count items found on 4 cohort peers."

    def test_api_error(self):
        def boom(p):
            raise ValueError("bad")
        out = run_tool(risk, "get_disruption_blast_radius",
                       RecordingClient({"host.get": boom}), host="srv-aq9001")
        assert out == "Error: bad"


def test_history_avg_without_types_queries_both_tables_and_skips_garbage():
    c = RecordingClient({"history.get": lambda p: (
        [{"itemid": "a", "value": "4"}, {"itemid": "a", "value": "x"}] if p["history"] == 3
        else [{"itemid": "a", "value": "2"}])})
    out = asyncio.run(risk._history_avg(c, ["a"], 1, 2))
    assert out == {"a": 3.0}
    assert sorted(p["history"] for _, p in c.calls) == [0, 3]
    assert asyncio.run(risk._history_avg(c, [], 1, 2)) == {}
