"""Wire tests for the disruption tools: service-port split, regional loss and
disruption waves. Driven through the real tool functions with a recording
client; every fixture is synthetic (documentation IPs, invented hostnames).
"""

from __future__ import annotations

import httpx
import pytest

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp import classify
from zbbx_mcp.tools import disruption

SERVICE_KEY = "svc.bps[test]"
MBPS = 1_000_000  # default bits/s config: raw / 1e6 = Mbps


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ZABBIX_PRODUCT_MAP", "")
    monkeypatch.setattr(classify, "_PRODUCT_MAP", None)
    monkeypatch.delenv("ZABBIX_TRAFFIC_UNIT", raising=False)


def _host(hid, name, ip, group="app_free"):
    return {"hostid": hid, "host": name, "interfaces": [{"ip": ip}],
            "groups": [{"name": group}]}


def _trends(baseline: dict, recent: dict):
    """trend.get responder: first call is the baseline window, second recent."""
    calls = []

    def respond(p):
        calls.append(p)
        src = baseline if len(calls) == 1 else recent
        out = []
        for iid in p["itemids"]:
            for v in src.get(iid, []):
                out.append({"itemid": iid, "value_avg": str(v)})
        return out
    return respond


def _raise(exc):
    def f(_p):
        raise exc
    return f


# --- detect_service_port_split ---------------------------------------------

SPLIT_HOSTS = [
    _host("1", "srv-aq9001", "192.0.2.1"),   # service collapsed, mgmt healthy → split
    _host("2", "srv-aq9002", "192.0.2.2"),   # both collapsed → full outage, not listed
    _host("3", "srv-bv9003", "198.51.100.3"),  # neither dropped → ok
    _host("4", "srv-hm9004", "203.0.113.4"),   # no service item at all
]
SPLIT_ITEMS = [
    {"itemid": "s1", "hostid": "1", "key_": SERVICE_KEY},
    {"itemid": "m1", "hostid": "1", "key_": "net.if.in[eth0]"},
    {"itemid": "s2", "hostid": "2", "key_": SERVICE_KEY},
    {"itemid": "m2", "hostid": "2", "key_": "net.if.in[eth0]"},
    {"itemid": "s3", "hostid": "3", "key_": SERVICE_KEY},
    {"itemid": "m3", "hostid": "3", "key_": "net.if.in[eth0]"},
    {"itemid": "m4", "hostid": "4", "key_": "net.if.in[eth0]"},
]
SPLIT_BASE = {"s1": [100 * MBPS], "m1": [200 * MBPS], "s2": [100 * MBPS], "m2": [200 * MBPS],
              "s3": [50 * MBPS], "m3": [80 * MBPS]}
SPLIT_RECENT = {"s1": [10 * MBPS], "m1": [195 * MBPS], "s2": [5 * MBPS], "m2": [20 * MBPS],
                "s3": [49 * MBPS], "m3": [80 * MBPS]}


