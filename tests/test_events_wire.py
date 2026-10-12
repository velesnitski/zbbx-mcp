"""Wire tests for the event tools: ``get_events``, ``get_trends``,
``get_event_frequency``, ``get_correlated_events``, ``get_error_rate`` and
``get_incident_report``. Fixtures are synthetic."""

import time

import httpx
import pytest

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp.formatters import _ts
from zbbx_mcp.tools import events

NOW = int(time.time())
FRONTEND = "https://zabbix.example.com"


def _boom(p):
    raise httpx.ConnectError("unreachable")


def _client(responses):
    c = RecordingClient(responses)
    c.frontend_url = FRONTEND
    return c


# --- get_events ----------------------------------------------------------------

EVENTS = [
    {"eventid": "101", "value": "1", "severity": "4", "clock": str(NOW - 60),
     "acknowledged": "1", "name": "High CPU on srv-aq9001"},
    {"eventid": "102", "value": "0", "severity": "2", "clock": str(NOW - 120),
     "acknowledged": "0", "name": "Disk low on srv-bv9002"},
    {"eventid": "103", "value": "1", "severity": "5", "clock": str(NOW - 180),
     "acknowledged": "0", "name": "Agent down on srv-hm9003"},
]


class TestGetEvents:
    def test_happy_path_with_filters(self):
        c = _client({"event.get": EVENTS, "hostgroup.get": [{"groupid": "7"}]})
        out = run_tool(events, "get_events", c, host_id="11", group="app_free",
                       time_from="1700000000", time_till="1700003600", max_results=3)
        assert out.startswith("**Found: 3 events** (showing first 3, more may exist)")
        assert "- **[High]** High CPU on srv-aq9001 [PROBLEM] [ACK]" in out
        assert "- **[Warning]** Disk low on srv-bv9002 [OK] —" in out
        assert "(eventid: 103)" in out
        p = c.sent("event.get")
        assert p["hostids"] == ["11"] and p["groupids"] == ["7"]
        assert p["time_from"] == 1700000000 and p["time_till"] == 1700003600
        assert p["limit"] == 3 and p["source"] == 0
        assert c.sent("hostgroup.get")["filter"] == {"name": ["app_free"]}

    def test_discovery_source_value_labels(self):
        ev = [{"eventid": "1", "value": "2", "severity": "0", "clock": str(NOW)}]
        c = _client({"event.get": ev})
        out = run_tool(events, "get_events", c, source=1)
        assert "**Found: 1 events**\n" in out
        assert "[Discovered]" in out
        assert c.sent("event.get")["source"] == 1

    def test_empty(self):
        c = _client({"event.get": []})
        assert run_tool(events, "get_events", c) == "No events found."
        assert "hostids" not in c.sent("event.get")

    def test_unknown_group(self):
        c = _client({"hostgroup.get": []})
        assert run_tool(events, "get_events", c, group="nope") == "Host group 'nope' not found."
        assert not any(m == "event.get" for m, _ in c.calls)

    def test_bad_time_is_an_error(self):
        out = run_tool(events, "get_events", _client({}), time_from="yesterday")
        assert out.startswith("Error querying Zabbix: invalid literal")


# --- get_trends ------------------------------------------------------------------

ITEM = [{"itemid": "55", "name": "Inbound", "key_": "net.if.in[eth0]",
         "units": "bps", "value_type": "3"}]


def _trend(clock, avg):
    return {"clock": str(clock), "value_min": str(avg - 1), "value_avg": str(avg),
            "value_max": str(avg + 1), "num": "60"}


