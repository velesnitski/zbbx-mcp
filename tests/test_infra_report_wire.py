"""``generate_infra_report`` wire test: inventory, underloaded candidates and a
provider cost summary from one host fetch plus five metric reads. Synthetic."""

from __future__ import annotations

import os
import time

import pytest
from openpyxl import load_workbook

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp import classify
from zbbx_mcp.tools import infra_report

NOW = int(time.time())
LIVE = str(NOW)
STALE = str(NOW - 7200)

KEY_CPU = "system.cpu.util[,idle]"
KEY_LOAD = "system.cpu.load[percpu,avg5]"
KEY_MEM_AVAIL = "vm.memory.size[available]"
KEY_MEM_TOTAL = "vm.memory.size[total]"
GB = 1_073_741_824


@pytest.fixture(autouse=True)
def _synthetic_env(monkeypatch, tmp_path):
    monkeypatch.setenv("ZABBIX_PRODUCT_MAP", "")
    monkeypatch.setattr(classify, "_PRODUCT_MAP", None)
    monkeypatch.setenv("ZABBIX_PROVIDER_CIDRS",
                       '{"Provider A": ["192.0.2.0/24"], "Provider B": ["198.51.100.0/24"]}')
    monkeypatch.setattr(classify, "_EXTRA_PROVIDER_NETS", None)
    monkeypatch.setenv("ZBBX_FILE_ROOTS", str(tmp_path))


def _host(hid, name, group, ip=None):
    ifaces = [{"ip": ip, "type": "1", "port": "10050"}] if ip else []
    return {"hostid": hid, "host": name, "name": name.upper(), "status": "0",
            "groups": [{"name": group}], "interfaces": ifaces}


HOSTS = [
    _host("1", "srv-aq9001", "app_free", "192.0.2.1"),     # nearly idle
    _host("2", "srv-aq9002", "app_free", "192.0.2.2"),     # fully idle
    _host("3", "srv-bv9001", "app_paid", "198.51.100.1"),  # busy
    _host("4", "srv-hm9001", "app_paid"),                  # no IP, agent silent
    _host("5", "srv-tf9001", "Templates"),                 # unclassified: skipped
]

DASHBOARDS = [{
    "dashboardid": "10", "name": "Fleet Overview",
    "pages": [{"name": "Base", "widgets": [{"fields": [{"type": "6", "value": "g1"}]}]}],
}]
GRAPHS = [{"graphid": "g1", "hosts": [{"hostid": "1"}]}]


def _item(hid, value, clock=LIVE):
    return {"hostid": hid, "lastvalue": str(value), "lastclock": clock}


METRICS = {
    KEY_CPU: [_item("1", 97), _item("2", 99.5), _item("3", 30), _item("4", 98, STALE)],
    KEY_LOAD: [_item("1", 0.1), _item("2", 0.02), _item("3", 4.5)],
    KEY_MEM_AVAIL: [_item("1", 12 * GB), _item("3", 2 * GB)],
    KEY_MEM_TOTAL: [_item("1", 16 * GB), _item("3", 8 * GB)],
}
COST = [
    {"hostid": "1", "hostmacroid": "m1", "macro": "{$COST_MONTH}", "value": "100"},
    {"hostid": "2", "hostmacroid": "m2", "macro": "{$COST_MONTH}", "value": "200"},
    {"hostid": "3", "hostmacroid": "m3", "macro": "{$COST_MONTH}", "value": "n/a"},
    {"hostid": "4", "hostmacroid": "m4", "macro": "{$COST_MONTH}", "value": "40"},
]


def _client(hosts=HOSTS, dashboards=DASHBOARDS, cost=COST):
    def item_get(p):
        key = (p.get("filter") or {}).get("key_")
        return [i for i in METRICS.get(key, []) if i["hostid"] in p["hostids"]]

    return RecordingClient({
        "host.get": hosts, "dashboard.get": dashboards, "graph.get": GRAPHS,
        "item.get": item_get, "usermacro.get": cost,
    })


def _run(c, **kw):
    return run_tool(infra_report, "generate_infra_report", c, **kw)


def _path(out):
    return next(ln for ln in out.splitlines() if ln.startswith("**File:**")).split("`")[1]