class TestServicePortSplitWire:
    def _client(self, hosts=SPLIT_HOSTS, items=SPLIT_ITEMS, base=SPLIT_BASE, recent=SPLIT_RECENT):
        def item_get(p):
            # Honour an exact key filter (service item) and the NIC prefix
            # search, as Zabbix does.
            keys = p.get("filter", {}).get("key_")
            out = [it for it in items if it["hostid"] in p["hostids"]]
            if keys:
                return [it for it in out if it["key_"] in keys]
            if "search" in p:
                return [it for it in out if it["key_"].startswith("net.if.in[")]
            return out
        return RecordingClient({"host.get": hosts, "item.get": item_get,
                                "trend.get": _trends(base, recent)})

    def test_not_configured(self, monkeypatch):
        monkeypatch.setattr(disruption, "KEY_SERVICE_BPS", "")
        c = self._client()
        out = run_tool(disruption, "detect_service_port_split", c)
        assert out.startswith("Not configured")
        assert c.calls == []

    def test_split_reported_only_for_service_only_collapse(self, monkeypatch):
        monkeypatch.setattr(disruption, "KEY_SERVICE_BPS", SERVICE_KEY)
        c = self._client()
        out = run_tool(disruption, "detect_service_port_split", c, window_days=7, recent_days=1)
        assert "**1 hosts with service/mgmt traffic split**" in out
        assert "| srv-aq9001 | 192.0.2.1 | 90% | 2% | 100.0 → 10.0 Mbps |" in out
        assert "srv-aq9002" not in out and "srv-bv9003" not in out
        svc, nic = [p for m, p in c.calls if m == "item.get"]
        assert svc["hostids"] == nic["hostids"] == ["1", "2", "3", "4"]
        assert svc["filter"]["key_"] == [SERVICE_KEY]
        assert nic["search"] == {"key_": "*net.if.in[*"} and nic["searchWildcardsEnabled"]
        trend_calls = [p for m, p in c.calls if m == "trend.get"]
        assert len(trend_calls) == 2
        base, rec = trend_calls
        assert base["time_till"] == rec["time_from"]
        assert rec["time_till"] - rec["time_from"] == 86400
        assert base["time_till"] - base["time_from"] == 6 * 86400
        assert set(base["itemids"]) == {"s1", "s2", "s3", "m1", "m2", "m3", "m4"}
        assert base["limit"] == 7 * 24 * 14

    def test_country_filter_and_no_match(self, monkeypatch):
        monkeypatch.setattr(disruption, "KEY_SERVICE_BPS", SERVICE_KEY)
        c = self._client()
        out = run_tool(disruption, "detect_service_port_split", c, country="bv")
        assert out.startswith("No service-vs-mgmt split detected (1 hosts")
        assert c.sent("item.get")["hostids"] == ["3"]
        out = run_tool(disruption, "detect_service_port_split", self._client(), country="tf")
        assert out == "No matching hosts."

    def test_no_service_items(self, monkeypatch):
        monkeypatch.setattr(disruption, "KEY_SERVICE_BPS", SERVICE_KEY)
        c = self._client(items=[it for it in SPLIT_ITEMS if it["key_"] != SERVICE_KEY])
        out = run_tool(disruption, "detect_service_port_split", c)
        assert out == "No service-port items found on the selected hosts."
        assert not any(m == "trend.get" for m, _ in c.calls)

    def test_missing_trend_data_is_not_a_split(self, monkeypatch):
        """A host whose service item has no recent trend rows is n/a, not a split."""
        monkeypatch.setattr(disruption, "KEY_SERVICE_BPS", SERVICE_KEY)
        recent = {k: v for k, v in SPLIT_RECENT.items() if k != "s1"}
        c = self._client(recent=recent)
        out = run_tool(disruption, "detect_service_port_split", c)
        assert out.startswith("No service-vs-mgmt split detected (4 hosts, window 7d, recent 1d)")

    def test_max_results_truncates(self, monkeypatch):
        monkeypatch.setattr(disruption, "KEY_SERVICE_BPS", SERVICE_KEY)
        base = dict(SPLIT_BASE, s2=[100 * MBPS], m2=[200 * MBPS])
        recent = dict(SPLIT_RECENT, s2=[30 * MBPS], m2=[200 * MBPS])
        c = self._client(base=base, recent=recent)
        out = run_tool(disruption, "detect_service_port_split", c, max_results=1)
        assert "**2 hosts with service/mgmt traffic split**" in out
        assert "srv-aq9001" in out and "srv-aq9002" not in out   # 90% outranks 70%
        assert "*1 more omitted*" in out

    def test_api_error_is_reported(self, monkeypatch):
        monkeypatch.setattr(disruption, "KEY_SERVICE_BPS", SERVICE_KEY)
        c = RecordingClient({"host.get": _raise(httpx.ConnectError("refused"))})
        assert run_tool(disruption, "detect_service_port_split", c) == "Error: refused"


# --- detect_regional_traffic_loss ------------------------------------------

