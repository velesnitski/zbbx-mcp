"""``generate_html_report`` wire test: current readings plus a trend window
rendered as one HTML page. Fixtures are synthetic."""

from __future__ import annotations

import os
import time

import pytest

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp import classify
from zbbx_mcp.tools import html_report

NOW = int(time.time())
LIVE = str(NOW)
STALE = str(NOW - 7200)

KEY_CPU = "system.cpu.util[,idle]"
KEY_LOAD = "system.cpu.load[percpu,avg5]"
KEY_MEM = "vm.memory.size[available]"
GB = 1_073_741_824
FRONTEND = "https://zabbix.example.com"


@pytest.fixture(autouse=True)
def _synthetic_env(monkeypatch, tmp_path):
    monkeypatch.setenv("ZABBIX_PRODUCT_MAP", "")
    monkeypatch.setattr(classify, "_PRODUCT_MAP", None)
    monkeypatch.setenv("ZABBIX_PROVIDER_CIDRS",
                       '{"Provider A": ["192.0.2.0/24"], "Provider B": ["198.51.100.0/24"]}')
    monkeypatch.setattr(classify, "_EXTRA_PROVIDER_NETS", None)
    monkeypatch.setenv("ZBBX_FILE_ROOTS", str(tmp_path))


def _host(hid, name, group, ip=None):
    return {"hostid": hid, "host": name, "name": name.upper(), "status": "0",
            "groups": [{"name": group}], "interfaces": [{"ip": ip}] if ip else []}


HOSTS = [
    _host("1", "srv-aq9001", "app_free", "192.0.2.1"),     # hot, saturated, on dashboard, trending up
    _host("2", "srv-aq9002", "app_free", "192.0.2.2"),     # quiet, off dashboard, no trend history
    _host("3", "srv-bv9001", "app_paid", "198.51.100.1"),  # agent silent, on dashboard
    _host("4", "srv-hm9001", "app_paid"),                  # no IP
]

DASHBOARDS = [{
    "dashboardid": "10", "name": "Fleet Overview",
    "pages": [
        {"name": "Base", "widgets": [{"fields": [{"type": "6", "value": "g1"}]}]},
        {"name": "Bouvet", "widgets": [{"fields": [{"type": "6", "value": "g2"}]}]},
    ],
}]
GRAPHS = [
    {"graphid": "g1", "hosts": [{"hostid": "1"}]},
    {"graphid": "g2", "hosts": [{"hostid": "3"}]},
]


def _item(hid, value, clock=LIVE, key=None, itemid=None):
    it = {"hostid": hid, "lastvalue": str(value), "lastclock": clock, "value_type": "0"}
    if key:
        it["key_"] = key
        it["itemid"] = itemid or f"{hid}-{key}"
    return it


CPU = [_item("1", 10, key=KEY_CPU, itemid="i1c"), _item("2", 95, key=KEY_CPU, itemid="i2c"),
       _item("3", 40, STALE, key=KEY_CPU, itemid="i3c"), _item("4", 50, key=KEY_CPU, itemid="i4c")]
METRICS = {
    KEY_LOAD: [_item("1", 3.0), _item("2", 0.1)],
    KEY_MEM: [_item("1", 2 * GB), _item("4", 4 * GB)],
}
TRAFFIC_IN = [
    _item("1", 700_000_000, key="net.if.in[eth0]", itemid="i1t"),
    _item("2", 300_000_000, key="net.if.in[eth0]", itemid="i2t"),
    _item("4", 550_000_000, key="net.if.in[eth0]", itemid="i4t"),
]
COST = [{"hostid": "1", "macro": "{$COST_MONTH}", "value": "100"}]


def _points(itemid, avgs):
    n = len(avgs)
    return [{"itemid": itemid, "clock": str(NOW - (n - k) * 3600), "num": "60",
             "value_avg": str(v), "value_min": str(v), "value_max": str(v)} for k, v in enumerate(avgs)]


TRENDS = (
    _points("i1c", [20] * 8)                                                # idle 20 -> 80% used
    + _points("i1t", [100e6, 100e6, 200e6, 300e6, 400e6, 500e6, 700e6, 700e6])   # rising
)