class TestWire:
    def test_calls(self, tmp_path):
        c = _client()
        _run(c, output_dir=str(tmp_path))
        hosts = c.sent("host.get")
        assert hosts["filter"] == {"status": "0"}
        assert hosts["selectInterfaces"] == ["ip", "type", "port"]
        assert c.sent("usermacro.get")["filter"] == {"macro": "{$COST_MONTH}"}
        assert set(c.sent("usermacro.get")["hostids"]) == {"1", "2", "3", "4", "5"}
        assert c.sent("graph.get") == {"graphids": ["g1"], "output": ["graphid"], "selectHosts": ["hostid"]}
        keys = [p["filter"]["key_"] for m, p in c.calls if m == "item.get"]
        assert sorted(keys) == sorted([KEY_CPU, KEY_LOAD, KEY_MEM_AVAIL, KEY_MEM_TOTAL])
        assert all("lastclock" in p["output"] for m, p in c.calls if m == "item.get")


class TestHappyPath:
    def test_summary_text(self, tmp_path):
        out = _run(_client(), output_dir=str(tmp_path))
        assert "**Infrastructure Report Generated**" in out
        assert "**Total servers:** 4 (3 with cost data)" in out
        assert "**Underloaded (CPU <10%):** 2" in out
        assert "**Providers:** 3" in out
        assert "**Total cost:** $340.00/month ($4,080.00/year)" in out
        assert "**Potential savings:** $300.00/month from underloaded servers" in out
        assert "1. **Apps & Infra** — 4 servers × 17 columns" in out
        assert "2. **Unused & Underloaded** — 2 decommission candidates" in out
        assert "No cost data found" not in out

    def test_top_candidates_are_the_idlest_first(self, tmp_path):
        out = _run(_client(), output_dir=str(tmp_path))
        assert "### Top Decommission Candidates" in out
        lines = [ln for ln in out.splitlines() if ln.startswith("- **srv-")]
        assert lines[0] == "- **srv-aq9002** (Provider A/app_free) — CPU 0.5% — $200.0/mo"
        assert lines[1] == "- **srv-aq9001** (Provider A/app_free) — CPU 3.0% — $100.0/mo"
        assert "srv-hm9001" not in out        # a silent agent is not "idle"

    def test_workbook_and_inventory_sheet(self, tmp_path):
        out = _run(_client(), output_dir=str(tmp_path / "infra"))
        path = _path(out)
        assert os.path.dirname(path) == os.path.realpath(tmp_path / "infra")
        assert os.path.basename(path).startswith("zabbix_infra_report_") and path.endswith(".xlsx")
        wb = load_workbook(path)
        assert wb.sheetnames == ["Apps & Infra", "Unused & Underloaded", "Provider Summary"]
        ws = wb["Apps & Infra"]
        header = [c.value for c in ws[1]]
        rows = {r[1]: dict(zip(header, r, strict=True)) for r in ws.iter_rows(min_row=2, values_only=True)}
        assert list(rows) == ["srv-aq9001", "srv-aq9002", "srv-bv9001", "srv-hm9001"]
        r1 = rows["srv-aq9001"]
        assert (r1["#"], r1["Product"], r1["Tier"], r1["Provider"], r1["IP"]) == (1, "app_free", "Default", "Provider A", "192.0.2.1")
        assert (r1["RAM Total GB"], r1["RAM Avail GB"], r1["CPU Used %"], r1["Load Avg5"]) == (16.0, 12.0, 3.0, 0.1)
        assert (r1["Cost/Month ($)"], r1["Cost/Year ($)"]) == (100, 1200.0)
        assert (r1["Dashboard"], r1["Dashboard Tab"], r1["Status"]) == ("Fleet Overview", "Fleet Overview / Base", "Active")
        r3 = rows["srv-bv9001"]
        assert r3["CPU Used %"] == 70.0 and r3["Cost/Month ($)"] is None and r3["Dashboard"] is None
        r4 = rows["srv-hm9001"]
        assert r4["CPU Used %"] is None and r4["IP"] is None and r4["Provider"] is None
        assert ws.auto_filter.ref == "A1:Q5"

    def test_cpu_colouring(self, tmp_path):
        ws = load_workbook(_path(_run(_client(), output_dir=str(tmp_path))))["Apps & Infra"]
        col = [c.value for c in ws[1]].index("CPU Used %") + 1
        fills = {ws.cell(row=r, column=2).value: ws.cell(row=r, column=col).fill for r in range(2, 6)}
        assert fills["srv-aq9001"].start_color.rgb.endswith("C6EFCE")   # 3% -> green
        assert fills["srv-bv9001"].start_color.rgb.endswith("FFEB9C")   # 70% -> yellow
        assert fills["srv-hm9001"].fill_type is None

    def test_underloaded_sheet(self, tmp_path):
        ws = load_workbook(_path(_run(_client(), output_dir=str(tmp_path))))["Unused & Underloaded"]
        header = [c.value for c in ws[1]]
        rows = [dict(zip(header, r, strict=True)) for r in ws.iter_rows(min_row=2, values_only=True)]
        assert [r["Resource"] for r in rows] == ["srv-aq9002", "srv-aq9001"]
        assert rows[0]["Priority"] == "High" and rows[1]["Priority"] == "Medium"
        assert rows[1]["Reason"] == "CPU idle 97% (used only 3.0%)"
        assert rows[0]["Recommendation"] == "Review for decommission"
        assert rows[0]["Host/Domain"] == "192.0.2.2" and rows[0]["Cost/Month ($)"] == 200
        prio_col = header.index("Priority") + 1
        assert ws.cell(row=2, column=prio_col).fill.start_color.rgb.endswith("FFC7CE")
        assert ws.cell(row=3, column=prio_col).fill.start_color.rgb.endswith("FFEB9C")

    def test_provider_summary_is_ranked_by_cost(self, tmp_path):
        ws = load_workbook(_path(_run(_client(), output_dir=str(tmp_path))))["Provider Summary"]
        rows = list(ws.iter_rows(min_row=2, values_only=True))
        assert [r[0] for r in rows] == ["Provider A", "No IP", "Provider B", "TOTAL"]
        assert rows[0] == ("Provider A", 2, 300, 3600, 2, 300, 1.8, 2)
        assert rows[1] == ("No IP", 1, 40, 480, 0, None, None, 0)       # silent agent: no CPU figures
        assert rows[2] == ("Provider B", 1, None, None, 0, None, 70.0, 0)
        assert rows[3][:6] == ("TOTAL", 4, 340, 4080, 2, 300)


