"""ADR 143: ``get_app_view`` wire test — the map is given, the join is by IP,
every seam between the two is named. Fixtures are synthetic."""

import time
from datetime import datetime, timedelta, timezone

import httpx

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp.app_map import APP_MAP_ENV, parse_app_map
from zbbx_mcp.tools import app_view
from zbbx_mcp.tools.app_view import entry_stats, fmt_age, rollup, scope_entries

NOW = int(time.time())


def _map(generated_at=None, predicate="active and heartbeat within 300 s"):
    ts = generated_at or datetime.now(timezone.utc) - timedelta(minutes=4)
    return parse_app_map({
        "generated_at": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "db",
        "predicate": predicate,
        "audiences": {
            "default": {"sections": [
                {"name": "Free", "entries": [
                    {"key": "aq_free", "title": "Base – Free", "code": "AQ",
                     "members": [{"ip": "198.51.100.1", "load": 0.21, "clients": 34},
                                 {"ip": "198.51.100.2"},
                                 {"ip": "198.51.100.9"}],       # nobody in Zabbix
                     "display_load": 0.04},
                    {"key": "bv_free", "title": "Bouvet – Free", "code": "BV",
                     "members": [{"ip": "198.51.100.3"}]},
                ]},
                {"name": "Paid", "entries": [
                    {"key": "hm_plus", "title": "Heard – Plus", "code": "auto",
                     "members": [{"ip": "203.0.113.1"}]},        # a SECOND interface
                ]},
            ]},
            "beta": {"sections": []},
        },
    })


def _host(hid, name, *ips):
    return {"hostid": hid, "host": name, "interfaces": [{"ip": ip} for ip in ips]}


HOSTS = [
    _host("1", "srv-aq9001", "198.51.100.1"),
    _host("2", "srv-aq9002", "198.51.100.2"),                    # agent silent
    _host("3", "srv-bv9001", "198.51.100.3"),
    _host("4", "srv-hm9001", "198.51.100.4", "203.0.113.1"),
    _host("5", "srv-tf9001", "198.51.100.5"),                    # not in the map
]


def _traffic(hid, key, bps, clock=NOW):
    return {"itemid": f"t{hid}{key}", "hostid": hid, "key_": key, "lastvalue": str(bps), "lastclock": str(clock)}


TRAFFIC = [
    _traffic("1", "net.if.in[eth0]", 8_000_000),
    _traffic("1", "net.if.in[docker0]", 99_000_000),             # virtual — must not count
    _traffic("2", "net.if.in[eth0]", 2_000_000),
    _traffic("3", "net.if.in[eth0]", 4_000_000),
    _traffic("3", "net.if.in[eth1]", 1_000_000),                 # carrier = busiest NIC
    _traffic("4", "net.if.in[eth0]", 6_000_000),
    _traffic("5", "net.if.in[eth0]", 50_000_000),                # unmapped host — never fetched
]
CPU = [  # idle → used: 40 / 70 / 50; host 2 has no reading at all
    {"hostid": "1", "lastvalue": "60", "lastclock": str(NOW)},
    {"hostid": "3", "lastvalue": "30", "lastclock": str(NOW)},
    {"hostid": "4", "lastvalue": "50", "lastclock": str(NOW)},
]


def _client():
    def items(p):
        if p.get("filter", {}).get("key_") == "system.cpu.util[,idle]":
            return [c for c in CPU if c["hostid"] in p["hostids"]]
        return [t for t in TRAFFIC if t["hostid"] in p["hostids"]]
    return RecordingClient({"host.get": HOSTS, "item.get": items})


def _given(monkeypatch, app_map):
    monkeypatch.setattr(app_view, "load_app_map", lambda source=None: (app_map, ""))