REGIONS = '{"AQ": "region.in[aq]", "BV": "region.in[bv]", "HM": "region.in[hm]"}'
REGION_ITEMS = [
    {"itemid": "r1", "key_": "region.in[aq]"},
    {"itemid": "r2", "key_": "region.in[bv]"},
    {"itemid": "r3", "key_": "region.in[bv]"},   # two items under one key are summed
    {"itemid": "r4", "key_": "region.in[hm]"},
]


class TestRegionalLossWire:
    def test_not_configured(self, monkeypatch):
        monkeypatch.delenv("ZABBIX_REGIONAL_TRAFFIC_KEYS", raising=False)
        c = RecordingClient()
        out = run_tool(disruption, "detect_regional_traffic_loss", c)
        assert out.startswith("Not configured. Set ZABBIX_REGIONAL_TRAFFIC_KEYS")
        assert c.calls == []

    def test_collapsed_region_against_flat_peer(self, monkeypatch):
        monkeypatch.setenv("ZABBIX_REGIONAL_TRAFFIC_KEYS", REGIONS)
        base = {"r1": [100 * MBPS], "r2": [40 * MBPS], "r3": [60 * MBPS], "r4": [10 * MBPS]}
        recent = {"r1": [20 * MBPS], "r2": [40 * MBPS], "r3": [58 * MBPS]}   # HM: no recent rows
        c = RecordingClient({"item.get": REGION_ITEMS, "trend.get": _trends(base, recent)})
        out = run_tool(disruption, "detect_regional_traffic_loss", c)
        assert out.startswith("**1 regions with traffic loss** (window 7d, recent 1d)")
        assert "| AQ | collapsed | 80% | 100.0 Mbps | 20.0 Mbps |" in out
        assert "| BV |" not in out and "| HM |" not in out
        assert c.sent("item.get")["filter"] == {
            "key_": ["region.in[aq]", "region.in[bv]", "region.in[hm]"], "status": "0"}

    def test_solo_drop_without_flat_peer(self, monkeypatch):
        monkeypatch.setenv("ZABBIX_REGIONAL_TRAFFIC_KEYS", REGIONS)
        base = {"r1": [100 * MBPS], "r2": [100 * MBPS], "r4": [100 * MBPS]}
        recent = {"r1": [50 * MBPS], "r2": [80 * MBPS], "r4": [40 * MBPS]}
        c = RecordingClient({"item.get": REGION_ITEMS, "trend.get": _trends(base, recent)})
        out = run_tool(disruption, "detect_regional_traffic_loss", c)
        assert "**2 regions with traffic loss**" in out
        # sorted by drop: HM 60% before AQ 50%
        assert out.index("| HM | solo-drop | 60%") < out.index("| AQ | solo-drop | 50%")

    def test_no_loss_and_no_items(self, monkeypatch):
        monkeypatch.setenv("ZABBIX_REGIONAL_TRAFFIC_KEYS", REGIONS)
        flat = {"r1": [10 * MBPS], "r2": [10 * MBPS], "r4": [10 * MBPS]}
        c = RecordingClient({"item.get": REGION_ITEMS, "trend.get": _trends(flat, flat)})
        out = run_tool(disruption, "detect_regional_traffic_loss", c)
        assert out == "No regional traffic loss (drop ≥ 30%) across 3 configured regions."
        c = RecordingClient({"item.get": []})
        out = run_tool(disruption, "detect_regional_traffic_loss", c)
        assert out == "No items match the configured region keys."

    def test_unparsable_trend_values_are_skipped(self, monkeypatch):
        monkeypatch.setenv("ZABBIX_REGIONAL_TRAFFIC_KEYS", REGIONS)
        base = {"r1": ["junk", 100 * MBPS], "r2": [10 * MBPS], "r4": [10 * MBPS]}
        recent = {"r1": [10 * MBPS], "r2": [10 * MBPS], "r4": [10 * MBPS]}
        c = RecordingClient({"item.get": REGION_ITEMS, "trend.get": _trends(base, recent)})
        out = run_tool(disruption, "detect_regional_traffic_loss", c)
        assert "| AQ | collapsed | 90% | 100.0 Mbps | 10.0 Mbps |" in out

    def test_api_error(self, monkeypatch):
        monkeypatch.setenv("ZABBIX_REGIONAL_TRAFFIC_KEYS", REGIONS)
        c = RecordingClient({"item.get": _raise(ValueError("bad response"))})
        assert run_tool(disruption, "detect_regional_traffic_loss", c) == "Error: bad response"