class TestThreshold:
    def test_threshold_moves_the_idle_line(self, tmp_path):
        out = _run(_client(), cpu_idle_threshold=99.0, output_dir=str(tmp_path))
        assert "**Underloaded (CPU <1%):** 1" in out
        assert "**Potential savings:** $200.00/month" in out
        assert "srv-aq9001" not in out.split("### Top Decommission Candidates")[1]


class TestEmptyFleet:
    def test_empty_fleet(self, tmp_path):
        c = _client(hosts=[], dashboards=[], cost=[])
        out = _run(c, output_dir=str(tmp_path))
        assert "**Total servers:** 0 (0 with cost data)" in out
        assert "**Underloaded (CPU <10%):** 0" in out
        assert "Total cost" not in out and "Potential savings" not in out
        assert "No cost data found" in out
        assert "Top Decommission Candidates" not in out
        assert "graph.get" not in {m for m, _ in c.calls}
        wb = load_workbook(_path(out))
        assert wb["Apps & Infra"].max_row == 1
        assert [r[0] for r in wb["Provider Summary"].iter_rows(min_row=2, values_only=True)] == ["TOTAL"]

    def test_no_cost_macros_is_said_not_hidden(self, tmp_path):
        out = _run(_client(cost=[]), output_dir=str(tmp_path))
        assert "**Total servers:** 4 (0 with cost data)" in out
        assert "No cost data found" in out and "import_server_costs" in out
        assert "Potential savings" not in out          # idle servers without a price save nothing on paper

    def test_output_dir_outside_the_roots_is_refused(self):
        out = _run(_client(), output_dir="/nonexistent-root-for-test/x")
        assert out.startswith("Error generating report:") and "not in the allowed roots" in out
