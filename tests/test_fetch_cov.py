"""The shared fetch layer (fetch.py) under a recording client.

Complements test_live_values.py (the live/stale gate), test_test_hosts.py
(exclude_test) and test_template_fallback.py (template classification): this
file drives the fetchers end to end — the API params they send and the maps
and rows they build — with no network. Fixtures follow ADR 119/127:
documentation addresses, reserved-band host names.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from tests.wiretest import RecordingClient
from zbbx_mcp import classify as classify_mod
from zbbx_mcp import fetch
from zbbx_mcp.country import INVENTORY_COUNTRY_FIELDS
from zbbx_mcp.data import (
    GB_BYTES,
    KEY_AGENT_VERSION,
    KEY_CPU_IDLE,
    KEY_CPU_LOAD,
    KEY_MEM_AVAIL,
    STATUS_ENABLED,
    TRAFFIC_IN_KEYS,
    TRAFFIC_OUT_KEYS,
)
from zbbx_mcp.fetch import (
    fetch_all_data,
    fetch_cpu_map,
    fetch_enabled_hosts,
    fetch_host_dashboards,
    fetch_service_status,
    fetch_traffic_map,
    fetch_trends_batch,
    rank_by_last_value,
)

NOW = int(time.time())
LIVE = str(NOW - 60)
STALE = str(NOW - 7200)
# A fixed noon, so hourly trend clocks around it share one daily key.
DAY = int(datetime(2026, 3, 10, 12, tzinfo=timezone.utc).timestamp())


@pytest.fixture(autouse=True)
def _classify_by_group_name(monkeypatch):
    """Group name = product (no product map), whatever another test cached."""
    monkeypatch.setattr(classify_mod, "_PRODUCT_MAP", {})
    monkeypatch.setattr(classify_mod, "_TEMPLATE_PRODUCT_MAP", {})
    monkeypatch.delenv("ZABBIX_PRODUCT_MAP", raising=False)
    monkeypatch.delenv("ZABBIX_TEMPLATE_PRODUCT_MAP", raising=False)


class CachingClient(RecordingClient):
    """RecordingClient with a real cache, for the cache-hit paths."""

    def __init__(self, responses=None):
        super().__init__(responses)
        self.cache: dict = {}

    def _get_cached(self, key, ttl=0.0):
        return self.cache.get(key)

    def _set_cache(self, key, value):
        self.cache[key] = value


def _host(hid: str, name: str, group: str = "app_free", groups=None) -> dict:
    return {
        "hostid": hid, "host": name, "name": name, "status": STATUS_ENABLED,
        "groups": [{"name": group}] if groups is None else groups,
        "interfaces": [{"ip": "127.0.0.1"}, {"ip": f"192.0.2.{int(hid) % 250 + 1}"}],
    }


def _item(hid: str, key: str, value, clock: str = LIVE, **extra) -> dict:
    return {"itemid": f"{hid}:{key}", "hostid": hid, "key_": key,
            "lastvalue": str(value), "lastclock": clock, **extra}


def _trend(itemid: str, k: int, avg, vmin=None, vmax=None, base: int = DAY) -> dict:
    return {"itemid": itemid, "clock": str(base + 3600 * k), "num": "60",
            "value_min": str(avg if vmin is None else vmin),
            "value_avg": str(avg),
            "value_max": str(avg if vmax is None else vmax)}


# --- fetch_enabled_hosts ---------------------------------------------------------


class TestFetchEnabledHosts:
    async def test_cache_hit_skips_the_api(self):
        c = CachingClient({"host.get": [_host("9001", "srv-aq9001")]})
        first = await fetch_enabled_hosts(c)
        second = await fetch_enabled_hosts(c)
        assert second == first
        assert len(c.calls) == 1

    async def test_inventory_requests_country_fields_and_bypasses_the_cache(self):
        c = CachingClient({"host.get": [_host("9001", "srv-aq9001")]})
        await fetch_enabled_hosts(c, inventory=True)
        await fetch_enabled_hosts(c, inventory=True)
        assert len(c.calls) == 2
        assert c.sent("host.get")["selectInventory"] == list(INVENTORY_COUNTRY_FIELDS)
        assert c.cache == {}

    async def test_extra_output_extends_output_and_bypasses_the_cache(self):
        c = CachingClient({"host.get": [_host("9001", "srv-aq9001")]})
        await fetch_enabled_hosts(c, extra_output=["name", "status"])
        assert c.sent("host.get")["output"] == ["hostid", "host", "name", "status"]
        assert c.cache == {}

    async def test_default_params_and_switches(self):
        c = RecordingClient({"host.get": []})
        await fetch_enabled_hosts(c, groups=False, interfaces=False)
        p = c.sent("host.get")
        assert p["filter"] == {"status": STATUS_ENABLED}
        assert p["sortfield"] == "host"
        assert "selectGroups" not in p and "selectInterfaces" not in p
        c = RecordingClient({"host.get": []})
        await fetch_enabled_hosts(c)
        p = c.sent("host.get")
        assert p["selectGroups"] == ["name"] and p["selectInterfaces"] == ["ip"]

    async def test_non_list_reply_is_an_empty_fleet(self):
        c = RecordingClient({"host.get": {"unexpected": True}})
        assert await fetch_enabled_hosts(c) == []


# --- fetch_traffic_map ---------------------------------------------------------------


class TestFetchTrafficMap:
    async def test_no_hosts_makes_no_call(self):
        c = RecordingClient()
        assert await fetch_traffic_map(c, []) == {}
        assert c.calls == []

    async def test_busiest_physical_nic_wins_per_host(self):
        c = RecordingClient({"item.get": [
            _item("9001", "net.if.in[eth0]", 50_000_000),
            _item("9001", "net.if.in[eth1]", 125_000_000),
            _item("9001", "net.if.in[docker0]", 999_000_000),     # virtual: ignored
            _item("9002", "net.if.in[eth0]", 80_000_000, clock="0"),  # never collected
            _item("9003", "net.if.in[eth0]", 80_000_000, clock=STALE),
            {"key_": "net.if.in[eth0]", "lastvalue": "1000000", "lastclock": LIVE},  # no hostid
        ]})
        out = await fetch_traffic_map(c, ["9001", "9002", "9003"])
        assert out == {"9001": 125.0}
        p = c.sent("item.get")
        assert p["tags"] == [{"tag": "Application", "value": "Network interfaces", "operator": "1"}]
        assert p["search"] == {"key_": fetch.TRAFFIC_IN_KEY_SEARCH}
        assert p["searchWildcardsEnabled"] is True
        assert "lastclock" in p["output"]

    async def test_falls_back_to_the_key_list_when_the_tag_search_is_empty(self):
        def item_get(p):
            if "tags" in p:
                return []
            return [_item("9001", "net.if.in[eth0]", 8_000_000)]

        c = RecordingClient({"item.get": item_get})
        assert await fetch_traffic_map(c, ["9001"]) == {"9001": 8.0}
        assert len(c.calls) == 2
        assert c.calls[1][1]["filter"] == {"key_": TRAFFIC_IN_KEYS}

    async def test_falls_back_when_the_tag_search_raises(self):
        def item_get(p):
            if "tags" in p:
                raise ValueError("tags unsupported")
            return [_item("9001", "net.if.in[eth0]", 8_000_000)]

        c = RecordingClient({"item.get": item_get})
        assert await fetch_traffic_map(c, ["9001"]) == {"9001": 8.0}


# --- fetch_host_dashboards -------------------------------------------------------


class TestFetchHostDashboards:
    DASHBOARDS = [
        {"dashboardid": "1", "name": "Fleet - Edge", "pages": [
            {"name": "Overview", "widgets": [{"fields": [
                {"type": "3", "value": "9001"}, {"type": "6", "value": "g1"}]}]},
            {"name": " ", "widgets": [{"fields": [
                {"type": "3", "value": "9002"}, {"type": "3", "value": "9001"}]}]},
        ]},
        {"dashboardid": "2", "name": "Plain", "pages": [
            {"name": "P", "widgets": [{"fields": [{"type": "3", "value": "9002"}]}]},
        ]},
    ]

    async def test_prefix_is_stripped_and_the_first_reference_wins(self):
        c = RecordingClient({"dashboard.get": self.DASHBOARDS})
        out = await fetch_host_dashboards(c)
        assert out == {"9001": "Edge - Overview", "9002": "Edge"}
        assert c.sent("dashboard.get") == {"output": ["dashboardid", "name"], "selectPages": "extend"}

    async def test_no_dashboards_is_empty(self):
        assert await fetch_host_dashboards(RecordingClient({"dashboard.get": []})) == {}

    async def test_dashboard_without_pages_or_name_is_tolerated(self):
        c = RecordingClient({"dashboard.get": [{"dashboardid": "3"}]})
        assert await fetch_host_dashboards(c) == {}


# --- fetch_service_status ------------------------------------------------------------


@pytest.fixture
def svc_keys(monkeypatch):
    monkeypatch.setattr(fetch, "KEY_service_PRIMARY", "svc.primary")
    monkeypatch.setattr(fetch, "KEY_service_SECONDARY", "svc.secondary")
    monkeypatch.setattr(fetch, "KEY_service_TERTIARY", "")


class TestFetchServiceStatus:
    async def test_no_hosts_makes_no_call(self, svc_keys):
        c = RecordingClient()
        assert await fetch_service_status(c, []) == {}
        assert c.calls == []

    async def test_no_keys_configured_makes_no_call(self, monkeypatch):
        for name in ("KEY_service_PRIMARY", "KEY_service_SECONDARY", "KEY_service_TERTIARY"):
            monkeypatch.setattr(fetch, name, "")
        c = RecordingClient()
        assert await fetch_service_status(c, ["9001"]) == {}
        assert c.calls == []

    async def test_ok_partial_down_and_missing(self, svc_keys):
        c = RecordingClient({"item.get": [
            _item("9001", "svc.primary", 1, state="0"),
            _item("9001", "svc.secondary", 1, state="0"),          # all OK
            _item("9002", "svc.primary", 1, state="0"),
            _item("9002", "svc.secondary", 0, state="0"),          # partial
            _item("9003", "svc.primary", 0, state="0"),            # down
            _item("9004", "svc.primary", 0, state="1"),            # unsupported: no verdict
            _item("9005", "svc.primary", 0, state="0", clock=STALE),  # stale: no verdict
        ]})
        out = await fetch_service_status(c, ["9001", "9002", "9003", "9004", "9005"])
        assert out == {"9001": 1, "9002": -1, "9003": 0}
        p = c.sent("item.get")
        assert p["filter"] == {"key_": ["svc.primary", "svc.secondary"], "status": STATUS_ENABLED}
        assert {"state", "lastclock", "lastvalue", "hostid", "key_"} <= set(p["output"])


# --- small pure helpers ------------------------------------------------------------


class TestRankAndCpuMap:
    def test_rank_by_last_value_treats_garbage_as_zero_and_keeps_order(self):
        items = [{"hostid": "a", "lastvalue": "x"}, {"hostid": "b", "lastvalue": "5"}, {"hostid": "c"}]
        assert [i["hostid"] for i in rank_by_last_value(items)] == ["b", "a", "c"]
        assert rank_by_last_value(None) == []

    async def test_fetch_cpu_map_with_no_hosts_makes_no_call(self):
        c = RecordingClient()
        assert await fetch_cpu_map(c, []) == {}
        assert c.calls == []


# --- fetch_all_data ------------------------------------------------------------------


@pytest.fixture
def all_keys(monkeypatch):
    monkeypatch.setattr(fetch, "KEY_CONNECTIONS", "conn.count")
    monkeypatch.setattr(fetch, "KEY_service_PRIMARY", "svc.primary")
    monkeypatch.setattr(fetch, "KEY_service_SECONDARY", "svc.secondary")
    monkeypatch.setattr(fetch, "KEY_service_TERTIARY", "svc.tertiary")


HOSTS = [
    _host("9001", "srv-aq9001"),                       # on the dashboard, full metrics
    _host("9002", "srv-bv9002", group="app_paid"),     # traffic only via the fallback
    _host("9003", "srv-aq9001 aq9003"),                # child of 9001: inherits its metrics
    _host("9004", "srv-hm9004", groups=[]),            # no product group: not a row
]

DASHBOARDS = [{"dashboardid": "d1", "name": "Fleet", "pages": [
    {"name": "", "widgets": [{"fields": [{"type": "6", "value": "g1"}, {"type": "3", "value": "9001"}]}]},
]}]


def _rich_client(hosts=HOSTS) -> RecordingClient:
    def host_get(p):
        if "selectParentTemplates" in p:
            return [{"hostid": "9001", "parentTemplates": [{"name": f"T{i}"} for i in range(1, 5)]},
                    {"hostid": "9002", "parentTemplates": []}]
        return hosts

    def item_get(p):
        key = (p.get("filter") or {}).get("key_")
        search = p.get("search") or {}
        if key == KEY_CPU_IDLE:
            return [_item("9001", key, 60)]
        if key == KEY_CPU_LOAD:
            return [_item("9001", key, 1.5)]
        if key == KEY_MEM_AVAIL:
            return [_item("9001", key, 2 * GB_BYTES)]
        if key == "conn.count":
            return [_item("9001", key, 120)]
        if key == TRAFFIC_IN_KEYS:
            return [_item("9001", "net.if.in[eth0]", 100_000_000),
                    _item("9001", "net.if.in[eth1]", 50_000_000)]
        if key == TRAFFIC_OUT_KEYS:
            return [_item("9001", "net.if.out[eth0]", 20_000_000)]
        if key == KEY_AGENT_VERSION:
            return [_item("9001", key, "7.0.1")]
        if key == "svc.primary":
            return [_item("9001", key, 1, state="0"), _item("9002", key, 0, state="1")]
        if key == "svc.secondary":
            return [_item("9001", key, 0, state="0")]
        if key == "svc.tertiary":
            return [_item("9001", key, 0, state="0"), _item("9001", key, 1, state="0")]
        if search.get("key_") == fetch.TRAFFIC_IN_KEY_SEARCH:
            return [_item("9002", 'net.if.in["ens18"]', 40_000_000)]
        if search.get("name"):
            return [_item("9002", 'net.if.out["ens18"]', 10_000_000)]
        raise AssertionError(f"unexpected item.get params: {p}")

    return RecordingClient({
        "host.get": host_get,
        "item.get": item_get,
        "dashboard.get": DASHBOARDS,
        "graph.get": [{"graphid": "g1", "hosts": [{"hostid": "9001"}]}],
        "usermacro.get": [
            {"hostid": "9001", "macro": "{$COST_MONTH}", "value": "50"},
            {"hostid": "9001", "macro": "{$BW_LIMIT}", "value": "1000"},
            {"hostid": "9002", "macro": "{$COST_MONTH}", "value": "n/a"},
            {"hostid": "9002", "macro": "{$OTHER}", "value": "1"},
        ],
    })


class TestFetchAllData:
    async def test_rows_carry_metrics_dashboards_and_derived_fields(self, all_keys):
        c = _rich_client()
        res = await fetch_all_data(c)
        by = {r["Host"]: r for r in res.rows}
        # Dashboard rows first, then product / tier / host.
        assert list(by) == ["srv-aq9001", "srv-aq9001 aq9003", "srv-bv9002"]
        assert "srv-hm9004" not in by
        assert (res.total_on_dashboard, res.total_off_dashboard) == (1, 2)

        r = by["srv-aq9001"]
        assert r["CPU %"] == 40.0 and r["Load Avg5"] == 1.5 and r["Mem Avail GB"] == 2.0
        assert r["Connections"] == 120.0
        assert (r["Traffic In Mbps"], r["Traffic Out Mbps"], r["Traffic Total Mbps"]) == (100.0, 20.0, 120.0)
        assert r["BW Util %"] == 10.0                     # against the {$BW_LIMIT} macro
        assert r["BW Tier"] == "LOW"
        assert r["Agent"] == "7.0.1"
        assert r["Templates"] == "T1, T2, T3 (+1)"
        assert (r["Cost/Month ($)"], r["Cost/Year ($)"]) == (50.0, 600.0)
        assert (r["service Primary"], r["service Secondary"], r["service Tertiary"]) == ("OK", "DOWN", "OK")
        assert (r["Dashboard"], r["Dashboard ID"], r["Tab"], r["Page Index"]) == ("Fleet", "d1", "Page 1", 0)
        assert r["All Tabs"] == "Fleet / Page 1" and r["On Dashboard"] == "Yes"
        assert (r["Country"], r["Product"], r["Tier"]) == ("AQ", "app_free", "Default")
        assert r["IP"] == "192.0.2.2"                      # loopback interface skipped
        assert r["Groups"] == "app_free"
        assert res.tab_data == {"Fleet||Page 1": [r]}

        child = by["srv-aq9001 aq9003"]
        assert child["CPU %"] == 40.0 and child["Traffic In Mbps"] == 100.0
        assert child["Templates"] == "T1, T2, T3 (+1)"
        assert child["On Dashboard"] == "No"

        fb = by["srv-bv9002"]
        assert (fb["Traffic In Mbps"], fb["Traffic Out Mbps"]) == (40.0, 10.0)
        assert fb["BW Util %"] == 5.0                     # no macro: the NIC ceiling applies
        assert fb["Cost/Month ($)"] is None               # "n/a" is not a cost
        assert fb["service Primary"] == ""                # an unsupported check is no verdict
        assert fb["Country"] == "BV" and fb["CPU %"] is None

    async def test_wire_shape_of_the_batch(self, all_keys):
        c = _rich_client()
        await fetch_all_data(c)
        assert "dashboardids" not in c.sent("dashboard.get")
        hosts_call = next(p for m, p in c.calls if m == "host.get" and "filter" in p)
        assert hosts_call["selectGroups"] == ["name"] and hosts_call["selectInterfaces"] == ["ip"]
        assert c.sent("graph.get") == {"graphids": ["g1"], "output": ["graphid"], "selectHosts": ["hostid"]}
        assert c.sent("usermacro.get")["filter"] == {"macro": ["{$COST_MONTH}", "{$BW_LIMIT}"]}
        item_calls = [p for m, p in c.calls if m == "item.get"]
        assert all("lastclock" in p["output"] for p in item_calls)
        fallback = next(p for p in item_calls if (p.get("search") or {}).get("key_"))
        # Only the hosts the fast key filter missed go to the fallback.
        assert sorted(fallback["hostids"]) == ["9002", "9003", "9004"]

    async def test_dashboard_filter_and_off_dashboard_exclusion(self, all_keys):
        c = _rich_client()
        res = await fetch_all_data(c, include_off_dashboard=False, dashboard_id="d1")
        assert c.sent("dashboard.get")["dashboardids"] == ["d1"]
        assert [r["Host"] for r in res.rows] == ["srv-aq9001"]
        assert res.total_off_dashboard == 2               # counted, not rendered

    async def test_cached_hosts_skip_the_host_query(self, all_keys):
        c = CachingClient(_rich_client()._responses)
        c.cache["all_enabled_hosts"] = [_host("9001", "srv-aq9001")]
        res = await fetch_all_data(c)
        assert [r["Host"] for r in res.rows] == ["srv-aq9001"]
        assert not any(m == "host.get" and "filter" in p for m, p in c.calls)

    async def test_failed_calls_degrade_to_unknown_not_a_crash(self):
        def boom(p):
            raise ValueError("api down")

        c = RecordingClient({
            "host.get": lambda p: [] if "selectParentTemplates" in p else [_host("9001", "srv-aq9001")],
            "dashboard.get": [], "item.get": boom, "usermacro.get": boom,
        })
        res = await fetch_all_data(c)
        assert len(res.rows) == 1
        r = res.rows[0]
        assert r["CPU %"] is None and r["Traffic In Mbps"] is None and r["BW Tier"] == ""
        assert r["Cost/Year ($)"] is None and r["Agent"] == "" and r["Templates"] == ""
        assert r["service Primary"] == "" and r["On Dashboard"] == "No"
        assert not any(m == "graph.get" for m, _ in c.calls)

    async def test_non_list_host_reply_yields_no_rows(self):
        c = RecordingClient({"host.get": {}, "dashboard.get": []})
        res = await fetch_all_data(c)
        assert res.rows == [] and res.tab_data == {}
        assert (res.total_on_dashboard, res.total_off_dashboard) == (0, 0)


# --- fetch_trends_batch ----------------------------------------------------------------


class TestFetchTrendsBatch:
    async def test_default_metrics_and_no_items_means_no_trend_query(self):
        c = RecordingClient({"host.get": [{"hostid": "9001", "host": "srv-aq9001"}], "item.get": []})
        rows, hosts = await fetch_trends_batch(c, ["9001"])
        keys = c.sent("item.get")["filter"]["key_"]
        assert KEY_CPU_IDLE in keys and KEY_CPU_LOAD in keys and TRAFFIC_IN_KEYS[0] in keys
        assert KEY_MEM_AVAIL not in keys
        assert rows == [] and hosts == {"9001": {"hostid": "9001", "host": "srv-aq9001"}}
        assert not any(m == "trend.get" for m, _ in c.calls)

    async def test_unknown_metric_makes_no_call(self):
        c = RecordingClient()
        assert await fetch_trends_batch(c, ["9001"], ["nope"]) == ([], {})
        assert c.calls == []

    async def test_busiest_interface_is_chosen_for_traffic(self):
        c = RecordingClient({
            "host.get": [{"hostid": "9001", "host": "srv-aq9001"}],
            "item.get": [
                _item("9001", "net.if.in[eth0]", 1_000_000, itemid="i-eth0"),
                _item("9001", "net.if.in[eth1]", 5_000_000, itemid="i-eth1"),
                _item("9001", "net.if.in[bond0]", "x", itemid="i-bond"),
                _item("9001", "vfs.dev.read.rate[sda]", 3, itemid="i-off"),   # not a wanted metric
            ],
            "trend.get": lambda p: [_trend(i, k, 2_000_000) for i in p["itemids"] for k in range(2)],
        })
        before = int(time.time())
        rows, _ = await fetch_trends_batch(c, ["9001"], ["traffic"], "1d")
        after = int(time.time())
        p = c.sent("trend.get")
        assert p["itemids"] == ["i-eth1"]
        assert p["limit"] == 24 * 30
        # Bracketed by clocks read around the call: a slow runner must not fail this.
        assert before - 86400 <= p["time_from"] <= after - 86400
        assert len(rows) == 1
        assert (rows[0].current, rows[0].avg, rows[0].trend_dir) == (5.0, 2.0, "")

    async def test_memory_is_reported_in_gb_with_a_daily_breakdown(self):
        c = RecordingClient({
            "host.get": [{"hostid": "9001", "host": "srv-aq9001"}],
            "item.get": [_item("9001", KEY_MEM_AVAIL, 4 * GB_BYTES, itemid="i-mem")],
            "trend.get": [_trend("i-mem", k, v * GB_BYTES) for k, v in enumerate((2, 2, 3, 3))],
        })
        rows, _ = await fetch_trends_batch(c, ["9001"], ["memory"], "7d")
        r = rows[0]
        assert (r.metric, r.current, r.avg, r.peak, r.min_val) == ("memory", 4.0, 2.5, 3.0, 2.0)
        assert r.trend_dir == "rising"
        assert r.daily == {"2026-03-10": 2.5}

    @pytest.mark.parametrize(("idle", "expected"), [
        ((80, 80, 40, 40), "rising"),      # used 20% -> 60%
        ((40, 40, 80, 80), "dropping"),    # used 60% -> 20%
        ((50, 50, 52, 52), "stable"),
    ])
    async def test_cpu_direction_is_inverted_from_idle(self, idle, expected):
        c = RecordingClient({
            "host.get": [{"hostid": "9001", "host": "srv-aq9001"}],
            "item.get": [_item("9001", KEY_CPU_IDLE, 70, itemid="i-cpu")],
            "trend.get": [_trend("i-cpu", k, v, vmin=v - 5, vmax=v + 5) for k, v in enumerate(idle)],
        })
        rows, _ = await fetch_trends_batch(c, ["9001"], ["cpu"], "1d")
        r = rows[0]
        assert r.trend_dir == expected
        assert r.current == 30.0
        assert r.peak == 100 - (min(idle) - 5)      # least idle = busiest hour
        assert r.min_val == 100 - (max(idle) + 5)   # most idle = quietest hour
        assert r.daily == {"2026-03-10": round(100 - sum(idle) / len(idle), 1)}

    async def test_item_without_trend_rows_is_dropped(self):
        c = RecordingClient({
            "host.get": [{"hostid": "9001", "host": "srv-aq9001"}],
            "item.get": [_item("9001", KEY_CPU_IDLE, 70, itemid="i-cpu"),
                         _item("9001", KEY_CPU_LOAD, 1.0, itemid="i-load")],
            "trend.get": [_trend("i-load", k, 1.25) for k in range(2)],
        })
        rows, _ = await fetch_trends_batch(c, ["9001"], ["cpu", "load"], "1d")
        assert [(r.metric, r.avg) for r in rows] == [("load", 1.2)]
        assert rows[0].daily == {"2026-03-10": 1.2}

    async def test_rows_are_sorted_by_host_then_metric(self):
        c = RecordingClient({
            "host.get": [{"hostid": "9002", "host": "srv-bv9002"}, {"hostid": "9001", "host": "srv-aq9001"}],
            "item.get": [_item("9002", KEY_CPU_LOAD, 1, itemid="b-load"),
                         _item("9002", KEY_CPU_IDLE, 50, itemid="b-cpu"),
                         _item("9001", KEY_CPU_LOAD, 1, itemid="a-load")],
            "trend.get": lambda p: [_trend(i, 0, 1) for i in p["itemids"]],
        })
        rows, _ = await fetch_trends_batch(c, ["9001", "9002"], ["cpu", "load"], "1d")
        assert [(r.hostname, r.metric) for r in rows] == [
            ("srv-aq9001", "load"), ("srv-bv9002", "cpu"), ("srv-bv9002", "load")]