def _client(hosts=HOSTS, dashboards=DASHBOARDS, trends=TRENDS):
    def host_get(p):
        if "selectParentTemplates" in p:
            return []
        if p.get("hostids"):
            return [{"hostid": h["hostid"], "host": h["host"]} for h in hosts if h["hostid"] in p["hostids"]]
        return hosts

    def item_get(p):
        hostids = set(p.get("hostids", []))
        if "itemid" in p.get("output", []):                     # trend item discovery
            return [i for i in CPU + TRAFFIC_IN if i["hostid"] in hostids]
        search = p.get("search") or {}
        if search:
            return []
        key = (p.get("filter") or {}).get("key_")
        if key == KEY_CPU:
            return [i for i in CPU if i["hostid"] in hostids]
        if isinstance(key, list):
            pool = TRAFFIC_IN if key[0].startswith("net.if.in[") else []
            return [t for t in pool if t["hostid"] in hostids and t["key_"] in key]
        return [i for i in METRICS.get(key, []) if i["hostid"] in hostids]

    def trend_get(p):
        return [t for t in trends if t["itemid"] in p["itemids"]]

    c = RecordingClient({
        "host.get": host_get, "dashboard.get": dashboards, "graph.get": GRAPHS,
        "item.get": item_get, "usermacro.get": COST, "trend.get": trend_get,
    })
    c.frontend_url = FRONTEND
    return c


def _run(c, **kw):
    return run_tool(html_report, "generate_html_report", c, **kw)


def _html(out):
    path = next(ln for ln in out.splitlines() if ln.startswith("**File:**")).split("`")[1]
    with open(path) as f:
        return path, f.read()


class TestWire:
    def test_trend_calls(self, tmp_path):
        c = _client()
        _run(c, output_dir=str(tmp_path), period="7d")
        trend = c.sent("trend.get")
        assert set(trend["itemids"]) == {"i1c", "i2c", "i3c", "i4c", "i1t", "i2t", "i4t"}
        assert abs(trend["time_from"] - (NOW - 7 * 86400)) < 120
        assert trend["limit"] == 7 * 24 * 30
        discovery = next(p for m, p in c.calls if m == "item.get" and "itemid" in p.get("output", []))
        assert KEY_CPU in discovery["filter"]["key_"] and "net.if.in[eth0]" in discovery["filter"]["key_"]
        assert discovery["filter"]["status"] == "0"