class TestPure:
    def test_scope_is_exact_on_section_and_entry(self):
        aud = _map().audiences["default"]
        assert [e.key for _s, e in scope_entries(aud)] == ["aq_free", "bv_free", "hm_plus"]
        assert [e.key for _s, e in scope_entries(aud, section="Free")] == ["aq_free", "bv_free"]
        assert scope_entries(aud, section="Fre") == []                       # exact, not prefix
        assert [e.key for _s, e in scope_entries(aud, entry="Base – Free")] == ["aq_free"]  # title works
        assert scope_entries(aud, entry="aq") == []

    def test_entry_stats_names_silent_and_keeps_coverage(self):
        matched = [{"hostid": "1", "host": "a"}, {"hostid": "2", "host": "b"}, {"hostid": "3", "host": "c"}]
        st = entry_stats(matched, ["198.51.100.9"], {"1": 8e6, "2": 2e6}, {"1": 40.0, "3": 70.0})
        assert (st["matched"], st["covered"], st["bps"], st["median_bps"]) == (3, 2, 10e6, 5e6)
        assert (st["cpu_med"], st["cpu_max"]) == (55.0, 70.0)
        assert st["silent"] == ["b"]
        assert st["unmatched"] == ["198.51.100.9"]

    def test_rollup_medians_over_hosts_not_over_entries(self):
        a = entry_stats([{"hostid": "1", "host": "a"}], [], {"1": 8e6}, {"1": 40.0})
        b = entry_stats([{"hostid": "2", "host": "b"}, {"hostid": "3", "host": "c"}], ["x"], {"2": 2e6, "3": 4e6}, {})
        r = rollup([a, b])
        assert (r["entries"], r["members"], r["matched"], r["unmatched"]) == (2, 4, 3, 1)
        assert (r["bps"], r["median_bps"], r["silent"]) == (14e6, 4e6, 2)

    def test_fmt_age(self):
        assert fmt_age(42) == "42 s"
        assert fmt_age(240) == "4 min"
        assert fmt_age(3900) == "1 h 05 min"
        assert fmt_age(90000) == "1 d 1 h"


