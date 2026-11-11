"""ADR 166: configuration reads are cached briefly; values never are.

Synthetic fixtures (uninhabited-territory names, reserved numbering).
"""

from __future__ import annotations

import asyncio
import math
import time

import httpx
import pytest

from tests.wiretest import RecordingClient
from zbbx_mcp import config_cache, fetch, trends
from zbbx_mcp.client import ZabbixClient
from zbbx_mcp.config import ZabbixConfig
from zbbx_mcp.fetch import fetch_trends_batch, physical_traffic_items

NOW = int(time.time())
LIVE = str(NOW - 30)


class CachingClient(RecordingClient):
    """A recording client with the real client's cache semantics."""

    def __init__(self, responses=None, delay: float = 0.0):
        super().__init__(responses)
        self._cache: dict = {}
        self.delay = delay

    async def call(self, method, params=None):
        if self.delay:
            await asyncio.sleep(self.delay)
        return await super().call(method, params)

    def _get_cached(self, key, ttl=60.0):
        hit = self._cache.get(key)
        return hit[1] if hit and time.monotonic() - hit[0] < ttl else None

    def _set_cache(self, key, value):
        self._cache[key] = (time.monotonic(), value)


def _fleet(n: int):
    """``n`` hosts, each with one physical and one virtual inbound NIC."""
    disc = []
    for i in range(n):
        hid = str(9000 + i)
        disc += [{"itemid": f"p{hid}", "hostid": hid, "key_": "net.if.in[eth0]"},
                 {"itemid": f"v{hid}", "hostid": hid, "key_": "net.if.in[docker0]"}]
    return disc


def _item_get(disc):
    def item_get(p):
        if "itemids" in p:            # value read by id
            return [{**d, "lastvalue": "1000000", "lastclock": LIVE}
                    for d in disc if d["itemid"] in p["itemids"]]
        if "hostids" in p:            # direct per-host search
            return [{**d, "lastvalue": "1000000", "lastclock": LIVE}
                    for d in disc if d["hostid"] in p["hostids"]]
        return list(disc)             # fleet-wide discovery
    return item_get


class TestCache:
    def test_ttl_and_disable(self, monkeypatch):
        monkeypatch.delenv("ZABBIX_CONFIG_CACHE_TTL", raising=False)
        assert config_cache.config_ttl() == config_cache.DEFAULT_TTL_S
        monkeypatch.setenv("ZABBIX_CONFIG_CACHE_TTL", "0")
        assert config_cache.config_ttl() == 0
        monkeypatch.setenv("ZABBIX_CONFIG_CACHE_TTL", "junk")
        assert config_cache.config_ttl() == config_cache.DEFAULT_TTL_S

    def test_a_repeat_read_is_served_from_the_cache(self):
        c = CachingClient({"host.get": [{"hostid": "9001"}]})
        for _ in range(3):
            assert asyncio.run(config_cache.cached_config(c, "host.get", {"output": ["hostid"]}))
        assert len(c.calls) == 1

    def test_concurrent_identical_reads_share_one_request(self):
        c = CachingClient({"host.get": [{"hostid": "9001"}]}, delay=0.05)

        async def both():
            return await asyncio.gather(*(config_cache.cached_config(c, "host.get", {"a": 1})
                                          for _ in range(5)))
        results = asyncio.run(both())
        assert len(c.calls) == 1 and all(r == [{"hostid": "9001"}] for r in results)

    def test_disabled_cache_always_asks(self, monkeypatch):
        monkeypatch.setenv("ZABBIX_CONFIG_CACHE_TTL", "0")
        c = CachingClient({"host.get": []})
        for _ in range(2):
            asyncio.run(config_cache.cached_config(c, "host.get", {}))
        assert len(c.calls) == 2


