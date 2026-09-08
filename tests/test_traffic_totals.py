"""ADR 137: exact filters, an honest connections count, and a total that names
what it could not count.

Fixtures use neutral names. Figures are illustrative.
"""

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp.data import label_matches
from zbbx_mcp.fetch import connections_from_items
from zbbx_mcp.tools import traffic, traffic_totals
from zbbx_mcp.tools.traffic_totals import carrier_traffic, summarise


class TestLabelMatches:
    def test_exact_case_insensitive(self):
        assert label_matches("Free", "free")
        assert label_matches(" Free ", "FREE")

    def test_a_prefix_is_not_a_match(self):
        # The regression: tier labels nest, and `in` matched every one of
        # them. Asking for one tier returned several with no indication.
        assert not label_matches("Free Plus", "Free")
        assert not label_matches("Free Proxy", "Free")
        assert not label_matches("Pro Max", "Pro")

    def test_empty_filter_matches_everything(self):
        assert label_matches("anything", "")
        assert label_matches(None, "")
        assert label_matches("", None)

    def test_missing_actual_never_matches_a_real_filter(self):
        assert not label_matches(None, "Free")
        assert not label_matches("", "Free")


def _conn(hid, value, clock=1_760_000_000):
    return {"hostid": hid, "lastvalue": str(value), "lastclock": str(clock)}


class TestConnectionsFromItems:
    def test_live_items_are_read(self):
        assert connections_from_items([_conn("1", 42)]) == {"1": 42.0}

    def test_never_collected_is_absent_not_zero(self):
        # lastclock=0 is the never-collected sentinel. Read blind, it became
        # 0.0 and a host moving hundreds of megabits printed "0 connections".
        assert connections_from_items([_conn("1", 0, clock=0)]) == {}
        assert "1" not in connections_from_items([_conn("1", "", clock=0)])

    def test_missing_lastclock_fails_closed(self):
        # A caller that forgot to request lastclock gets nothing, not zeros.
        assert connections_from_items([{"hostid": "1", "lastvalue": "7"}]) == {}

    def test_garbage_is_skipped_not_fatal(self):
        assert connections_from_items([{"hostid": "1", "lastvalue": "x", "lastclock": "5"}]) == {}
        assert connections_from_items(None) == {}


def _traffic(hid, value, clock=1_760_000_000, key="net.if.in[eth0]"):
    return {"itemid": f"{hid}{key}", "hostid": hid, "key_": key,
            "lastvalue": str(value), "lastclock": str(clock)}


class TestCarrierTraffic:
    def test_busiest_interface_is_the_carrier(self):
        per_host, never = carrier_traffic([
            _traffic("1", 100, key="net.if.in[eth0]"),
            _traffic("1", 900, key="net.if.in[bond0]"),
        ])
        assert per_host == {"1": 900.0}
        assert never == 0

    def test_never_collected_is_counted_and_skipped(self):
        per_host, never = carrier_traffic([
            _traffic("1", 900),
            _traffic("2", 0, clock=0),
        ])
        assert per_host == {"1": 900.0}
        assert never == 1


class TestSummarise:
    HOSTS = [{"hostid": "1", "host": "srv-hm01"}, {"hostid": "2", "host": "srv-bv01"},
             {"hostid": "3", "host": "srv-aq01"}]

    def test_uncovered_hosts_are_named_not_summed_as_zero(self):
        s = summarise(self.HOSTS, {"1": 8_000_000.0, "2": 2_000_000.0}, top=5)
        assert s["servers"] == 3
        assert s["covered"] == 2
        assert s["uncovered"] == ["srv-aq01"]
        assert s["total_mbps"] == 10.0
        # Average is over COVERED hosts, or the silent one would drag it down.
        assert s["avg_mbps_per_covered"] == 5.0

    def test_top_is_ranked_desc(self):
        s = summarise(self.HOSTS, {"1": 1_000_000.0, "2": 9_000_000.0}, top=1)
        assert s["top"] == [("srv-bv01", 9.0)]

    def test_nothing_covered_has_no_average(self):
        s = summarise(self.HOSTS, {}, top=5)
        assert s["covered"] == 0
        assert s["avg_mbps_per_covered"] is None
        assert s["total_mbps"] == 0.0


class TestGetTrafficTotalsWire:
    def _client(self):
        hosts = [
            {"hostid": "1", "host": "srv-hm01", "groups": [{"name": "app_free"}]},
            {"hostid": "2", "host": "srv-bv01", "groups": [{"name": "app_free"}]},
            {"hostid": "3", "host": "srv-aq01", "groups": [{"name": "app_free"}]},
        ]
        items = [
            _traffic("1", 8_000_000),
            _traffic("2", 2_000_000),
            _traffic("3", 0, clock=0),   # never collected
        ]
        return RecordingClient({"host.get": hosts, "item.get": lambda p: items})

    def test_total_with_explicit_coverage(self):
        out = run_tool(traffic_totals, "get_traffic_totals", self._client(), group="app_free")
        assert "10.0 Mbps" in out
        assert "2 of 3" in out, out
        assert "srv-aq01" in out, "the uncovered host must be named"
        assert "not counted as zero" in out
        assert "never collected" in out

    def test_group_filter_is_exact(self):
        out = run_tool(traffic_totals, "get_traffic_totals", self._client(), group="app")
        assert out.startswith("No enabled hosts match"), out

    def test_asks_for_lastclock(self):
        # The sentinel cannot be honoured unless it is fetched.
        client = self._client()
        run_tool(traffic_totals, "get_traffic_totals", client, group="app_free")
        assert "lastclock" in client.sent("item.get")["output"]


class TestAnomaliesNoLongerAssertsZeroConnections:
    CONN_KEY = "app.sessions"

    def test_a_never_collected_connections_item_does_not_crash_or_print_zero(self, monkeypatch):
        # Before: host_conns.get(hid, 0) rendered an unmeasured count as 0, and
        # after the fix a None must survive every comparison downstream.
        monkeypatch.setattr(traffic, "KEY_CONNECTIONS", self.CONN_KEY, raising=False)
        hosts = [{"hostid": "1", "host": "srv-hm01", "groups": [{"name": "app_free"}],
                  "interfaces": [{"ip": "10.0.0.1"}]}]

        def items(p):
            key = p.get("filter", {}).get("key_")
            if key == self.CONN_KEY:
                return [_conn("1", 0, clock=0)]
            if isinstance(key, list):
                return [_traffic("1", 5_000_000)]
            return []

        client = RecordingClient({"host.get": hosts, "item.get": items})
        out = run_tool(traffic, "detect_traffic_anomalies", client)
        assert "0 conns" not in out
        conn_calls = [pr for mth, pr in client.calls
                      if mth == "item.get" and pr.get("filter", {}).get("key_") == self.CONN_KEY]
        assert conn_calls, "connections fetch was not issued"
        assert "lastclock" in conn_calls[0]["output"], "sentinel cannot be honoured unless fetched"