class TestGetAppViewWire:
    def test_full_audience(self, monkeypatch):
        _given(monkeypatch, _map())
        out = run_tool(app_view, "get_app_view", _client())
        assert out.startswith("App view — audience `default`"), out
        assert "_Map generated " in out and "(4 min ago), source: db; rows satisfy: active and heartbeat within 300 s._" in out
        assert "Stale map" not in out
        # members / matched / unmatched, carrier traffic, median, CPU, silent, display load, map clients
        assert "| Free / Base – Free (`aq_free`) | AQ | 3 | 2 | 1 | 10 Mbps | 5 Mbps | 40% / 40% | 1 | 0.04 | 34 |" in out, out
        assert "| Free / Bouvet – Free (`bv_free`) | BV | 1 | 1 | 0 | 4 Mbps | 4 Mbps | 70% / 70% | 0 | – | – |" in out, out
        assert "| Paid / Heard – Plus (`hm_plus`) | auto | 1 | 1 | 0 | 6 Mbps | 6 Mbps | 50% / 50% | 0 | – | – |" in out, out
        # the seams are named, not absorbed
        assert "**Unmatched members**" in out and "- `aq_free`: 198.51.100.9" in out
        assert "**Agent not reporting**" in out and "- `aq_free`: srv-aq9002" in out
        # rollups: section, then audience
        assert "| **Section Free** | 2 | 4 | 3 | 1 | 14 Mbps | 4 Mbps | 55% / 70% | 1 |" in out, out
        assert "| **Section Paid** | 1 | 1 | 1 | 0 | 6 Mbps | 6 Mbps | 50% / 50% | 0 |" in out, out
        assert "| **Audience default** | 3 | 5 | 4 | 1 | 20 Mbps | 5 Mbps | 50% / 70% | 1 |" in out, out
        assert "Top 4 matched servers by traffic:" in out
        assert "- srv-aq9001 — 8 Mbps — `aq_free`" in out
        assert "srv-tf9001" not in out                       # not in the map — not in the view

    def test_members_without_an_address_are_counted_and_named(self, monkeypatch):
        # The exporter writes "ip": null for a member whose address column is
        # NULL. Offered by the product, joinable to nothing: it is in the
        # members count and in a note, never in matched/unmatched.
        _given(monkeypatch, parse_app_map({
            "generated_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "predicate": "heartbeat within 300 s",
            "audiences": {"default": {"sections": [{"name": "Free", "entries": [
                {"key": "aq_free", "title": "Base – Free", "code": "AQ",
                 "members": [{"ip": "198.51.100.1"}, {"ip": None, "load": 0.5}]},
            ]}]}},
        }))
        out = run_tool(app_view, "get_app_view", _client())
        assert "| Free / Base – Free (`aq_free`) | AQ | 2 | 1 | 0 |" in out, out
        assert "**Members without an address**" in out and "- `aq_free`: ×1" in out
        assert "| **Section Free** | 1 | 2 | 1 | 0 |" in out, out
        assert "**Unmatched members**" not in out

    def test_second_interface_joins(self, monkeypatch):
        _given(monkeypatch, _map())
        out = run_tool(app_view, "get_app_view", _client(), section="Paid")
        assert "(`hm_plus`) | auto | 1 | 1 | 0 |" in out, out
        assert "aq_free" not in out

    def test_wire_contract(self, monkeypatch):
        _given(monkeypatch, _map())
        c = _client()
        run_tool(app_view, "get_app_view", c, entry="aq_free")
        assert c.sent("host.get")["selectInterfaces"] == ["ip"]
        traffic_calls = [p for m, p in c.calls if m == "item.get" and "lastclock" in (p.get("output") or [])]
        assert traffic_calls, "traffic items must be fetched with lastclock (ADR 137)"
        # Only the matched hosts are queried — never the whole fleet.
        for m, p in c.calls:
            if m == "item.get":
                assert set(p["hostids"]) == {"1", "2"}, p

    def test_stale_map_is_flagged_against_the_predicate_window(self, monkeypatch):
        _given(monkeypatch, _map(generated_at=datetime.now(timezone.utc) - timedelta(minutes=42)))
        out = run_tool(app_view, "get_app_view", _client())
        assert "**Stale map** — 42 min old against a 300 s window" in out, out

    def test_default_window_when_predicate_names_none(self, monkeypatch):
        _given(monkeypatch, _map(generated_at=datetime.now(timezone.utc) - timedelta(minutes=20), predicate="active"))
        out = run_tool(app_view, "get_app_view", _client())
        assert "against a 900 s window" in out, out
        _given(monkeypatch, _map(generated_at=datetime.now(timezone.utc) - timedelta(minutes=10), predicate="active"))
        assert "Stale map" not in run_tool(app_view, "get_app_view", _client())

    def test_missing_map_names_the_variable_and_the_reason(self, monkeypatch):
        monkeypatch.setattr(app_view, "load_app_map", lambda source=None: (None, f"{APP_MAP_ENV} is not set"))
        c = _client()
        out = run_tool(app_view, "get_app_view", c)
        assert out.startswith(f"No app map available — {APP_MAP_ENV} is not set."), out
        assert APP_MAP_ENV in out and "ADR 143" in out
        assert c.calls == []                                   # nothing to join against, nothing fetched

    def test_unknown_audience_lists_the_ones_that_exist(self, monkeypatch):
        _given(monkeypatch, _map())
        out = run_tool(app_view, "get_app_view", _client(), audience="nope")
        assert out == "Audience `nope` is not in the map. Audiences: `default`, `beta`."

    def test_unknown_section_and_entry_list_what_exists(self, monkeypatch):
        _given(monkeypatch, _map())
        out = run_tool(app_view, "get_app_view", _client(), section="Fre")
        assert out == "Section `Fre` is not in audience `default`. Sections: `Free`, `Paid`."
        out = run_tool(app_view, "get_app_view", _client(), section="Free", entry="aq")
        assert out == "Entry `aq` is not in section `Free`. Entries: `aq_free`, `bv_free`."

    def test_empty_audience_means_default(self, monkeypatch):
        _given(monkeypatch, _map())
        out = run_tool(app_view, "get_app_view", _client(), audience="")
        assert out.startswith("App view — audience `default`")

    def test_api_error_is_a_message(self, monkeypatch):
        _given(monkeypatch, _map())

        def boom(p):
            raise httpx.ConnectError("boom")
        out = run_tool(app_view, "get_app_view", RecordingClient({"host.get": boom}))
        assert out == "Error building app view: boom"