class TestFleetDiscovery:
    def test_a_fleet_request_discovers_once_then_reads_values_by_id(self):
        disc = _fleet(fetch.FLEET_DISCOVERY_MIN_HOSTS)
        c = CachingClient({"item.get": _item_get(disc)})
        ids = sorted({d["hostid"] for d in disc})
        for _ in range(2):
            items = asyncio.run(physical_traffic_items(c, ids, output=("hostid", "lastvalue", "lastclock")))
            assert len(items) == len(ids) and all(it["key_"] == "net.if.in[eth0]" for it in items)
        calls = [p for m, p in c.calls if m == "item.get"]
        discovery = [p for p in calls if "itemids" not in p]
        assert len(discovery) == 1                        # the scan ran once
        assert discovery[0]["templated"] is False and "hostids" not in discovery[0]
        by_id = [p for p in calls if "itemids" in p]
        assert len(by_id) == 2 and "lastclock" in by_id[0]["output"]
        # Virtual NICs are dropped before the value read, not after it.
        assert set(by_id[0]["itemids"]) == {"p" + h for h in ids}

    def test_a_small_request_uses_a_warm_cache_and_otherwise_asks_directly(self):
        disc = _fleet(fetch.FLEET_DISCOVERY_MIN_HOSTS)
        c = CachingClient({"item.get": _item_get(disc)})
        few = [str(9000 + i) for i in range(fetch.WARM_DISCOVERY_MIN_HOSTS)]
        asyncio.run(physical_traffic_items(c, few))
        assert "hostids" in c.calls[0][1]                  # cold: direct per-host search
        asyncio.run(physical_traffic_items(c, sorted({d["hostid"] for d in disc})))
        c.calls.clear()
        asyncio.run(physical_traffic_items(c, few, output=("itemid", "hostid", "key_")))
        assert c.calls == []                               # warm, structure only: no call

    def test_a_write_clears_the_cache(self):
        async def run():
            def handler(request):
                return httpx.Response(200, json={"jsonrpc": "2.0", "result": [], "id": 1})
            client = ZabbixClient(ZabbixConfig(url="https://zabbix.example.com", token="t"))
            client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                               base_url="https://zabbix.example.com")
            client._set_cache("cfg:x", [1])
            await client.call("host.get", {})
            assert client._get_cached("cfg:x", ttl=60) == [1]      # a read keeps it
            await client.call("item.create", {})
            assert client._get_cached("cfg:x", ttl=60) is None     # a write clears it
            await client.close()
        asyncio.run(run())


class TestTrendsWithoutExtremes:
    def _client(self, seen):
        def trend_get(p):
            seen.append(p["output"])
            return [{"itemid": "t1", "clock": str(NOW - h * 3600), "value_avg": "8000000",
                     "value_max": "9000000", "value_min": "7000000"} for h in range(48)]
        return RecordingClient({
            "host.get": [{"hostid": "9001", "host": "srv-aq9001"}],
            "item.get": [{"itemid": "t1", "hostid": "9001", "key_": "net.if.in[eth0]",
                          "lastvalue": "8000000", "lastclock": LIVE, "value_type": "3"}],
            "trend.get": trend_get,
        })

    def test_only_averages_are_fetched_and_extremes_are_nan(self):
        seen: list = []
        full, _ = asyncio.run(fetch_trends_batch(self._client(seen), ["9001"], ["traffic"], "2d"))
        lean, _ = asyncio.run(fetch_trends_batch(self._client(seen), ["9001"], ["traffic"], "2d",
                                                 extremes=False))
        assert "value_max" in seen[0] and seen[1] == ["itemid", "clock", "value_avg"]
        (f,), (ln,) = full, lean
        assert (f.avg, f.daily, f.trend_dir, f.current) == (ln.avg, ln.daily, ln.trend_dir, ln.current)
        assert f.peak == 9.0 and math.isnan(ln.peak) and math.isnan(ln.min_val)


class TestTransport:
    def test_responses_are_parsed(self):
        async def run():
            def handler(request):
                return httpx.Response(200, content=b'{"jsonrpc":"2.0","result":[{"hostid":"9001"}],"id":1}')
            client = ZabbixClient(ZabbixConfig(url="https://zabbix.example.com", token="t"))
            client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                               base_url="https://zabbix.example.com")
            try:
                return await client.call("host.get", {})
            finally:
                await client.close()
        assert asyncio.run(run()) == [{"hostid": "9001"}]

    @pytest.mark.parametrize("raw,expected", [("", 4), ("8", 8), ("0", 1), ("99", 16), ("x", 4)])
    def test_trend_concurrency_is_clamped(self, monkeypatch, raw, expected):
        monkeypatch.setenv("ZABBIX_TREND_CONCURRENCY", raw)
        assert trends.concurrency() == expected