class TestGetTrends:
    def test_default_window_newest_first(self):
        rows = [_trend(NOW - 7200, 10), _trend(NOW - 3600, 20), _trend(NOW - 10800, 5)]
        c = _client({"item.get": ITEM, "trend.get": rows})
        out = run_tool(events, "get_trends", c, item_id="55", limit=24)
        assert "# Trends: Inbound" in out
        assert "**Key:** `net.if.in[eth0]`" in out
        assert "**Records:** 3 (hourly aggregates)" in out
        assert "Window clipped" not in out
        body = [ln for ln in out.splitlines() if ln.startswith("| ") and "Time" not in ln]
        assert [ln.split(" | ")[0][2:] for ln in body] == [
            _ts(str(NOW - 3600)), _ts(str(NOW - 7200)), _ts(str(NOW - 10800))]
        p = c.sent("trend.get")
        assert "sortfield" not in p                     # ADR 096
        assert p["time_till"] == pytest.approx(int(time.time()), abs=5)
        assert p["time_till"] - p["time_from"] == 24 * 3600
        assert p["limit"] == 24 and p["itemids"] == ["55"]

    def test_explicit_range_clipped_is_announced(self):
        c = _client({"item.get": ITEM, "trend.get": [_trend(1700000000, 1)]})
        till = 1700000000
        out = run_tool(events, "get_trends", c, item_id="55",
                       time_from=str(till - 7 * 86400), time_till=str(till), limit=10)
        assert "⚠ **Window clipped**: you asked for ~168h but `limit=10`" in out
        p = c.sent("trend.get")
        assert p["time_till"] == till and p["time_from"] == till - 10 * 3600

    def test_explicit_range_inside_limit_is_kept(self):
        c = _client({"item.get": ITEM, "trend.get": [_trend(1700000000, 1)]})
        out = run_tool(events, "get_trends", c, item_id="55",
                       time_from="1699996400", time_till="1700000000", limit=50)
        assert "Window clipped" not in out
        assert c.sent("trend.get")["time_from"] == 1699996400

    def test_unparseable_time_refused(self):
        c = _client({"item.get": ITEM})
        out = run_tool(events, "get_trends", c, item_id="55", time_from="now-7d")
        assert out.startswith("Unrecognised time value(s): time_from='now-7d'.")
        assert not any(m == "trend.get" for m, _ in c.calls)

    def test_item_missing(self):
        assert run_tool(events, "get_trends", _client({"item.get": []}), item_id="9") == \
            "Item '9' not found."

    def test_no_trend_data(self):
        out = run_tool(events, "get_trends", _client({"item.get": ITEM}), item_id="55")
        assert out == "No trend data for item 'Inbound'."

    def test_api_error(self):
        out = run_tool(events, "get_trends", _client({"item.get": _boom}), item_id="55")
        assert out == "Error querying Zabbix: unreachable"


# --- frequency / correlation ------------------------------------------------------

def _pe(eid, clock, trig, name, host=None, sev="3"):
    e = {"eventid": eid, "clock": str(clock), "objectid": trig, "name": name, "severity": sev}
    if host is not None:
        e["hosts"] = [{"hostid": host, "host": host}]
    return e


FLAP = [
    _pe("1", NOW - 600, "t1", "Ping loss", "srv-aq9001", "4"),
    _pe("2", NOW - 1200, "t1", "Ping loss", "srv-aq9001", "4"),
    _pe("3", NOW - 1800, "t1", "Ping loss", "srv-aq9001", "4"),
    _pe("4", NOW - 100, "t2", "Agent down", "srv-bv9002"),
    _pe("5", NOW - 3 * 3600, "t2", "Agent down", "srv-bv9002"),
    _pe("6", NOW - 6 * 3600, "t2", "Agent down", "srv-bv9002"),
    _pe("7", NOW - 50, "t3", "Disk low", "srv-hm9003"),
    _pe("8", NOW - 70, "t4", "Orphan event"),
]


class TestEventFrequency:
    def test_flapping_ranked(self):
        c = _client({"event.get": FLAP, "hostgroup.get": [{"groupid": "3"}]})
        out = run_tool(events, "get_event_frequency", c, hours=12, group="app_paid")
        assert out.startswith("**2 flapping triggers** (last 12h, min 3 events)")
        assert "| srv-aq9001 | Ping loss | High | 3 | 10m |" in out
        assert "| srv-bv9002 | Agent down | Average | 3 | 3.0h |" in out
        assert "*2 more triggers with < 3 events omitted*" in out
        p = c.sent("event.get")
        assert p["value"] == 1 and p["groupids"] == ["3"]
        assert p["time_from"] == pytest.approx(int(time.time()) - 12 * 3600, abs=5)

    def test_single_event_interval_dash(self):
        out = run_tool(events, "get_event_frequency", _client({"event.get": FLAP}),
                       min_events=1, max_results=10)
        assert "| ? | Orphan event | Average | 1 | – |" in out

    def test_nothing_flapping(self):
        out = run_tool(events, "get_event_frequency", _client({"event.get": FLAP}), min_events=9)
        assert out == "No flapping detected (min 9 events in 24h). 8 total events checked."

    def test_empty_and_missing_group(self):
        assert run_tool(events, "get_event_frequency", _client({"event.get": []}), hours=2) == \
            "No problem events in the last 2h."
        assert run_tool(events, "get_event_frequency", _client({}), group="x") == \
            "Host group 'x' not found."

    def test_api_error(self):
        assert run_tool(events, "get_event_frequency", _client({"event.get": _boom})) == \
            "Error: unreachable"