# --- detect_disruption_wave -------------------------------------------------

# Five AQ hosts across separate subnets collapse together; five AQ peers in the same
# cohort stay flat (so the peer-relative gate passes); one sub-host folds into
# its parent; one BV host sits below the baseline floor.
WAVE_IPS = ["192.0.2.11", "198.51.100.12", "203.0.113.13", "10.0.1.14", "10.0.2.15"]
WAVE_HOSTS = (
    [_host(str(9001 + i), f"srv-aq{9001 + i}", ip) for i, ip in enumerate(WAVE_IPS)]
    + [_host(str(9101 + i), f"srv-aq{9101 + i}", f"10.9.0.{i + 1}") for i in range(5)]
    + [_host("9201", "srv-aq9001 aq9201", "192.0.2.99"),
       _host("9301", "srv-bv9301", "10.3.0.1")]
)
WAVE_ITEMS = [{"itemid": f"i{h['hostid']}", "hostid": h["hostid"], "key_": "net.if.in[eth0]"}
              for h in WAVE_HOSTS]


def _wave_trends(dropped_recent=5 * MBPS):
    base, recent = {}, {}
    for h in WAVE_HOSTS:
        iid = f"i{h['hostid']}"
        hid = int(h["hostid"])
        if hid == 9301:
            base[iid], recent[iid] = [1 * MBPS], [0]        # under the 5 Mbps floor
        elif hid == 9201:
            base[iid], recent[iid] = [50 * MBPS], [1 * MBPS]  # folds into 9001
        elif hid < 9100:
            base[iid], recent[iid] = [100 * MBPS], [dropped_recent]
        else:
            base[iid], recent[iid] = [100 * MBPS], [100 * MBPS]
    return _trends(base, recent)


