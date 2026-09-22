"""``generate_ceo_report`` wire test: the whole-fleet page from enabled hosts,
a traffic trend window, current traffic/CPU and cost macros. Synthetic."""

from __future__ import annotations

import json
import os
import time

import pytest

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp import classify
from zbbx_mcp.tools import ceo_report

NOW = int(time.time())
LIVE = str(NOW)
STALE = str(NOW - 7200)
KEY_CPU = "system.cpu.util[,idle]"


@pytest.fixture(autouse=True)
def _synthetic_env(monkeypatch, tmp_path):
    monkeypatch.setenv("ZABBIX_PRODUCT_MAP", "")
    monkeypatch.setattr(classify, "_PRODUCT_MAP", None)
    monkeypatch.setenv("ZABBIX_PROVIDER_CIDRS",
                       '{"Provider A": ["192.0.2.0/24"], "Provider B": ["198.51.100.0/24"]}')
    monkeypatch.setattr(classify, "_EXTRA_PROVIDER_NETS", None)
    monkeypatch.setenv("ZBBX_FILE_ROOTS", str(tmp_path))
    snap = tmp_path / "snapshots"
    snap.mkdir()
    monkeypatch.setattr(ceo_report, "_snapshot_dir", lambda: snap)   # never the user's home


def _host(hid, name, group, ip=None):
    return {"hostid": hid, "host": name, "groups": [{"name": group}], "interfaces": [{"ip": ip}] if ip else []}


HOSTS = [
    _host("1", "srv-aq9001", "app_free", "192.0.2.1"),       # traffic halved over the window
    _host("2", "srv-aq9002", "app_free", "192.0.2.2"),       # idle
    _host("3", "srv-bv9001", "app_paid", "198.51.100.1"),    # dead: paid for, carries nothing
    _host("4", "srv-hm9001", "app_paid"),                    # hot, no IP, no history
    _host("6", "srv-aq9001 9002", "app_free", "192.0.2.3"),  # cluster member of host 1
]
TEST_HOST = _host("5", "srv-aq9099-test", "app_free", "192.0.2.9")


def _traffic(hid, bps, clock=LIVE):
    return {"itemid": f"i{hid}", "hostid": hid, "key_": "net.if.in[eth0]",
            "lastvalue": str(bps), "lastclock": clock, "value_type": "3"}


def _traffic_items(h1_clock=LIVE):
    return [
        _traffic("1", 2_000_000_000, h1_clock),
        _traffic("2", 3_000_000),
        _traffic("3", 0),
        _traffic("4", 4_000_000_000),
    ]


CPU = [
    {"hostid": "1", "lastvalue": "40", "lastclock": LIVE},
    {"hostid": "2", "lastvalue": "95", "lastclock": LIVE},
    {"hostid": "3", "lastvalue": "99", "lastclock": LIVE},
    {"hostid": "4", "lastvalue": "10", "lastclock": LIVE},
]
COST = [
    {"hostid": "1", "macro": "{$COST_MONTH}", "value": "100"},
    {"hostid": "2", "macro": "{$COST_MONTH}", "value": "60"},
    {"hostid": "3", "macro": "{$COST_MONTH}", "value": "80"},
    {"hostid": "4", "macro": "{$COST_MONTH}", "value": "0"},      # zero is "unpriced"
]


def _points(itemid, avgs):
    n = len(avgs)
    return [{"itemid": itemid, "clock": str(NOW - (n - k) * 3600),
             "value_avg": str(v), "value_min": str(v), "value_max": str(v)} for k, v in enumerate(avgs)]


TRENDS = (
    _points("i1", [5e9, 5e9, 4e9, 4e9, 3e9, 3e9, 2e9, 2e9])    # avg 3.5 Gbps, now 2.0
    + _points("i3", [1e9] * 8)                                  # avg 1.0 Gbps, now 0
)