CORR = [
    _pe("1", NOW - 3000, "t1", "Ping loss on srv-aq9001", "srv-aq9001", "4"),
    _pe("2", NOW - 2900, "t2", "Ping loss on srv-aq9002", "srv-aq9002", "5"),
    _pe("3", NOW - 2800, "t3", "Ping loss on srv-aq9003", "srv-aq9003", "4"),
    _pe("4", NOW - 100, "t1", "Ping loss on srv-aq9001", "srv-aq9001", "4"),   # outside window
    _pe("5", NOW - 500, "t5", "Agent down", "srv-bv9002"),                    # single host
    _pe("6", NOW - 400, "t6", "   "),                                         # blank name
]


class TestCorrelatedEvents:
    def test_cluster_found(self):
        c = _client({"event.get": CORR})
        out = run_tool(events, "get_correlated_events", c, window_minutes=10)
        assert out.startswith("**1 correlated incidents** (last 24h, 10m window)")
        assert "**[Disaster]** Ping loss" in out
        assert f"3 hosts at {_ts(str(NOW - 3000))}: srv-aq9001, srv-aq9002, srv-aq9003" in out

    def test_more_than_eight_hosts_truncated(self):
        many = [_pe(str(i), NOW - 100 - i, f"t{i}", "Ping loss", f"srv-sj{9000 + i}")
                for i in range(10)]
        out = run_tool(events, "get_correlated_events", _client({"event.get": many}))
        assert "10 hosts at" in out and "+2 more" in out

    def test_none_correlated(self):
        out = run_tool(events, "get_correlated_events", _client({"event.get": CORR}), min_hosts=5)
        assert out == ("No correlated events found (6 events across 2 triggers, "
                       "window 10m, min 5 hosts).")

    def test_empty_missing_group_error(self):
        assert run_tool(events, "get_correlated_events", _client({"event.get": []})) == \
            "No problem events in the last 24h."
        c = _client({})
        assert run_tool(events, "get_correlated_events", c, group="g") == "Host group 'g' not found."
        assert run_tool(events, "get_correlated_events", _client({"event.get": _boom})) == \
            "Error: unreachable"

    def test_group_filter_sent(self):
        c = _client({"event.get": CORR, "hostgroup.get": [{"groupid": "9"}]})
        run_tool(events, "get_correlated_events", c, group="app_free")
        assert c.sent("event.get")["groupids"] == ["9"]


# --- error rate / incident report ----------------------------------------------------

def _rate_events():
    return [
        # srv-aq9001: 3 recent, 1 older → WORSE
        *[_pe(f"a{i}", NOW - 600 * (i + 1), "t1", "High CPU", "srv-aq9001", "4") for i in range(3)],
        _pe("a9", NOW - 20 * 3600, "t1", "High CPU", "srv-aq9001", "4"),
        # srv-bv9002: only recent → NEW
        _pe("b1", NOW - 300, "t2", "Agent down", "srv-bv9002"),
        # srv-hm9003: 0 recent, 2 older → better
        _pe("c1", NOW - 20 * 3600, "t3", "Disk low", "srv-hm9003"),
        _pe("c2", NOW - 21 * 3600, "t3", "Disk low", "srv-hm9003"),
        # no hosts: resolved via trigger.get
        _pe("d1", NOW - 200, "t4", "Ping loss"),
        # no hosts, unknown trigger, name carries host
        _pe("e1", NOW - 200, "t5", "Link flap on srv-tf9004"),
        # no hosts, unknown trigger, bare name
        _pe("f1", NOW - 200, "", "Mystery"),
    ]


