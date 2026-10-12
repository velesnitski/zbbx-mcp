"""Wire tests for ``get_trends_batch`` (daily view), ``get_server_dashboard``
and ``compare_servers``. Fixtures are synthetic.

``fetch_trends_batch`` is replaced by a recorder returning fixed TrendRows, so
these tests pin the rendering and the host-resolution calls; the trend fetch
itself is covered by tests/test_trends_batch_hosts.py.
"""

import ipaddress

import httpx
import pytest

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp import classify
from zbbx_mcp.data import TrendRow, day_label
from zbbx_mcp.tools import trends_compare

D1, D2 = "2026-10-01", "2026-10-02"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(classify, "_PRODUCT_MAP", {})
    monkeypatch.setattr(classify, "_EXTRA_PROVIDER_NETS",
                        [("Provider A", ipaddress.ip_network("192.0.2.0/24"))])


def _boom(_params):
    raise httpx.ConnectError("unreachable")


def _row(hid, host, metric, avg, peak, mn, cur, trend="stable", daily=None):
    return TrendRow(hostid=hid, hostname=host, metric=metric, avg=avg, peak=peak,
                    min_val=mn, current=cur, trend_dir=trend, daily=daily or {})


ROWS = [
    _row("1", "srv-aq9001", "cpu", 20.0, 60.0, 5.0, 25.0, "up", {D1: 18.0, D2: 22.0}),
    _row("1", "srv-aq9001", "traffic", 400.0, 700.0, 100.0, 420.0, "stable", {D1: 390.0, D2: 410.0}),
    _row("2", "srv-bv9001", "cpu", 10.0, 30.0, 2.0, None, "", {D2: 11.0}),
    _row("2", "srv-bv9001", "traffic", 0.0, 0.0, 0.0, 0.0, "down", {}),
]


@pytest.fixture
def fetched(monkeypatch):
    calls = []

    async def fake(client, hostids, metrics, period):
        calls.append((list(hostids), list(metrics), period))
        rows = [r for r in ROWS if r.hostid in hostids and r.metric in metrics]
        return rows, {}
    monkeypatch.setattr(trends_compare, "fetch_trends_batch", fake)
    return calls


HOSTS = [
    {"hostid": "1", "host": "srv-aq9001", "groups": [{"name": "app_free"}], "interfaces": [{"ip": "192.0.2.1"}]},
    {"hostid": "2", "host": "srv-bv9001", "groups": [{"name": "app_paid"}], "interfaces": []},
]


# ------------------------------------------------------- get_trends_batch daily

def test_get_trends_batch_daily_table(fetched):
    c = RecordingClient({"host.get": HOSTS})
    out = run_tool(trends_compare, "get_trends_batch", c, aggregation="daily", metrics="cpu,traffic")
    assert fetched == [(["1", "2"], ["cpu", "traffic"], "7d")]
    assert out.startswith("**Daily Trends (7d) for 2 servers**")
    assert f"| Server | Metric | {day_label(D1)} | {day_label(D2)} |" in out
    assert "| srv-aq9001 | cpu | 18.0 % | 22.0 % |" in out
    assert "| srv-bv9001 | cpu |  | 11.0 % |" in out
    assert "| srv-aq9001 | traffic | 390.0 Mbps | 410.0 Mbps |" in out


def test_get_trends_batch_daily_compact(fetched):
    c = RecordingClient({"host.get": HOSTS})
    out = run_tool(trends_compare, "get_trends_batch", c, aggregation="daily",
                   format="compact", metrics="cpu,load")
    assert f"host|metric|{day_label(D1)}|{day_label(D2)} (units: cpu=%, load=ratio)" in out
    assert "srv-aq9001|cpu|18.0|22.0" in out and "srv-bv9001|cpu||11.0" in out


def test_get_trends_batch_summary_compact_and_bad_format(fetched):
    c = RecordingClient({"host.get": HOSTS})
    out = run_tool(trends_compare, "get_trends_batch", c, format="compact", metrics="cpu")
    assert "host|metric|avg|peak|min|now|trend (units: cpu=%)" in out
    assert "srv-bv9001|cpu|10.0|30.0|2.0|n/a|n/a" in out
    out = run_tool(trends_compare, "get_trends_batch", RecordingClient(), format="xml")
    assert out == "Unknown format 'xml'. Use 'table' or 'compact'."


def test_get_trends_batch_daily_without_days_and_no_data(fetched):
    c = RecordingClient({"host.get": HOSTS[1:]})
    out = run_tool(trends_compare, "get_trends_batch", c, aggregation="daily", metrics="traffic")
    assert out == "No daily data available."
    c = RecordingClient({"host.get": HOSTS})
    out = run_tool(trends_compare, "get_trends_batch", c, metrics="memory", period="30d")
    assert out == "No trend data for the last 30d."


def test_get_trends_batch_group_filters_and_errors(fetched):
    c = RecordingClient({"hostgroup.get": [], "host.get": HOSTS})
    out = run_tool(trends_compare, "get_trends_batch", c, group="app_none")
    assert out == "Host group 'app_none' not found."
    c = RecordingClient({"host.get": HOSTS})
    run_tool(trends_compare, "get_trends_batch", c, country="bv", product="app_paid")
    assert fetched[-1][0] == ["2"]
    out = run_tool(trends_compare, "get_trends_batch", RecordingClient({"host.get": _boom}))
    assert out == "Error: unreachable"


