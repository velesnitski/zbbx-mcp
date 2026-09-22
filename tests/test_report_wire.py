"""``generate_server_report`` wire test: dashboards + hosts + three metric
reads become a three-sheet workbook. Fixtures are synthetic."""

from __future__ import annotations

import os
import time

import pytest
from openpyxl import load_workbook

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp import classify
from zbbx_mcp.tools import report

NOW = int(time.time())
LIVE = str(NOW)
STALE = str(NOW - 7200)  # two hours old: a fact about the past, not a reading (ADR 141)

KEY_CPU = "system.cpu.util[,idle]"
KEY_LOAD = "system.cpu.load[percpu,avg5]"
KEY_MEM = "vm.memory.size[available]"
GB = 1_073_741_824


@pytest.fixture(autouse=True)
def _synthetic_env(monkeypatch, tmp_path):
    monkeypatch.setenv("ZABBIX_PRODUCT_MAP", "")          # raw group names are the product
    monkeypatch.setattr(classify, "_PRODUCT_MAP", None)
    monkeypatch.setenv("ZABBIX_PROVIDER_CIDRS",
                       '{"Provider A": ["192.0.2.0/24"], "Provider B": ["198.51.100.0/24"]}')
    monkeypatch.setattr(classify, "_EXTRA_PROVIDER_NETS", None)
    monkeypatch.setenv("ZBBX_FILE_ROOTS", str(tmp_path))


def _host(hid, name, group, ip=None):
    ifaces = [{"ip": ip}] if ip else []
    return {"hostid": hid, "host": name, "name": name.upper(), "status": "0",
            "groups": [{"name": group}], "interfaces": ifaces}


HOSTS = [
    _host("1", "srv-aq9001", "app_free", "192.0.2.1"),
    _host("2", "srv-aq9002", "app_free", "192.0.2.2"),
    _host("3", "srv-bv9001", "app_paid", "198.51.100.1"),   # agent silent
    _host("4", "srv-hm9001", "app_paid"),                   # no interface at all
    _host("5", "srv-tf9001", "Templates"),                  # unclassified: not a server row
]

DASHBOARDS = [{
    "dashboardid": "10", "name": "Fleet Overview",
    "pages": [
        {"name": "Base", "widgets": [
            {"fields": [{"type": "6", "value": "g1"}, {"type": "6", "value": "g2"}]},
            {"fields": [{"type": "3", "value": "1"}]},        # host widget, not a graph
        ]},
        {"name": "", "widgets": [{"fields": [{"type": "6", "value": "g9"}]}]},   # unresolvable graph
    ],
}]

GRAPHS = [
    {"graphid": "g1", "hosts": [{"hostid": "1"}]},
    {"graphid": "g2", "hosts": [{"hostid": "2"}]},
]


def _item(hid, value, clock=LIVE):
    return {"hostid": hid, "lastvalue": str(value), "lastclock": clock}


METRICS = {
    KEY_CPU: [_item("1", 20), _item("2", 40), _item("3", 95, STALE), _item("4", 70)],
    KEY_LOAD: [_item("1", 1.5), _item("2", 0.5), _item("4", 0.25)],
    KEY_MEM: [_item("1", 4 * GB), _item("2", 8 * GB), _item("3", 1 * GB, STALE)],
}


def _client(hosts=HOSTS, dashboards=DASHBOARDS, graphs=GRAPHS):
    def item_get(p):
        key = (p.get("filter") or {}).get("key_")
        return [i for i in METRICS.get(key, []) if i["hostid"] in p.get("hostids", [])]

    return RecordingClient({
        "host.get": hosts, "dashboard.get": dashboards, "graph.get": graphs, "item.get": item_get,
    })


def _run(c, **kw):
    return run_tool(report, "generate_server_report", c, **kw)


def _report_path(out):
    line = next(ln for ln in out.splitlines() if ln.startswith("**File:**"))
    return line.split("`")[1]


class TestWire:
    def test_the_calls_are_shaped_for_zabbix(self, tmp_path):
        c = _client()
        _run(c, output_dir=str(tmp_path))
        hosts = c.sent("host.get")
        assert hosts["filter"] == {"status": "0"}
        assert hosts["selectGroups"] == ["name"] and hosts["selectInterfaces"] == ["ip"]
        assert c.sent("dashboard.get")["selectPages"] == "extend"
        graphs = c.sent("graph.get")
        assert set(graphs["graphids"]) == {"g1", "g2", "g9"}
        assert graphs["selectHosts"] == ["hostid"]
        keys = [p["filter"]["key_"] for m, p in c.calls if m == "item.get"]
        assert sorted(keys) == sorted([KEY_CPU, KEY_LOAD, KEY_MEM])
        for m, p in c.calls:
            if m == "item.get":
                assert "lastclock" in p["output"], "a value without its clock is not a reading"

    def test_no_graphs_means_no_graph_call(self, tmp_path):
        c = _client(dashboards=[])
        _run(c, output_dir=str(tmp_path))
        assert "graph.get" not in {m for m, _ in c.calls}