class TestDisruptionWaveWire:
    def test_wave_detected(self):
        c = RecordingClient({"host.get": WAVE_HOSTS, "item.get": WAVE_ITEMS,
                             "trend.get": _wave_trends()})
        out = run_tool(disruption, "detect_disruption_wave", c)
        assert out.startswith("**1 disruption waves** (≥5 hosts × ≥")
        assert out.splitlines()[0].endswith("drop ≥ 50%)")
        assert "### Wave 1 — critical" in out
        assert "- **Hosts:** 5 (srv-aq9001, srv-aq9002, srv-aq9003, srv-aq9004, srv-aq9005)" in out
        assert "- **Subnets:** 5 (" in out
        assert "- **Centered on:** AQ (100%)" in out
        assert "aq9201" not in out and "srv-bv9301" not in out
        hosts_p = c.sent("host.get")
        assert hosts_p["selectGroups"] == ["name"] and hosts_p["filter"] == {"status": "0"}
        item_p = c.sent("item.get")
        assert item_p["output"] == ["itemid", "hostid", "key_"]
        assert len(item_p["hostids"]) == len(WAVE_HOSTS)
        base_p, rec_p = [p for m, p in c.calls if m == "trend.get"]
        assert rec_p["time_till"] - rec_p["time_from"] == 2 * 3600
        assert base_p["time_till"] - base_p["time_from"] == 10 * 3600

    def test_more_than_five_hosts_and_high_severity(self):
        """A sixth collapsed host in the wave is summarised as ``+1``."""
        extra = _host("9006", "srv-aq9006", "10.0.3.16")
        hosts = WAVE_HOSTS + [extra]
        items = WAVE_ITEMS + [{"itemid": "i9006", "hostid": "9006", "key_": "net.if.in[eth0]"}]
        base = {f"i{h['hostid']}": [100 * MBPS] for h in hosts}
        recent = {k: [100 * MBPS] for k in base}
        for h in hosts:
            if int(h["hostid"]) < 9100 or h["hostid"] == "9201":
                recent[f"i{h['hostid']}"] = [35 * MBPS]   # 65% drop → high
        base["i9301"] = [1 * MBPS]
        c = RecordingClient({"host.get": hosts, "item.get": items,
                             "trend.get": _trends(base, recent)})
        out = run_tool(disruption, "detect_disruption_wave", c)
        assert "### Wave 1 — high" in out
        assert "- **Hosts:** 6 (" in out and " +1)" in out

    def test_no_wave_when_too_few_hosts(self):
        c = RecordingClient({"host.get": WAVE_HOSTS, "item.get": WAVE_ITEMS,
                             "trend.get": _wave_trends()})
        out = run_tool(disruption, "detect_disruption_wave", c, min_hosts=6)
        assert out.startswith("No disruption waves (5 hosts dropped, but no cluster met ≥6 hosts × ")

    def test_country_filter_empty_and_no_items(self):
        c = RecordingClient({"host.get": WAVE_HOSTS})
        assert run_tool(disruption, "detect_disruption_wave", c, country="tf") == "No matching hosts."
        c = RecordingClient({"host.get": WAVE_HOSTS, "item.get": []})
        out = run_tool(disruption, "detect_disruption_wave", c, country="bv")
        assert out == "No traffic items found on the selected hosts."
        assert c.sent("item.get")["hostids"] == ["9301"]

    def test_no_trend_rows_means_no_drops(self):
        c = RecordingClient({"host.get": WAVE_HOSTS, "item.get": WAVE_ITEMS, "trend.get": []})
        out = run_tool(disruption, "detect_disruption_wave", c)
        assert out.startswith("No disruption waves (0 hosts dropped")

    def test_api_error(self):
        c = RecordingClient({"host.get": _raise(httpx.ReadTimeout("timed out"))})
        assert run_tool(disruption, "detect_disruption_wave", c) == "Error: timed out"


class TestServicePortSplitNics:
    """ADR 158: management NICs are found by prefix and summed bond-aware."""

    def _run(self, monkeypatch, items, base, recent):
        monkeypatch.setattr(disruption, "KEY_SERVICE_BPS", SERVICE_KEY)
        return run_tool(disruption, "detect_service_port_split",
                        TestServicePortSplitWire()._client(items=items, base=base, recent=recent),
                        window_days=7, recent_days=1)

    def test_a_bond_and_its_members_are_not_counted_twice(self, monkeypatch):
        items = [
            {"itemid": "s1", "hostid": "1", "key_": SERVICE_KEY},
            {"itemid": "b1", "hostid": "1", "key_": "net.if.in[bond0]"},
            {"itemid": "e1", "hostid": "1", "key_": "net.if.in[eth0]"},
            {"itemid": "e2", "hostid": "1", "key_": "net.if.in[eth1]"},
        ]
        base = {"s1": [100 * MBPS], "b1": [200 * MBPS], "e1": [100 * MBPS], "e2": [100 * MBPS]}
        recent = {"s1": [10 * MBPS], "b1": [190 * MBPS], "e1": [60 * MBPS], "e2": [60 * MBPS]}
        out = self._run(monkeypatch, items, base, recent)
        assert "**1 hosts with service/mgmt traffic split**" in out
        assert "| 90% | 5% |" in out          # the bond alone: 200 → 190, not 400 → 310

    def test_quoted_stock_template_keys_are_seen(self, monkeypatch):
        items = [
            {"itemid": "s1", "hostid": "1", "key_": SERVICE_KEY},
            {"itemid": "q1", "hostid": "1", "key_": 'net.if.in["enp3s0"]'},
        ]
        base = {"s1": [100 * MBPS], "q1": [200 * MBPS]}
        recent = {"s1": [10 * MBPS], "q1": [196 * MBPS]}
        out = self._run(monkeypatch, items, base, recent)
        assert "**1 hosts with service/mgmt traffic split**" in out
