"""Wire tests for the geo-level traffic tools (``tools/geo_traffic.py``).

Regional anomaly detection at daily and hourly grain, per-country trends,
the drop timeline and the expansion report. Fixture countries are
uninhabited-territory codes, which belong to no region: the expansion test
installs its own region map. Fixtures are synthetic.
"""

from __future__ import annotations

import ipaddress
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp import classify, country
from zbbx_mcp.tools import geo_traffic

NOW = int(time.time())
LIVE = str(NOW)
NIC = "net.if.in[eth0]"
CHECK = "primary_check.sh[{HOST.IP}]"
M = 1_000_000


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(classify, "_PRODUCT_MAP", {})
    monkeypatch.setattr(classify, "_EXTRA_PROVIDER_NETS",
                        [("Provider A", ipaddress.ip_network("198.51.100.0/24"))])
    monkeypatch.setattr(geo_traffic, "KEY_service_PRIMARY", "")   # off unless a test turns it on


def _day_key(days_ago):
    return datetime.fromtimestamp(NOW - days_ago * 86400, tz=timezone.utc).strftime("%Y-%m-%d")


def _daily(itemid, mbps_per_day):
    """One trend row per day, oldest first, the last one today."""
    n = len(mbps_per_day)
    return [{"itemid": itemid, "clock": str(NOW - (n - 1 - i) * 86400), "num": "24",
             "value_avg": str(v * M), "value_max": str(v * M), "value_min": str(v * M)}
            for i, v in enumerate(mbps_per_day)]


class Geo:
    def __init__(self):
        self.hosts: list[dict] = []
        self.items: list[dict] = []
        self.trends: dict[str, list[dict]] = {}

    def add(self, hid, name, *, ip=None, group="app_free", daily=None, now=None,
            rows=None, service=None):
        self.hosts.append({"hostid": hid, "host": name, "groups": [{"name": group}],
                           "interfaces": [{"ip": ip}] if ip else []})
        if daily is not None or rows is not None or now is not None:
            cur = now if now is not None else daily[-1]
            self.items.append({"itemid": f"t{hid}", "hostid": hid, "key_": NIC,
                               "lastvalue": str(cur * M), "lastclock": LIVE, "value_type": "3"})
            if daily is not None:
                self.trends[f"t{hid}"] = _daily(f"t{hid}", daily)
            elif rows is not None:
                self.trends[f"t{hid}"] = rows(f"t{hid}")
        if service is not None:
            self.items.append({"itemid": f"s{hid}", "hostid": hid, "key_": CHECK,
                               "lastvalue": str(service), "lastclock": LIVE, "state": "0"})
        return self

    def client(self, **overrides):
        def host_get(p):
            ids = p.get("hostids")
            return [dict(h) for h in self.hosts if not ids or h["hostid"] in ids]

        def item_get(p):
            keys = p.get("filter", {}).get("key_")
            keys = [keys] if isinstance(keys, str) else keys
            ids = p.get("hostids")
            return [it for it in self.items
                    if (not keys or it["key_"] in keys) and (not ids or it["hostid"] in ids)]

        def trend_get(p):
            return [r for iid in p["itemids"] for r in self.trends.get(iid, [])]

        return RecordingClient({"host.get": host_get, "item.get": item_get,
                                "trend.get": trend_get, **overrides})


def _daily_fleet():
    g = Geo()
    # Two AQ hosts fell from 100 to 10 Mbps for the last two days.
    g.add("1", "srv-aq9001", ip="192.0.2.1", daily=[100] * 5 + [10, 10])
    g.add("2", "srv-aq9002", ip="192.0.2.2", daily=[100] * 5 + [10, 10])
    g.add("3", "srv-bv9001", ip="198.51.100.1", daily=[50] * 7)
    g.add("4", "srv-bv9002", ip="198.51.100.2", daily=[50] * 7)
    g.add("5", "srv-hm9001", ip="203.0.113.1", daily=[50] * 7)      # alone: below min_servers
    g.add("6", "mon-9001")                                           # no country in the name
    return g