def _client(hosts=None, traffic=None, trends=TRENDS, cost=COST):
    hosts = [*HOSTS, TEST_HOST] if hosts is None else hosts
    traffic = _traffic_items() if traffic is None else traffic

    def host_get(p):
        if p.get("hostids"):
            return [{"hostid": h["hostid"], "host": h["host"]} for h in hosts if h["hostid"] in p["hostids"]]
        return hosts

    def item_get(p):
        hostids = set(p.get("hostids", []))
        if "tags" in p or "itemid" in p.get("output", []):
            return [t for t in traffic if t["hostid"] in hostids]
        if (p.get("filter") or {}).get("key_") == KEY_CPU:
            return [c for c in CPU if c["hostid"] in hostids]
        return []

    def trend_get(p):
        return [t for t in trends if t["itemid"] in p["itemids"]]

    return RecordingClient({
        "host.get": host_get, "item.get": item_get, "trend.get": trend_get, "usermacro.get": cost,
    })


def _run(c, **kw):
    return run_tool(ceo_report, "generate_ceo_report", c, **kw)


def _html(out):
    path = next(ln for ln in out.splitlines() if ln.startswith("**File:**")).split("`")[1]
    with open(path) as f:
        return path, f.read()


class TestWire:
    def test_calls(self, tmp_path):
        c = _client()
        _run(c, output_dir=str(tmp_path))
        hosts = c.sent("host.get")
        assert hosts["filter"] == {"status": "0"} and hosts["sortfield"] == "host"
        assert hosts["selectGroups"] == ["name"] and hosts["selectInterfaces"] == ["ip"]
        traffic = next(p for m, p in c.calls if m == "item.get" and "tags" in p)
        assert traffic["search"] == {"key_": "*net.if.in[*"} and traffic["searchWildcardsEnabled"] is True
        assert "lastclock" in traffic["output"]
        cpu = next(p for m, p in c.calls if m == "item.get" and (p.get("filter") or {}).get("key_") == KEY_CPU)
        assert "lastclock" in cpu["output"]
        assert c.sent("usermacro.get")["filter"] == {"macro": ["{$COST_MONTH}"]}
        trend = c.sent("trend.get")
        assert set(trend["itemids"]) == {"i1", "i2", "i3", "i4"}
        assert abs(trend["time_from"] - (NOW - 30 * 86400)) < 120

    def test_test_hosts_are_excluded_before_anything_is_measured(self, tmp_path):
        c = _client()
        out = _run(c, output_dir=str(tmp_path))
        assert "5" not in c.sent("usermacro.get")["hostids"]
        assert "srv-aq9099-test" not in _html(out)[1]


