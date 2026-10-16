"""Wire tests for ``tools/executive.py``: the leadership dashboards driven
through a recording client. Every fixture is synthetic."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import httpx
import pytest

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp import classify, fetch
from zbbx_mcp.data import TRAFFIC_IN_KEYS
from zbbx_mcp.tools import executive

NOW = int(time.time())
LIVE = str(NOW - 60)
STALE = str(NOW - 7200)
KEY_CPU = "system.cpu.util[,idle]"
SVC = "svc.check"
SVC2 = "svc.check2"
MBPS = 1_000_000  # default ZABBIX_TRAFFIC_UNIT is bits/s


@pytest.fixture(autouse=True)
def _synthetic_env(monkeypatch, tmp_path):
    monkeypatch.setenv("ZABBIX_PRODUCT_MAP", "")
    monkeypatch.setattr(classify, "_PRODUCT_MAP", None)
    monkeypatch.setenv("ZABBIX_PROVIDER_CIDRS",
                       '{"Provider A": ["192.0.2.0/24"], "Provider B": ["198.51.100.0/24"]}')
    monkeypatch.setattr(classify, "_EXTRA_PROVIDER_NETS", None)
    monkeypatch.setenv("ZBBX_FILE_ROOTS", str(tmp_path))


def _host(hid, name, group, ip=None):
    groups = [{"name": group}] if group else []
    return {"hostid": hid, "host": name, "groups": groups,
            "interfaces": [{"ip": ip}] if ip else []}


HOSTS = [
    _host("1", "srv-aq9001", "app_free", "192.0.2.1"),
    _host("2", "srv-aq9002", "app_free", "192.0.2.2"),
    _host("3", "srv-bv9001", "app_paid", "198.51.100.1"),
    _host("4", "srv-hm9001", "app_paid", "203.0.113.1"),
    _host("5", "srv-tf9001", "app_paid", "10.0.0.5"),
]


def _traffic(hid, mbps, clock=LIVE, nic="eth0", suffix=""):
    return {"itemid": f"t{hid}{suffix}", "hostid": hid, "key_": f"net.if.in[{nic}]",
            "lastvalue": str(mbps * MBPS), "lastclock": clock, "value_type": "3"}


TRAFFIC = [_traffic("1", 300), _traffic("2", 100), _traffic("3", 100), _traffic("4", 0)]
CPU = [  # idle → used: 80 / 70 / 10; host 4 stale (dropped), host 5 never reported
    {"hostid": "1", "lastvalue": "20", "lastclock": LIVE},
    {"hostid": "2", "lastvalue": "30", "lastclock": LIVE},
    {"hostid": "3", "lastvalue": "90", "lastclock": LIVE},
    {"hostid": "4", "lastvalue": "5", "lastclock": STALE},
]


_SIXTEEN_DAYS = range(16)


def _daily(itemid, prior_mbps, recent_mbps, days=_SIXTEEN_DAYS):
    """One point per day for ``days`` days back: k ≥ 7 → prior, k < 7 → recent."""
    return [{"itemid": itemid, "clock": str(NOW - k * 86400),
             "value_avg": str((prior_mbps if k >= 7 else recent_mbps) * MBPS),
             "value_max": str((prior_mbps if k >= 7 else recent_mbps) * MBPS),
             "value_min": "0"} for k in days]


DAILY = _daily("t1", 100, 200) + _daily("t3", 100, 60)


def _client(hosts=None, traffic=None, cpu=None, trends=None, service=None, dashboards=None,
            lookup=None):
    hosts = HOSTS if hosts is None else hosts
    traffic = TRAFFIC if traffic is None else traffic
    cpu = CPU if cpu is None else cpu
    trends = DAILY if trends is None else trends

    def host_get(p):
        if (p.get("filter") or {}).get("host"):
            return lookup or []
        if p.get("hostids"):
            return [{"hostid": h["hostid"], "host": h["host"]} for h in hosts
                    if h["hostid"] in p["hostids"]]
        return hosts

    def item_get(p):
        ids = set(p.get("hostids", []))
        key = (p.get("filter") or {}).get("key_")
        if "search" in p or key == TRAFFIC_IN_KEYS:
            return [t for t in traffic if t["hostid"] in ids]
        if key == KEY_CPU:
            return [c for c in cpu if c["hostid"] in ids]
        if service is not None and key in (SVC, [SVC], [SVC, SVC2]):
            return [s for s in service if s["hostid"] in ids]
        return []

    return RecordingClient({
        "host.get": host_get, "item.get": item_get,
        "trend.get": lambda p: [t for t in trends if t["itemid"] in p["itemids"]],
        "dashboard.get": dashboards or [],
    })


def _failing():
    def boom(_p):
        raise httpx.ConnectError("unreachable")
    return RecordingClient({"host.get": boom})


def _run(tool, c, **kw):
    return run_tool(executive, tool, c, **kw)


# --------------------------------------------------------------- dashboard


class TestExecutiveDashboard:
    def test_kpis_growth_and_decline(self):
        c = _client()
        out = _run("get_executive_dashboard", c)
        lines = out.splitlines()
        assert lines[0] == "**Fleet:** 5 servers, 4 countries, 2 products, 2 providers"
        # 500 Mbps of live traffic; CPU averages only the three live readings.
        assert lines[1] == "**Traffic:** 0.5 Gbps | **Avg CPU:** 53.3%"
        assert "**High CPU (>70%):** AQ" in lines
        assert "**Growth (30d):** AQ +100%" in lines
        assert "**Declining:** BV -40%" in lines
        trend = c.sent("trend.get")
        assert set(trend["itemids"]) == {"t1", "t2", "t3", "t4"}
        assert abs(trend["time_from"] - (NOW - 30 * 86400)) < 120

    def test_short_trend_history_is_not_growth(self):
        out = _run("get_executive_dashboard", _client(trends=_daily("t1", 100, 900, range(5))))
        assert "Growth" not in out and "Declining" not in out

    def test_empty_fleet(self):
        out = _run("get_executive_dashboard", _client(hosts=[]))
        assert out == ("**Fleet:** 0 servers, 0 countries, 0 products, 0 providers\n"
                       "**Traffic:** 0.0 Gbps | **Avg CPU:** 0%")

    def test_api_error(self):
        assert _run("get_executive_dashboard", _failing()) == "Error: unreachable"


# ------------------------------------------------------- month over month


class TestMonthOverMonth:
    def test_period_delta(self):
        c = _client()
        out = _run("get_month_over_month", c, days=7)
        assert "| Traffic Gbps | 0.2 | 0.3 | +50.0% |" in out
        assert "| Avg CPU % | – | 53.3 | – |" in out
        assert "| Countries | 2 | 2 | +0.0% |" in out
        assert "| Servers | 5 | 5 | – |" in out
        assert "retention" not in out
        assert abs(c.sent("trend.get")["time_from"] - (NOW - 14 * 86400)) < 120

    def test_short_retention_reads_na(self):
        out = _run("get_month_over_month", _client(trends=_daily("t1", 100, 100, range(4))), days=7)
        assert "| Traffic Gbps | 0.0 | 0.1 | n/a |" in out
        assert "Trend retention shorter than 14d" in out

    def test_no_trends(self):
        out = _run("get_month_over_month", _client(trends=[]), days=7)
        assert "| Traffic Gbps | 0.0 | 0.0 | – |" in out
        assert "| Countries | 0 | 0 | – |" in out

    def test_api_error(self):
        assert _run("get_month_over_month", _failing()) == "Error: unreachable"


# --------------------------------------------------------------- risk score


class TestFleetRisk:
    def test_scores(self):
        out = _run("get_fleet_risk_score", _client(), region="")
        assert out.startswith("**Fleet Risk Score — ** (4 countries)")
        # AQ: one provider (+30), CPU 75% (+15), two servers (+10).
        assert "| AQ | 55/100 | 2 | single provider | diversify providers |" in out
        # BV: one provider (+30), a single server (+25).
        assert "| BV | 55/100 | 1 | single provider | add servers |" in out

    def test_traffic_concentration_and_cpu(self):
        hosts = [_host("1", "srv-aq9001", "app_free", "192.0.2.1"),
                 _host("2", "srv-aq9002", "app_free", "198.51.100.2"),
                 _host("3", "srv-aq9003", "app_free", "198.51.100.3")]
        cpu = [{"hostid": h, "lastvalue": "10", "lastclock": LIVE} for h in ("1", "2", "3")]
        traffic = [_traffic("1", 900), _traffic("2", 10), _traffic("3", 10)]
        out = _run("get_fleet_risk_score", _client(hosts=hosts, cpu=cpu, traffic=traffic), region="")
        # 2/3 on one provider (+15), CPU 90% (+30), traffic 98% on one host (+15).
        assert "| AQ | 60/100 | 3 | CPU >80% | upgrade capacity |" in out

    def test_min_servers_and_truncation(self):
        out = _run("get_fleet_risk_score", _client(), region="", min_servers=2)
        assert "(1 countries)" in out and "| AQ |" in out and "| BV |" not in out
        out = _run("get_fleet_risk_score", _client(), region="", max_results=1)
        assert "*3 more omitted*" in out

    def test_region_without_matches(self):
        out = _run("get_fleet_risk_score", _client(), region="LATAM")
        assert out.startswith("**Fleet Risk Score — LATAM** (0 countries)")

    def test_api_error(self):
        assert _run("get_fleet_risk_score", _failing()) == "Error: unreachable"


# -------------------------------------------------------------- SLA


def _svc(hid, v, clock=LIVE, key=SVC):
    return {"hostid": hid, "key_": key, "lastvalue": str(v), "lastclock": clock}


SERVICE = [_svc("1", 1), _svc("2", 0), _svc("3", 1), _svc("4", 0)]


class TestSlaDashboard:
    @pytest.fixture(autouse=True)
    def _key(self, monkeypatch):
        monkeypatch.setattr(executive, "KEY_service_PRIMARY", SVC)

    def test_rows_and_disclosure(self):
        c = _client(service=SERVICE)
        out = _run("get_sla_dashboard", c)
        sent = c.sent("item.get")
        assert sent["filter"] == {"key_": SVC, "status": "0"}
        assert sent["output"] == ["hostid", "lastvalue", "lastclock"]
        lines = out.splitlines()
        # Lowest uptime first: host 4 reads 0 and carries no traffic.
        body = [ln for ln in lines if ln.startswith("| app_")]
        assert body[0] == "| app_paid | HM | 0.0% | 1 | 1 | 0.0 |"
        # Host 2 reads 0 but carries 100 Mbps → traffic-validated as up.
        assert "| app_free | AQ | 100.0% | 2 | 0 | 0.4 |" in body
        assert "| app_paid | BV | 100.0% | 1 | 0 | 0.1 |" in body
        assert "_1 enabled host(s) carry no item" in out

    def test_filters(self):
        out = _run("get_sla_dashboard", _client(service=SERVICE), product="app_paid", country="hm")
        assert [ln for ln in out.splitlines() if ln.startswith("| app_")] == [
            "| app_paid | HM | 0.0% | 1 | 1 | 0.0 |"]
        out = _run("get_sla_dashboard", _client(service=SERVICE), max_results=1)
        assert "*2 more omitted*" in out

    def test_stale_check_counts_as_no_item(self):
        service = [_svc("1", 1, clock=STALE)]
        out = _run("get_sla_dashboard", _client(service=service), product="app_free")
        assert out == "No servers match the filters."

    def test_unconfigured_key(self, monkeypatch):
        monkeypatch.setattr(executive, "KEY_service_PRIMARY", "")
        c = _client(service=SERVICE)
        assert _run("get_sla_dashboard", c) == "No servers match the filters."
        assert not any(p.get("filter", {}).get("key_") == SVC for _m, p in c.calls)

    def test_api_error(self):
        assert _run("get_sla_dashboard", _failing()) == "Error: unreachable"


# ------------------------------------------------------------- snapshot


class TestReportSnapshot:
    def test_json(self):
        snap = json.loads(_run("get_report_snapshot", _client()))
        assert snap["date"] == datetime.now(timezone.utc).strftime("%Y-%m-%d")
        assert (snap["servers"], snap["countries"], snap["products"]) == (5, 4, 2)
        assert (snap["traffic_gbps"], snap["avg_cpu"]) == (0.5, 53.3)
        assert snap["top_countries"][0] == {"cc": "AQ", "servers": 2, "traffic_mbps": 400.0}
        assert [r["cc"] for r in snap["top_countries"]][:2] == ["AQ", "BV"]

    def test_file(self, tmp_path):
        target = tmp_path / "snap.json"
        out = _run("get_report_snapshot", _client(), output=str(target))
        assert out == f"Snapshot saved to {target.resolve()}"
        assert json.loads(target.read_text())["servers"] == 5

    def test_path_outside_roots_is_refused(self, tmp_path):
        out = _run("get_report_snapshot", _client(), output="/nonexistent-root/snap.json")
        assert out.startswith("Error:") and "allowed roots" in out

    def test_empty(self):
        snap = json.loads(_run("get_report_snapshot", _client(hosts=[])))
        assert snap["servers"] == 0 and snap["avg_cpu"] == 0 and snap["top_countries"] == []

    def test_api_error(self):
        assert _run("get_report_snapshot", _failing()) == "Error: unreachable"


# -------------------------------------------------------------- peak


_DAY0 = (NOW // 86400 - 1) * 86400  # yesterday 00:00 UTC
HOURLY = [
    {"itemid": "t1", "clock": str(_DAY0 + 2 * 3600), "value_avg": str(100 * MBPS), "value_max": str(150 * MBPS)},
    {"itemid": "t1", "clock": str(_DAY0 + 14 * 3600), "value_avg": str(300 * MBPS), "value_max": str(400 * MBPS)},
    {"itemid": "t2", "clock": str(_DAY0 + 14 * 3600), "value_avg": str(300 * MBPS), "value_max": str(350 * MBPS)},
]


class TestPeakAnalysis:
    def test_fleet_busiest_nic_and_hours(self):
        traffic = [*TRAFFIC, _traffic("1", 1, nic="eth1", suffix="b")]   # idle second NIC
        c = _client(traffic=traffic, trends=HOURLY)
        out = _run("get_peak_analysis", c)
        item = c.sent("item.get")
        # Prefix discovery through the shared physical-NIC helper (ADR 153).
        assert item["search"] == {"key_": "*net.if.in[*"}
        assert "lastvalue" in item["output"]
        trend = c.sent("trend.get")
        assert set(trend["itemids"]) == {"t1", "t2", "t3", "t4"}       # never t1b
        assert trend["limit"] == 4 * 24 * 7
        assert out.startswith("**Peak Analysis — Fleet** (5 servers, 7d)")
        assert "| 02:00 | 100 | 150 | 1 |" in out
        assert "| 14:00 | 300 | 400 | 2 |" in out
        assert "**Busiest hour:** 14:00 (300 Mbps avg) | **Quietest:** 02:00 (100 Mbps avg) | **Ratio:** 3.0x" in out

    def test_country(self):
        c = _client(trends=HOURLY)
        out = _run("get_peak_analysis", c, country="aq", period="3d")
        assert out.startswith("**Peak Analysis — AQ** (2 servers, 3d)")
        assert set(c.sent("item.get")["hostids"]) == {"1", "2"}

    def test_single_host_by_name(self):
        c = _client(trends=HOURLY, lookup=[{"hostid": "1", "host": "srv-aq9001"}])
        out = _run("get_peak_analysis", c, host_id="srv-aq9001")
        assert c.sent("host.get")["filter"] == {"host": ["srv-aq9001"]}
        assert out.startswith("**Peak Analysis — srv-aq9001** (7d)")
        # Same hour-of-day profile as the fleet view, not a raw 48-hour dump.
        assert "| 02:00 | 100 | 150 | 1 |" in out and "| 14:00 | 300 | 400 | 1 |" in out
        when = datetime.fromtimestamp(_DAY0 + 14 * 3600, tz=timezone.utc).strftime("%Y-%m-%d %H:00")
        assert f"**Overall:** avg 200 Mbps | highest single reading 400 Mbps in the hour of {when} UTC" in out
        assert "_Trend data covers 2 of the 168 hours requested" in out

    def test_single_host_by_id_is_named(self):
        c = _client(trends=HOURLY)
        out = _run("get_peak_analysis", c, host_id="1")
        assert c.sent("host.get")["hostids"] == ["1"]
        assert out.startswith("**Peak Analysis — srv-aq9001** (7d)")

    def test_the_peak_is_the_highest_reading_not_the_highest_average(self):
        # A month of quiet hours and one burst: the old view printed the
        # largest hourly AVERAGE as the peak and missed the burst (ADR 153).
        quiet = [{"itemid": "t1", "clock": str(NOW - h * 3600 - NOW % 3600), "value_avg": str(20 * MBPS),
                  "value_max": str(40 * MBPS)} for h in range(1, 30 * 24)]
        burst = {"itemid": "t1", "clock": str(NOW - 500 * 3600 - NOW % 3600), "value_avg": str(70 * MBPS),
                 "value_max": str(980 * MBPS)}
        out = _run("get_peak_analysis", _client(trends=[*quiet, burst]), host_id="1", period="30d")
        assert "highest single reading 980 Mbps" in out
        assert "covers" not in out                     # a full window says nothing

    def test_weeks_and_bad_periods(self):
        c = _client(trends=HOURLY)
        _run("get_peak_analysis", c, host_id="1", period="2w")
        assert c.sent("trend.get")["limit"] == 1 * 24 * 14
        assert _run("get_peak_analysis", _client(trends=HOURLY), period="soon") == (
            "period must look like '7d' or '2w', got 'soon'.")

    def test_degraded_paths(self):
        assert _run("get_peak_analysis", _client(), host_id="srv-aq9099") == "Host 'srv-aq9099' not found."
        assert _run("get_peak_analysis", _client(), country="sj") == "No hosts match the filter."
        assert _run("get_peak_analysis", _client(traffic=[])) == "No traffic items found."
        assert _run("get_peak_analysis", _client(trends=[]), period="2d") == "No trend data for 2d."

    def test_api_error(self):
        assert _run("get_peak_analysis", _failing()) == "Error: unreachable"


# ------------------------------------------------------------ product audit


AUDIT_HOSTS = [
    _host("1", "srv-aq9001", "app_free", "192.0.2.1"),        # ACTIVE
    _host("2", "srv-aq9002", "app_free", "192.0.2.2"),        # IDLE
    _host("3", "srv-aq9003", "app_free", "192.0.2.3"),        # DEAD
    _host("4", "srv-aq9004", "app_free", "192.0.2.4"),        # service DOWN
    _host("5", "srv-aq9005", "app_free", "192.0.2.5"),        # DEGRADED (one of two checks)
    _host("6", "srv-aq9001 9006", "app_free", "192.0.2.6"),   # CLUSTER member of host 1
    _host("7", "srv-bv9007", "app_free"),                     # NO DATA, no IP
    _host("8", "srv-bv9008", "app_paid", "198.51.100.8"),     # other product
]
AUDIT_TRAFFIC = [_traffic("1", 50), _traffic("2", 3), _traffic("3", 0), _traffic("4", 1),
                 _traffic("5", 20), _traffic("8", 70)]
AUDIT_CPU = [{"hostid": h, "lastvalue": v, "lastclock": LIVE}
             for h, v in (("1", "70"), ("2", "90"), ("3", "99.5"), ("4", "90"), ("5", "90"))]
AUDIT_SVC = [_svc("1", 1), _svc("2", 1), _svc("4", 0), _svc("5", 1),
             _svc("5", 0, key=SVC2), _svc("3", 1, clock=STALE)]
DASHBOARDS = [{"dashboardid": "d1", "name": "Ops - Fleet", "pages": [
    {"name": "AQ", "widgets": [{"fields": [{"type": "3", "value": "1"}]}]}]}]


class TestProductAudit:
    @pytest.fixture(autouse=True)
    def _keys(self, monkeypatch):
        monkeypatch.setattr(fetch, "KEY_service_PRIMARY", SVC)
        monkeypatch.setattr(fetch, "KEY_service_SECONDARY", SVC2)
        monkeypatch.setattr(fetch, "KEY_service_TERTIARY", "")

    def _audit(self, product="app_free", **kw):
        c = _client(hosts=kw.pop("hosts", AUDIT_HOSTS), traffic=AUDIT_TRAFFIC, cpu=AUDIT_CPU,
                    service=AUDIT_SVC, dashboards=DASHBOARDS)
        return c, _run("get_product_audit", c, product=product)

    def test_categories(self):
        c, out = self._audit()
        assert c.sent("host.get")["output"] == ["hostid", "host", "status"]
        svc = next(p for m, p in c.calls if m == "item.get"
                   and (p.get("filter") or {}).get("key_") == [SVC, SVC2])
        assert svc["output"] == ["hostid", "lastvalue", "key_", "state", "lastclock"]
        assert out.startswith("**Product Audit: app_free** (7 servers)")
        # host 6 inherits its primary's 50 Mbps for display.
        assert "Traffic: 0.1 Gbps | Countries: 2" in out
        for cat in ("ACTIVE", "DEGRADED", "CLUSTER", "IDLE", "service DOWN", "DEAD", "NO DATA"):
            assert f"| {cat} | 1 |" in out
        assert "| srv-aq9001 | AQ | Fleet - AQ | 50.0 | 30.0% | OK |" in out
        assert "| srv-aq9005 | AQ | - | 20.0 | 10.0% | PARTIAL |" in out
        assert "| srv-aq9004 | AQ | - | 1.0 | 10.0% | DOWN |" in out
        assert "| srv-aq9001 9006 | AQ | - | 50.0 | 0% | - |" in out
        # A stale check reading is no evidence: host 3 renders "-", not OK.
        assert "| srv-aq9003 | AQ | - | 0.0 | 0.5% | - |" in out
        assert "| srv-bv9007 | BV | - | 0.0 | 0% | - |" in out
        assert "srv-bv9008" not in out
        assert out.index("**DEAD**") < out.index("**ACTIVE**")

    def test_infra_and_why_unclassified(self):
        hosts = [_host("1", "srv-aq9001", "Infrastructure", "192.0.2.1"),
                 _host("2", "srv-aq9002", "", "192.0.2.2")]
        _c, out = self._audit("Infrastructure", hosts=hosts)
        assert "| INFRA | 1 |" in out
        _c, out = self._audit("Unknown", hosts=hosts)
        assert "**Why unclassified** (1 hosts)" in out
        assert "| (no groups) | 1 |" in out

    def test_more_than_twelve_in_a_category(self):
        hosts = [_host(f"x{i}", f"srv-aq{9000 + i}", "app_free") for i in range(1, 15)]
        _c, out = self._audit(hosts=hosts)
        assert "**NO DATA** (14)" in out and "*+2 more*" in out

    def test_required_and_no_match(self):
        assert _run("get_product_audit", _client()) == "product is required."
        _c, out = self._audit("app_none")
        assert out == "No servers found for 'app_none'."

    def test_api_error(self):
        assert _run("get_product_audit", _failing(), product="app_free") == "Error: unreachable"