# ------------------------------------------------------- get_server_dashboard

def test_get_server_dashboard_by_name_daily(fetched):
    c = RecordingClient({"host.get": [HOSTS[0]]})
    out = run_tool(trends_compare, "get_server_dashboard", c, host="srv-aq9001")
    assert c.sent("host.get")["filter"] == {"host": ["srv-aq9001"]}
    assert fetched == [(["1"], ["cpu", "traffic", "load", "memory"], "7d")]
    assert out.startswith("# Dashboard: srv-aq9001 (last 7d)")
    assert "**Cpu:** avg 20.0 % | peak 60.0 % | min 5.0 % | current 25.0 % | trend: up" in out
    assert "## Daily Breakdown" in out
    assert f"| Metric | {day_label(D1)} | {day_label(D2)} |" in out
    assert "| cpu (%) | 18.0 | 22.0 |" in out and "| traffic (Mbps) | 390.0 | 410.0 |" in out


def test_get_server_dashboard_falls_back_to_search_then_not_found(fetched):
    def host_get(p):
        return [HOSTS[1]] if "search" in p else []
    c = RecordingClient({"host.get": host_get})
    out = run_tool(trends_compare, "get_server_dashboard", c, host="bv9001", aggregation="summary")
    searches = [p for m, p in c.calls if m == "host.get"]
    assert searches[1]["search"] == {"host": "bv9001", "name": "bv9001"} and searches[1]["limit"] == 1
    assert out.startswith("# Dashboard: srv-bv9001")
    assert "current n/a (not reporting)" in out and "Daily Breakdown" not in out
    out = run_tool(trends_compare, "get_server_dashboard", RecordingClient(), host="srv-pn9001")
    assert out == "Host 'srv-pn9001' not found."


def test_get_server_dashboard_by_id_and_no_data(fetched):
    c = RecordingClient({"host.get": []})
    out = run_tool(trends_compare, "get_server_dashboard", c, host="77", period="1d")
    assert c.sent("host.get") == {"hostids": ["77"], "output": ["host"]}
    assert out == "No trend data for '77' in the last 1d."
    c = RecordingClient({"host.get": [{"host": "srv-aq9001"}]})
    out = run_tool(trends_compare, "get_server_dashboard", c, host="1")
    assert out.startswith("# Dashboard: srv-aq9001")
    out = run_tool(trends_compare, "get_server_dashboard", RecordingClient({"host.get": _boom}), host="x")
    assert out == "Error: unreachable"


# ------------------------------------------------------------ compare_servers

def test_compare_servers_side_by_side_with_efficiency(fetched):
    c = RecordingClient({"host.get": HOSTS})
    out = run_tool(trends_compare, "compare_servers", c,
                   hosts="srv-aq9001, srv-bv9001", metrics="cpu,traffic,memory")
    assert c.sent("host.get")["filter"] == {"host": ["srv-aq9001", "srv-bv9001"]}
    assert out.startswith("**Server Comparison (7d)**")
    assert "| Metric | srv-aq9001 | srv-bv9001 |" in out
    assert "| cpu avg | 20.0 % | 10.0 % |" in out
    assert "| cpu now | 25.0 % | n/a (not reporting) |" in out
    assert "| cpu trend | up | n/a |" in out
    assert "| traffic peak | 700.0 Mbps | 0.0 Mbps |" in out
    assert "memory" not in out                                    # no rows for it
    assert "| CPU per 100 Mbps | 5.0% | N/A |" in out
    assert "| BW headroom (vs 800) | 100 Mbps | 800 Mbps |" in out
    assert "| Provider | Provider A | ? |" in out
    assert "| Country | AQ | BV |" in out


def test_compare_servers_missing_metric_for_one_host(fetched, monkeypatch):
    monkeypatch.setattr(trends_compare, "fetch_trends_batch",
                        _only([ROWS[0], ROWS[1], ROWS[2]]))
    c = RecordingClient({"host.get": HOSTS})
    out = run_tool(trends_compare, "compare_servers", c, hosts="srv-aq9001,srv-bv9001")
    assert "| traffic avg | 400.0 Mbps | N/A |" in out
    assert "| CPU per 100 Mbps | 5.0% | N/A |" in out
    assert "| BW headroom (vs 800) | 100 Mbps | N/A |" in out


def _only(rows):
    async def fake(client, hostids, metrics, period):
        return rows, {}
    return fake


def test_compare_servers_guards(fetched):
    assert run_tool(trends_compare, "compare_servers", RecordingClient(), hosts="srv-aq9001") == \
        "Need at least 2 hostnames to compare."
    c = RecordingClient({"host.get": HOSTS[:1]})
    assert run_tool(trends_compare, "compare_servers", c, hosts="srv-aq9001,srv-pn9001") == \
        "Found only 1 hosts. Need at least 2."
    c = RecordingClient({"host.get": HOSTS})
    out = run_tool(trends_compare, "compare_servers", c, hosts="srv-aq9001,srv-bv9001", metrics="load")
    assert out == "No trend data for the last 7d."
    out = run_tool(trends_compare, "compare_servers", RecordingClient({"host.get": _boom}),
                   hosts="srv-aq9001,srv-bv9001")
    assert out == "Error comparing servers: unreachable"