class TestHappyPath:
    def test_return_text_and_file(self, tmp_path):
        out = _run(_client(), output_dir=str(tmp_path / "ceo"))
        assert "**CEO Report Generated**" in out
        assert "**Fleet:** 5 servers, 3 countries, 6.0 Gbps" in out
        assert "**Alerts:** 2 | **service issues:** 0 countries" in out
        assert "vs snapshot" not in out
        path, html = _html(out)
        assert os.path.dirname(path) == os.path.realpath(tmp_path / "ceo")
        assert os.path.basename(path).startswith("infra-report-20") and path.endswith(".html")
        assert html.startswith("<!DOCTYPE html>") and html.rstrip().endswith("</div></body></html>")

    def test_header_kpis(self, tmp_path):
        _, html = _html(_run(_client(), output_dir=str(tmp_path)))
        assert '<div class="kpi-value">5</div><div class="kpi-label">Servers</div>' in html
        assert '<div class="kpi-value">6.0 Gbps</div><div class="kpi-label">Total Traffic</div>' in html
        assert '<div class="kpi-value">3</div><div class="kpi-label">Countries</div>' in html
        assert '<div class="kpi-value">2</div><div class="kpi-label">Products</div>' in html
        assert '<div class="kpi-value">2</div><div class="kpi-label">Providers</div>' in html
        assert '<div class="kpi-value">39.0%</div><div class="kpi-label">Avg CPU</div>' in html   # (60+5+1+90)/4
        assert "&uarr;" not in html and "&darr;" not in html      # nothing to compare against yet

    def test_executive_summary_alerts(self, tmp_path):
        _, html = _html(_run(_client(), output_dir=str(tmp_path)))
        assert '<div class="alert alert-red"><b>BV: Complete blackout.</b> 1 servers, 0 Gbps traffic.' in html
        assert ('<div class="alert alert-yellow"><b>AQ: Traffic declining -43%.</b> '
                'Now 2.0 Gbps (avg 3.5).') in html
        assert "All systems healthy" not in html

    def test_traffic_by_country_table(self, tmp_path):
        _, html = _html(_run(_client(), output_dir=str(tmp_path)))
        assert "<h2>Traffic by Country (30d)</h2>" in html
        assert "3 countries. Total fleet throughput: 6.0 Gbps." in html
        # HM: no trend history, so avg 0 and no change; AQ: dropping; BV: dead
        assert ('<tr><td><b>HM</b></td><td class="num">1</td><td class="num">0.0 Gbps</td>'
                '<td class="num">4.0 Gbps</td><td><span class="badge badge-stable">Stable</span></td>'
                '<td class="num" style="color:#6b7280">+0%</td>') in html
        assert ('<tr><td><b>AQ</b></td><td class="num">3</td><td class="num">3.5 Gbps</td>'
                '<td class="num">2.0 Gbps</td><td><span class="badge badge-dropping">Dropping</span></td>'
                '<td class="num" style="color:#dc2626">-43%</td>') in html
        assert '<td><b>BV</b></td><td class="num">1</td><td class="num">1.0 Gbps</td><td class="num">0.0 Gbps</td>' in html
        assert '<span class="badge badge-dead">Dead</span>' in html

    def test_capacity_and_risk(self, tmp_path):
        _, html = _html(_run(_client(), output_dir=str(tmp_path)))
        assert ('<tr><td><b>HM</b></td><td class="num">1</td><td class="num">4.0 Gbps</td>'
                '<td class="num">4000.0</td><td><span class="badge badge-critical">OVERLOADED</span></td>') in html
        assert '<td class="num">666.7</td><td><span class="badge badge-ok">OK</span>' in html      # AQ: 2.0 Gbps / 3
        # HM: single host (+25) and CPU >80% (+30); BV: single provider (+30) and single host (+25)
        assert '<span class="badge badge-high">55/100</span></td><td class="num">1</td><td>CPU >80%</td>' in html
        assert '<span class="badge badge-high">55/100</span></td><td class="num">1</td><td>no redundancy</td>' in html
        assert "<h2>Fleet Composition</h2>" in html and "5 servers across 2 products" in html
        assert '<div class="card-title">app_free</div><div class="card-value">3</div>' in html

    def test_cost_analysis(self, tmp_path):
        _, html = _html(_run(_client(), output_dir=str(tmp_path)))
        assert "3 of 5 servers have cost data (60.0% coverage)" in html
        assert '<div class="card-title">Monthly Cost</div><div class="card-value">$240</div><div class="card-sub">$2,880/year</div>' in html
        assert '<div class="card-title">Cost Gap</div><div class="card-value">2</div>' in html
        assert ('<tr><td>AQ</td><td class="num">2</td><td class="num">$160</td><td class="num">2,003</td>'
                '<td class="num">$80</td><td><span class="badge badge-ok">GOOD</span></td></tr>') in html
        assert '<td class="num">$80</td><td class="num">0</td><td class="num">$0</td><td><span class="badge badge-warning">NO TRAFFIC</span>' in html
        assert "Waste Detection &mdash; 1 servers, $80/mo" in html
        assert '<tr><td>srv-bv9001</td><td>app_paid</td><td class="num">$80</td><td class="num">0.00</td></tr>' in html
        assert "Cost Gaps by Product &mdash; 2 servers" in html

    def test_shutdown_candidates(self, tmp_path):
        _, html = _html(_run(_client(), output_dir=str(tmp_path)))
        assert "Waste Reduction &mdash; Shutdown Candidates" in html
        assert "2 servers for decommission or investigation" in html
        assert '<span class="badge badge-critical">Dead</span></td><td>srv-bv9001</td><td>app_paid</td><td>Provider B</td><td class="num">1.0%</td><td class="num">0.0 Mbps</td>' in html
        assert '<span class="badge badge-stable">Idle</span></td><td>srv-aq9002</td><td>app_free</td><td>Provider A</td><td class="num">5.0%</td><td class="num">3.0 Mbps</td>' in html
        assert "srv-aq9001 9002" not in html.split("Shutdown Candidates")[1].split("</table>")[0]   # unmeasured

    def test_deep_dives_and_clusters(self, tmp_path):
        _, html = _html(_run(_client(), output_dir=str(tmp_path)))
        assert "<h2>Country Deep Dives</h2>" in html
        dives = html.split("<h2>Country Deep Dives</h2>")[1].split("<h2>Provider Distribution</h2>")[0]
        assert dives.index("<h3>BV ") < dives.index("<h3>AQ ")             # dead before dropping
        assert '<span class="badge badge-critical">dead</span>' in dives
        assert '<span class="badge badge-high">traffic drop</span>' in dives
        assert "<b>Recommendation:</b> All 1 servers offline. Investigate or decommission." in dives
        assert "<b>Recommendation:</b> Traffic declined -43%. Check for regional anomalies" in dives
        assert '<tr><td><b>srv-aq9001</b></td><td class="num">2</td><td>app_free</td><td class="num">2000.0 Mbps</td><td>Provider A</td></tr>' in dives
        assert '<div class="card-title">Providers</div><div class="card-value">1</div><div class="card-sub">Provider A (3)</div>' in dives
        assert "HM " not in dives

    def test_requested_deep_dive(self, tmp_path):
        _, html = _html(_run(_client(), deep_dive_country="hm", output_dir=str(tmp_path)))
        dives = html.split("<h2>Country Deep Dives</h2>")[1].split("<h2>Provider Distribution</h2>")[0]
        assert '<h3>HM <span class="badge badge-stable">requested</span></h3>' in dives
        assert "<b>Recommendation:</b> Manual review requested. 1 servers, 4.0 Gbps." in dives
        assert '<div class="card-sub">? (1)</div>' in dives                 # no IP, no provider

    def test_provider_distribution_and_redistribution(self, tmp_path):
        _, html = _html(_run(_client(), output_dir=str(tmp_path)))
        assert "4 servers across 2 providers" in html                        # the no-IP host is not counted
        assert 'title="Provider A: 3"' in html and 'title="Provider B: 1"' in html
        assert "<b>Concentration risk:</b> Provider A hosts 3 servers (75%)." in html
        assert "<h2>Traffic Redistribution Analysis</h2>" in html
        assert '<tr><td><b>AQ</b></td><td><span class="badge badge-dropping">Dropping</span></td><td class="num">1.5</td><td>Traffic lost' in html
        assert '<tr><td><b>BV</b></td><td><span class="badge badge-critical">Dead</span></td><td class="num">1.0</td>' in html
        assert "<h3>Immediate (This Week)</h3>" in html
        assert "<b>Shutdown 1 dead servers</b> (0 traffic, 0 CPU)" in html