class TestDetectRegionalAnomaliesDaily:
    def test_a_country_wide_drop_is_critical(self):
        c = _daily_fleet().client()
        out = run_tool(geo_traffic, "detect_regional_anomalies", c)
        assert out.startswith("**Regional Anomaly Detection: 1 countries affected**\n"), out
        assert "| AQ | 2 | 2/2 (100%) | -90% | 0 | CRITICAL |" in out
        assert "\n### AQ — CRITICAL\n- srv-aq9001: 10.0 Mbps (was 74.3) | service: ?\n" \
               "- srv-aq9002: 10.0 Mbps (was 74.3) | service: ?" in out
        assert out.endswith("**Healthy countries:** BV")
        # The baseline window drives the trend fetch (measured from the call, not import).
        assert 0 <= (int(time.time()) - 7 * 86400) - c.sent("trend.get")["time_from"] < 300
        assert not any(m == "item.get" and p.get("filter", {}).get("key_") == CHECK for m, p in c.calls)

    def test_a_host_whose_service_is_down_is_not_a_traffic_block(self, monkeypatch):
        monkeypatch.setattr(geo_traffic, "KEY_service_PRIMARY", CHECK)
        g = _daily_fleet()
        g.add("1", "srv-aq9001", service=1)      # the same host id: adds its check item
        g.add("2", "srv-aq9002", service=0)
        g.hosts = g.hosts[:6]                    # the two extra host rows were only for the items
        c = g.client()
        out = run_tool(geo_traffic, "detect_regional_anomalies", c)
        assert "| AQ | 2 | 1/2 (50%) | -90% | 1 | WARNING |" in out, out
        assert "- srv-aq9001: 10.0 Mbps (was 74.3) | service: OK" in out
        assert "- srv-aq9002: 10.0 Mbps (was 74.3) | service: DOWN" in out
        svc = next(p for m, p in c.calls if m == "item.get" and p.get("filter", {}).get("key_") == CHECK)
        assert "lastclock" in svc["output"] and svc["filter"]["status"] == "0"
        # Raising the country threshold above the affected share clears it.
        assert run_tool(geo_traffic, "detect_regional_anomalies", g.client(), country_threshold=60) == \
            "No regional anomalies detected across 2 countries (4 servers)."

    def test_not_enough_servers_or_wrong_product(self):
        g = _daily_fleet()
        assert run_tool(geo_traffic, "detect_regional_anomalies", g.client(),
                        min_servers=3) == "No countries with enough servers for analysis."
        assert run_tool(geo_traffic, "detect_regional_anomalies", g.client(),
                        product="app_paid") == "No countries with enough servers for analysis."
        quiet = Geo().add("1", "srv-aq9001", daily=[0.5] * 7).add("2", "srv-aq9002", daily=[0.5] * 7)
        # A micro-market never alerts, whatever its percentages do.
        assert run_tool(geo_traffic, "detect_regional_anomalies", quiet.client()) == \
            "No regional anomalies detected across 1 countries (2 servers)."