TRIG_HOSTS = [{"triggerid": "t4", "hosts": [{"host": "srv-gs9005"}]},
              {"triggerid": "t5", "hosts": []}]


class TestErrorRate:
    def test_ranked_with_trends_and_links(self):
        c = _client({"event.get": _rate_events(), "trigger.get": TRIG_HOSTS})
        out = run_tool(events, "get_error_rate", c, hours=24, severity_min=3)
        assert out.startswith("**Error rate (24h, sev>=3): 6 servers, 10 events**")
        assert (f"- **srv-aq9001** 4 err (3/1 WORSE) [→]({FRONTEND}/zabbix.php?"
                "action=search&search=srv-aq9001) — High CPU") in out
        assert "- **srv-bv9002** 1 err (1/0 NEW)" in out
        assert "- **srv-hm9003** 2 err (0/2 better)" in out
        assert "- **srv-gs9005** 1 err (1/0 NEW)" in out
        assert "- **srv-tf9004** 1 err" in out
        assert "- **[Mystery]** 1 err" in out
        p = c.sent("event.get")
        assert p["severities"] == [3, 4, 5]
        assert sorted(c.sent("trigger.get")["triggerids"]) == ["t4", "t5"]

    def test_all_hosts_known_skips_trigger_lookup(self):
        ev = [_pe("1", NOW - 60, "t1", "High CPU", "srv-aq9001", "4")]
        c = _client({"event.get": ev})
        out = run_tool(events, "get_error_rate", c, max_results=1)
        assert "srv-aq9001** 1 err (1/0 NEW)" in out
        assert not any(m == "trigger.get" for m, _ in c.calls)

    def test_empty_and_error(self):
        assert run_tool(events, "get_error_rate", _client({"event.get": []}), severity_min=4) == \
            "No errors (severity >= 4) in 24h."
        assert run_tool(events, "get_error_rate", _client({"event.get": _boom})) == \
            "Error: unreachable"


class TestIncidentReport:
    def test_correlated_and_top_errors(self):
        ev = _rate_events() + [
            _pe(f"z{i}", NOW - 1000 - i * 60, f"trg{i}", "Ping loss", f"srv-pn{9000 + i}", "5")
            for i in range(5)
        ] + [_pe("zz", NOW - 30000, "trg9", "Ping loss", "srv-pn9009", "5")]  # breaks cluster
        c = _client({"event.get": ev, "trigger.get": TRIG_HOSTS})
        out = run_tool(events, "get_incident_report", c, hours=24)
        assert out.startswith("**Incidents (24h): 16 events, 12 servers**")
        assert "**Correlated (3+ hosts, same trigger):**" in out
        # the trigger-resolved srv-gs9005 event lands >15 min later: not in the cluster
        assert (f"- Ping loss — 5 hosts at {_ts(str(NOW - 1240))}: "
                "srv-pn9000, srv-pn9001, srv-pn9002, srv-pn9003 +1") in out
        assert "**Top errors:**" in out
        assert "- **srv-aq9001** 4 (3/1 WORSE)" in out
        assert "- **srv-hm9003** 2 (0/2 better)" in out
        assert "- **srv-tf9004** 1 (1/0 NEW)" in out
        assert "- **[Mystery]** 1 (1/0 NEW)" in out
        assert c.sent("event.get")["severities"] == [3, 4, 5]

    def test_no_cluster(self):
        ev = [_pe("1", NOW - 60, "t1", "High CPU", "srv-aq9001", "4")]
        out = run_tool(events, "get_incident_report", _client({"event.get": ev}))
        assert "Correlated" not in out
        assert f"- **srv-aq9001** 1 (1/0 NEW) [→]({FRONTEND}/zabbix.php?action=search&search=srv-aq9001)" in out

    def test_empty_and_error(self):
        assert run_tool(events, "get_incident_report", _client({"event.get": []}), severity_min=5) == \
            "No incidents (sev>=5) in 24h."
        assert run_tool(events, "get_incident_report", _client({"event.get": _boom})) == \
            "Error: unreachable"