class TestSnapshots:
    def test_a_snapshot_is_written_for_next_time(self, tmp_path):
        _run(_client(), output_dir=str(tmp_path))
        files = sorted((tmp_path / "snapshots").glob("ceo_report_*.json"))
        assert len(files) == 1
        snap = json.loads(files[0].read_text())
        assert (snap["total_servers"], snap["total_countries"], snap["total_traffic"]) == (5, 3, 6.0)
        assert snap["country_data"]["AQ"]["trend"] == "dropping" and snap["country_data"]["AQ"]["change"] == -43
        assert snap["country_data"]["BV"]["trend"] == "dead"

    def test_deltas_against_the_previous_snapshot(self, tmp_path):
        prev = {"date": "2000-01-01", "total_servers": 4, "total_traffic": 3.0, "total_countries": 3, "avg_cpu": 50.0,
                "country_data": {"AQ": {"traffic_gbps": 1.0}, "BV": {"traffic_gbps": 0.0}, "PN": {"traffic_gbps": 0.5}}}
        (tmp_path / "snapshots" / "ceo_report_2000-01-01.json").write_text(json.dumps(prev))
        out = _run(_client(), output_dir=str(tmp_path))
        assert "| **vs snapshot:** 2000-01-01" in out
        _, html = _html(out)
        assert "&bull; vs snapshot 2000-01-01" in html
        assert html.count("&uarr; 25%") == 1            # servers 4 -> 5
        assert "&uarr; 100%" in html                    # traffic 3.0 -> 6.0
        assert "&darr; 22%" in html                     # avg CPU 50 -> 39.0, lower is better
        assert "<h2>Changes Since Last Report</h2>" in html
        assert "Snapshot 2000-01-01 &rarr; " in html
        assert '<tr><td><b>HM</b></td><td class="num">&mdash;</td><td class="num">4.0 Gbps</td>' in html
        assert '<tr><td><b>PN</b></td><td class="num">0.5 Gbps</td><td class="num">&mdash;</td>' in html
        assert '<tr><td><b>AQ</b></td><td class="num">1.0 Gbps</td><td class="num">2.0 Gbps</td><td class="num" style="color:#16a34a">&uarr; 100%</td>' in html
        assert "<b>BV</b></td><td class=\"num\">0.0 Gbps</td><td class=\"num\">0.0 Gbps</td>" not in html   # 0 -> 0 is not a change