class TestHappyPath:
    def test_summary_text(self, tmp_path):
        out = _run(_client(), output_dir=str(tmp_path))
        assert "**Server Report Generated**" in out
        assert "**Servers:** 4 (2 on dashboards)" in out       # the Templates host is not a server
        assert "**Median CPU:** 60.0%" in out                  # 80 / 60 / 30 — the silent agent has no figure
        assert "**Median Load:** 0.50" in out
        assert "**Products:** 2" in out
        assert "**Providers:** 3" in out                       # Provider A, Provider B, No IP
        assert "1. **Servers** — 4 rows, 13 columns" in out

    def test_workbook_is_written_under_the_output_dir(self, tmp_path):
        out = _run(_client(), output_dir=str(tmp_path / "reports"))
        path = _report_path(out)
        assert os.path.dirname(path) == os.path.realpath(tmp_path / "reports")
        assert os.path.basename(path).startswith("zabbix_server_report_")
        assert path.endswith(".xlsx") and os.path.isfile(path)
        assert load_workbook(path).sheetnames == ["Servers", "Products", "Providers"]

    def test_servers_sheet_rows(self, tmp_path):
        ws = load_workbook(_report_path(_run(_client(), output_dir=str(tmp_path))))["Servers"]
        header = [c.value for c in ws[1]]
        assert header[:6] == ["Host", "Name", "Product", "Tier", "IP", "Provider"]
        rows = {r[0]: dict(zip(header, r, strict=True)) for r in ws.iter_rows(min_row=2, values_only=True)}
        assert list(rows) == ["srv-aq9001", "srv-aq9002", "srv-bv9001", "srv-hm9001"]  # product, tier, host
        r1 = rows["srv-aq9001"]
        assert (r1["Product"], r1["Tier"], r1["IP"], r1["Provider"]) == ("app_free", "Default", "192.0.2.1", "Provider A")
        assert (r1["CPU %"], r1["Load Avg5"], r1["Mem Avail GB"]) == (80.0, 1.5, 4.0)
        assert r1["On Dashboard"] == "Yes"
        assert r1["Dashboards"] == "Fleet Overview"
        assert r1["Dashboard Tabs"] == "Fleet Overview / Base"
        assert r1["Groups"] == "app_free"
        assert ws.freeze_panes == "A2" and ws.auto_filter.ref == "A1:M5"

    def test_a_silent_agent_has_no_figure_and_no_colour(self, tmp_path):
        ws = load_workbook(_report_path(_run(_client(), output_dir=str(tmp_path))))["Servers"]
        by_host = {ws.cell(row=r, column=1).value: r for r in range(2, ws.max_row + 1)}
        silent = by_host["srv-bv9001"]
        assert ws.cell(row=silent, column=7).value is None      # CPU %: stale idle 95 is NOT "5%"
        assert ws.cell(row=silent, column=9).value is None      # Mem
        assert ws.cell(row=silent, column=7).fill.fill_type is None
        assert ws.cell(row=silent, column=10).value == "No"
        # colour coding follows the live figures
        assert ws.cell(row=by_host["srv-aq9001"], column=7).fill.start_color.rgb.endswith("FFC7CE")  # 80 -> red
        assert ws.cell(row=by_host["srv-aq9002"], column=7).fill.start_color.rgb.endswith("FFEB9C")  # 60 -> yellow
        assert ws.cell(row=by_host["srv-hm9001"], column=7).fill.start_color.rgb.endswith("C6EFCE")  # 30 -> green

    def test_host_without_interface_has_no_ip_and_no_provider(self, tmp_path):
        ws = load_workbook(_report_path(_run(_client(), output_dir=str(tmp_path))))["Servers"]
        row = next(r for r in ws.iter_rows(min_row=2, values_only=True) if r[0] == "srv-hm9001")
        assert row[4] is None and row[5] is None

    def test_products_sheet_medians(self, tmp_path):
        ws = load_workbook(_report_path(_run(_client(), output_dir=str(tmp_path))))["Products"]
        rows = {(r[0], r[1]): r for r in ws.iter_rows(min_row=2, values_only=True)}
        assert set(rows) == {("app_free", "Default"), ("app_paid", "Default")}
        assert rows[("app_free", "Default")][2:] == (2, 2, 70.0, 1.0)
        # one live CPU figure, no live load figure at all
        assert rows[("app_paid", "Default")][2:] == (2, 0, 30.0, 0.25)

    def test_providers_sheet_is_ranked_by_server_count(self, tmp_path):
        ws = load_workbook(_report_path(_run(_client(), output_dir=str(tmp_path))))["Providers"]
        rows = list(ws.iter_rows(min_row=2, values_only=True))
        assert rows[0] == ("Provider A", 2, 70.0)
        assert {r[0] for r in rows[1:]} == {"Provider B", "No IP"}
        assert dict((r[0], r[2]) for r in rows)["Provider B"] is None   # no live CPU -> blank, not 0


class TestFilters:
    def test_product_filter_is_exact(self, tmp_path):
        out = _run(_client(), product="app_paid", output_dir=str(tmp_path))
        assert "**Servers:** 2 (0 on dashboards)" in out
        assert "**Products:** 1" in out

    def test_country_filter_reads_the_host_name(self, tmp_path):
        out = _run(_client(), country="aq", output_dir=str(tmp_path))
        assert "**Servers:** 2 (2 on dashboards)" in out
        ws = load_workbook(_report_path(out))["Servers"]
        assert [r[0] for r in ws.iter_rows(min_row=2, values_only=True)] == ["srv-aq9001", "srv-aq9002"]


class TestEmptyFleet:
    def test_empty_fleet_still_writes_a_workbook(self, tmp_path):
        out = _run(_client(hosts=[], dashboards=[], graphs=[]), output_dir=str(tmp_path))
        assert "**Servers:** 0 (0 on dashboards)" in out
        assert "Median CPU" not in out and "Median Load" not in out
        assert "**Products:** 0" in out and "**Providers:** 0" in out
        wb = load_workbook(_report_path(out))
        assert wb["Servers"].max_row == 1 and wb["Products"].max_row == 1


class TestConfinement:
    def test_output_dir_outside_the_roots_is_refused(self):
        out = _run(_client(), output_dir="/nonexistent-root-for-test/reports")
        assert out.startswith("Error generating report:")
        assert "not in the allowed roots" in out