# Frozen mid-hour so the recent/baseline split never sits on a bucket edge.
FROZEN = (NOW // 3600) * 3600 + 1800
BASE = FROZEN - 1800


def _hourly(values_for):
    """168 hourly rows, newest first at the current hour; ``values_for(i)`` gives Mbps for i hours ago."""
    def rows(itemid):
        return [{"itemid": itemid, "clock": str(BASE - i * 3600), "value_avg": str(values_for(i) * M)}
                for i in range(168)]
    return rows


class TestDetectRegionalAnomaliesAcute:
    @pytest.fixture(autouse=True)
    def _freeze(self, monkeypatch):
        monkeypatch.setattr(geo_traffic, "_time", SimpleNamespace(time=lambda: FROZEN))

    def _fleet(self):
        g = Geo()
        # AQ: 100 Mbps per host all week, 5 Mbps for the last six hours.
        g.add("1", "srv-aq9001", now=5, rows=_hourly(lambda i: 5 if i < 6 else 100))
        g.add("2", "srv-aq9002", now=5, rows=_hourly(lambda i: 5 if i < 6 else 100))
        g.add("3", "srv-bv9001", now=50, rows=_hourly(lambda i: 50))
        g.add("4", "srv-bv9002", now=50, rows=_hourly(lambda i: 50))
        return g

    def test_six_anomalous_hours_is_sustained(self):
        c = self._fleet().client()
        out = run_tool(geo_traffic, "detect_regional_anomalies", c, acute=True)
        assert out.startswith("**Acute regional anomalies: 1 countr(y/ies)** "
                              "(country-aggregate hourly vs same-hour seasonal band)\n"), out
        assert out.endswith("| AQ | SUSTAINED | 89% | 10.0 → 200.0 Mbps | **95%** | "
                            "anomalous for 6 consecutive buckets — sustained |"), out
        sent = c.sent("trend.get")
        assert sent["time_from"] == FROZEN - 7 * 86400
        assert sent["limit"] == 4 * 7 * 24
        assert sent["output"] == ["itemid", "clock", "value_avg"]

    def test_a_one_hour_window_is_acute_not_sustained(self):
        out = run_tool(geo_traffic, "detect_regional_anomalies", self._fleet().client(),
                       acute=True, recent_hours=1)
        assert out.endswith("| AQ | ACUTE | 74% | 10.0 → 194.3 Mbps | **95%** | "
                            "anomalous on the current bucket (1 consecutive) — immediate |"), out

    def test_quiet_fleet_and_no_traffic_items(self):
        g = Geo()
        for i in (1, 2):
            g.add(str(i), f"srv-aq900{i}", now=100, rows=_hourly(lambda i: 100))
        assert run_tool(geo_traffic, "detect_regional_anomalies", g.client(), acute=True) == \
            "No acute regional anomalies (analyzed 1 countries; country-aggregate hourly traffic within the seasonal band)."
        bare = Geo().add("1", "srv-aq9001").add("2", "srv-aq9002")
        assert run_tool(geo_traffic, "detect_regional_anomalies", bare.client(), acute=True) == \
            "No traffic items found for acute analysis."


def _trend_fleet():
    g = Geo()
    ramp = [100, 100, 150, 150, 200, 200, 300, 300]
    g.add("1", "srv-aq9001", daily=ramp)                    # growing market
    g.add("2", "srv-aq9002", daily=ramp)
    g.add("3", "srv-bv9001", daily=[100] * 8)               # flat
    g.add("4", "srv-bv9002", daily=[100] * 8)
    g.add("5", "srv-hm9001", daily=[100] * 8, now=0)        # carried traffic, now silent
    g.add("6", "srv-hm9002", daily=[100] * 8, now=0)
    g.add("7", "srv-tf9001", daily=[10] * 8)                # too small to list
    g.add("8", "srv-tf9002", daily=[10] * 8)
    g.add("9", "srv-gs9001", daily=[500] * 8)               # alone: excluded
    return g


class TestGeoTrafficTrends:
    def test_country_rows_and_daily_breakdown(self):
        out = run_tool(geo_traffic, "get_geo_traffic_trends", _trend_fleet().client())
        assert out.startswith("**Geo Traffic Trends (30d): 3 countries**\n"), out
        rows = [ln for ln in out.splitlines() if ln.startswith("| ") and "Country" not in ln
                and "---" not in ln and "Gbps)" not in ln]
        assert rows == [
            "| AQ | 2 | 0.4 Gbps | 0.6 Gbps | rising | +60% |",
            "| BV | 2 | 0.2 Gbps | 0.2 Gbps | stable | +0% |",
            "| HM | 2 | 0.2 Gbps | 0.0 Gbps | dead | -100% |",
        ]
        assert "*1 countries with <0.1 Gbps omitted*" in out
        days = " | ".join(_day_key(7 - i) for i in range(8))
        assert f"### Daily Breakdown (top countries)\n\n| Country | {days} |" in out, out
        assert "| AQ (Gbps) | 0.2 | 0.2 | 0.3 | 0.3 | 0.4 | 0.4 | 0.6 | 0.6 |" in out
        assert "| TF (Gbps) | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 |" in out

    def test_summary_only_regions_and_no_data(self):
        g = _trend_fleet()
        out = run_tool(geo_traffic, "get_geo_traffic_trends", g.client(), aggregation="summary")
        assert "Daily Breakdown" not in out and "| AQ | 2 |" in out
        assert run_tool(geo_traffic, "get_geo_traffic_trends", g.client(),
                        region="XX") == "Unknown region 'XX'. Use: LATAM, APAC, EMEA, NA, CIS, ALL."
        # The fixture codes belong to no real region.
        assert run_tool(geo_traffic, "get_geo_traffic_trends", g.client(),
                        region="EMEA") == "No countries with enough servers."
        assert run_tool(geo_traffic, "get_geo_traffic_trends", g.client(**{"trend.get": []}),
                        period="30d") == "No traffic trend data for 30d."


class TestTrafficDropTimeline:
    def test_the_drop_day_and_duration(self):
        g = Geo()
        g.add("1", "srv-aq9001", daily=[100] * 5 + [10] * 5)
        g.add("2", "srv-aq9002", daily=[100] * 5 + [10] * 5)
        g.add("3", "srv-bv9001", daily=[100] * 10)
        g.add("4", "srv-bv9002", daily=[100] * 10)
        g.add("5", "srv-hm9001", daily=[2] * 10)              # never carried enough to drop
        g.add("6", "srv-hm9002", daily=[2] * 10)
        out = run_tool(geo_traffic, "get_traffic_drop_timeline", g.client())
        label = datetime.strptime(_day_key(4), "%Y-%m-%d").strftime("%b %d")
        assert out == (
            "**Traffic Drop Timeline (30d)**\n\n"
            "| Country | Servers | Drop Started | Duration | Pre-drop Traffic | Current |\n"
            "|---------|---------|--------------|----------|-------------------|---------|\n"
            f"| AQ | 2 | {label} | 5d | 0.2 Gbps | 0.02 Gbps |"
        ), out

    def test_no_drops_and_no_countries(self):
        g = Geo().add("3", "srv-bv9001", daily=[100] * 10).add("4", "srv-bv9002", daily=[100] * 10)
        assert run_tool(geo_traffic, "get_traffic_drop_timeline", g.client()) == \
            "No traffic drops detected in 30d across 1 countries."
        assert run_tool(geo_traffic, "get_traffic_drop_timeline", Geo().client()) == \
            "No countries with enough servers."


class TestExpansionReport:
    @pytest.fixture(autouse=True)
    def _region(self, monkeypatch):
        monkeypatch.setattr(country, "REGION_MAP", {"POLAR": ["AQ", "BV", "HM", "TF"]})

    def _fleet(self):
        g = Geo()
        g.add("1", "srv-aq9001", ip="192.0.2.1", now=3500)
        g.add("2", "srv-aq9002", ip="198.51.100.1", now=3500)      # a second provider
        g.add("3", "srv-bv9001", ip="198.51.100.2", now=100)
        g.add("4", "srv-hm9001", ip="203.0.113.1", now=20)
        g.add("5", "srv-test-tf9001", ip="203.0.113.2", now=900)   # staging box: excluded
        g.add("6", "mon-9001", ip="203.0.113.3", now=50)
        return g

    def test_density_status_and_gaps(self):
        c = self._fleet().client()
        out = run_tool(geo_traffic, "get_expansion_report", c, region="polar")
        assert out.startswith("**Expansion Report — polar** (3 countries with servers)\n"), out
        rows = [ln for ln in out.splitlines() if ln.startswith("| ") and "Country" not in ln and "---" not in ln]
        assert rows == [
            "| AQ | 2 | 7.0 | 3500.0 | 2 | OVERLOADED |",
            "| BV | 1 | 0.1 | 100.0 | 1 | OK |",
            "| HM | 1 | 0.02 | 20.0 | 1 | LOW |",
        ]
        assert "\n**Needs more servers:** AQ\n" in out
        assert out.endswith("**No servers in region:** TF")
        assert c.sent("host.get")["filter"] == {"status": "0"}

    def test_floor_cap_and_unknown_region(self):
        g = self._fleet()
        out = run_tool(geo_traffic, "get_expansion_report", g.client(), min_traffic_mbps=50)
        assert "(2 countries with servers)" in out and "| HM |" not in out, out
        out = run_tool(geo_traffic, "get_expansion_report", g.client(), max_results=1)
        assert "*2 more countries omitted*" in out and "| BV |" not in out, out
        assert run_tool(geo_traffic, "get_expansion_report", g.client(),
                        region="XX") == "Unknown region 'XX'. Use: LATAM, APAC, EMEA, NA, CIS, ALL."