class TestDegraded:
    def test_a_traffic_item_that_stopped_reporting_is_not_a_current_reading(self, tmp_path):
        # Host 1 has a month of history but its agent went quiet two hours ago:
        # its last rate must count for nothing, and the report must still render.
        out = _run(_client(traffic=_traffic_items(h1_clock=STALE)), output_dir=str(tmp_path))
        assert "**CEO Report Generated**" in out
        assert "**Fleet:** 5 servers, 3 countries, 4.0 Gbps" in out      # not 6.0: the stale 2 Gbps is gone
        _, html = _html(out)
        assert "2.0 Gbps" not in html
        # The history stays (avg 3.5) but "now" is only what is still reporting: 3 Mbps.
        assert ('<tr><td><b>AQ</b></td><td class="num">3</td><td class="num">3.5 Gbps</td>'
                '<td class="num">0.0 Gbps</td><td><span class="badge badge-dead">Dead</span></td>') in html
        assert "<b>AQ: Complete blackout.</b> 3 servers, 0 Gbps traffic." in html

    def test_empty_fleet(self, tmp_path):
        c = _client(hosts=[], traffic=[], trends=[], cost=[])
        out = _run(c, output_dir=str(tmp_path))
        assert "**Fleet:** 0 servers, 0 countries, 0.0 Gbps" in out
        assert "**Alerts:** 1" in out
        _, html = _html(out)
        assert "<b>All systems healthy.</b>" in html
        assert "0 servers across 0 providers" in html
        assert "Concentration risk" not in html
        assert "Country Deep Dives" not in html and "Cost Analysis" not in html
        assert "trend.get" not in {m for m, _ in c.calls}

    def test_output_dir_outside_the_roots_is_refused(self, tmp_path):
        out = _run(_client(), output_dir="/nonexistent-root-for-test/x")
        assert out.startswith("Error:") and "not in the allowed roots" in out
        assert not list((tmp_path / "snapshots").iterdir())     # nothing recorded for a report that was not written