class TestHappyPath:
    def test_return_text_and_file(self, tmp_path):
        out = _run(_client(), output_dir=str(tmp_path / "html"))
        assert "**HTML Report Generated**" in out
        assert "**Servers:** 4" in out
        path, html = _html(out)
        assert os.path.dirname(path) == os.path.realpath(tmp_path / "html")
        assert os.path.basename(path).startswith("zabbix_report_20") and path.endswith(".html")
        assert html.startswith("<!DOCTYPE html>") and html.rstrip().endswith("</body></html>")
        assert "<title>Infrastructure Report</title>" in html
        assert "Period: 7d" in html

    def test_kpi_cards(self, tmp_path):
        _, html = _html(_run(_client(), output_dir=str(tmp_path)))
        assert '<div class="card-header">Servers</div><div class="card-value">4</div>' in html
        assert "3 countries, 2 providers" in html
        assert '<div class="card-value" style="color: var(--red)">50.0%</div>' in html   # median of 90/5/50
        assert "1 servers &gt; 80%" in html
        assert ">550 Mbps</div>" in html and "1 near saturation" in html
        assert '<div class="card-header">service Health</div>' in html

    def test_executive_summary_badges(self, tmp_path):
        _, html = _html(_run(_client(), output_dir=str(tmp_path)))
        assert '<span class="badge badge-orange">1 servers &gt;80% CPU</span>' in html
        assert '<span class="badge badge-orange">1 near BW limit</span>' in html
        assert '<span class="badge badge-blue">2 single-server countries</span>' in html
        assert "All clear" not in html

    def test_traffic_by_country(self, tmp_path):
        _, html = _html(_run(_client(), output_dir=str(tmp_path)))
        assert '<td style="width:40px;font-weight:600">AQ</td>' in html
        assert ">2 srv</td>" in html and ">1.00 Gbps</td>" in html
        assert ">0.55 Gbps</td>" in html and ">0.00 Gbps</td>" in html
        assert html.count(">no redundancy</span>") == 2        # BV and HM
        assert "high CPU" not in html                            # AQ averages 47.5

    def test_server_rows(self, tmp_path):
        _, html = _html(_run(_client(), output_dir=str(tmp_path)))
        assert f'<a href="{FRONTEND}/zabbix.php?action=dashboard.view&dashboardid=10&page=0"' in html
        assert "<strong>srv-aq9001</strong>" in html
        assert '<td class="cpu-critical">90.0%</td><td>80.0%</td>' in html           # now / 7d avg
        assert "<td>700 Mbps</td><td>375 Mbps</td>" in html                          # now / 7d avg
        assert '<td class="trend-rise">rising</td>' in html
        # off-dashboard host links to latest data, has no trend history
        assert f'<a href="{FRONTEND}/zabbix.php?action=latest.view&hostids%5B%5D=2"' in html
        assert '<td class="cpu-ok">5.0%</td><td>N/A</td>' in html
        assert '<td class="trend-stable">stable</td>' not in html
        # silent agent renders as unknown, never as a stale number
        assert '<td class="">N/A</td><td>N/A</td>' in html
        assert "60.0%" not in html
        assert '<code style="font-size:.8rem;color:var(--muted)">192.0.2.1</code>' in html

    def test_grouping_and_provider_cards(self, tmp_path):
        _, html = _html(_run(_client(), output_dir=str(tmp_path)))
        assert "<h2>app_free/Default</h2>" in html and "<h2>app_paid/Default</h2>" in html
        assert f'<a href="{FRONTEND}/zabbix.php?action=dashboard.view&dashboardid=10" target="_blank"' in html
        assert "Base (1 servers, 700 Mbps total)" in html
        assert "Bouvet (1 servers, 0 Mbps total)" in html
        assert "<h2>Off-Dashboard (2 servers)</h2>" in html
        assert '<div class="card-header">Provider A</div><div class="card-value">2</div>' in html
        assert '<div class="card-header">Other</div><div class="card-value">1</div>' in html
        assert "| 4 servers |" in html


class TestFilters:
    def test_country_filter_names_the_file_and_title(self, tmp_path):
        out = _run(_client(), country="aq", output_dir=str(tmp_path))
        assert "**Servers:** 2" in out
        path, html = _html(out)
        assert os.path.basename(path).startswith("zabbix_report_aq_")
        assert "<title>Infrastructure Report — AQ</title>" in html
        assert "srv-bv9001" not in html

    def test_product_and_tier_filters_are_exact(self, tmp_path):
        out = _run(_client(), product="app_paid", tier="Default", output_dir=str(tmp_path))
        assert "**Servers:** 2" in out
        path, html = _html(out)
        assert os.path.basename(path).startswith("zabbix_report_app_paid_Default_")
        assert "Infrastructure Report — app_paid" in html
        assert _run(_client(), tier="Premium", output_dir=str(tmp_path)) == "No servers match the filters."

    def test_include_and_exclude_lists(self, tmp_path):
        assert "**Servers:** 2" in _run(_client(), products="app_free", output_dir=str(tmp_path))
        assert "**Servers:** 2" in _run(_client(), exclude_product="app_free", output_dir=str(tmp_path))


class TestDegraded:
    def test_empty_fleet(self, tmp_path):
        c = _client(hosts=[], dashboards=[])
        assert _run(c, output_dir=str(tmp_path)) == "No servers match the filters."
        assert "trend.get" not in {m for m, _ in c.calls}

    def test_no_trend_history_at_all(self, tmp_path):
        _, html = _html(_run(_client(trends=[]), output_dir=str(tmp_path)))
        assert "rising" not in html
        assert '<td class="cpu-critical">90.0%</td><td>N/A</td>' in html

    def test_output_dir_outside_the_roots_is_refused(self):
        out = _run(_client(), output_dir="/nonexistent-root-for-test/x")
        assert out.startswith("Error:") and "not in the allowed roots" in out
