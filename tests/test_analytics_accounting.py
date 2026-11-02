"""ADR 160: analytics say which calls failed and what they cost in API time."""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest

from zbbx_mcp import logging as zlog
from zbbx_mcp.client import ZabbixClient
from zbbx_mcp.config import ZabbixConfig


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def captured(monkeypatch):
    log = logging.getLogger("zbbx_mcp.analytics.test")
    log.propagate = False
    log.setLevel(logging.INFO)
    h = _Capture()
    log.addHandler(h)
    monkeypatch.setattr(zlog, "_analytics_logger", log)
    yield h.records
    log.removeHandler(h)


def _client(delay: float = 0.0) -> ZabbixClient:
    async def handler(request):
        await asyncio.sleep(delay)
        return httpx.Response(200, json={"jsonrpc": "2.0", "result": [], "id": 1})
    c = ZabbixClient(ZabbixConfig(url="https://zabbix.example.com", token="t"))
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                  base_url="https://zabbix.example.com")
    return c


class TestStatus:
    def test_error_text_is_a_failure(self, captured):
        @zlog.logged
        async def some_tool():
            return "Error: Server error '500 Internal Server Error'"
        asyncio.run(some_tool())
        (r,) = captured
        assert r.status == "error" and r.error_detail.startswith("Error: Server error")

    def test_a_normal_answer_is_ok(self, captured):
        @zlog.logged
        async def some_tool():
            return "No errors found."      # "Error" mid-text is not a failure
        asyncio.run(some_tool())
        assert captured[0].status == "ok" and captured[0].error_detail == ""


class TestApiAccounting:
    def test_calls_made_in_parallel_are_all_counted(self, captured):
        client = _client(delay=0.01)

        @zlog.logged
        async def some_tool():
            await asyncio.gather(*(client.call("host.get", {}) for _ in range(3)))
            await client.call("item.get", {})
            return "ok"
        asyncio.run(some_tool())
        (r,) = captured
        assert r.api_calls == 4 and r.api_ms >= 10

    def test_concurrent_tool_calls_do_not_mix(self, captured):
        client = _client()

        @zlog.logged
        async def busy():
            for _ in range(5):
                await client.call("host.get", {})
            return "ok"

        @zlog.logged
        async def quiet():
            await client.call("host.get", {})
            return "ok"

        async def both():
            await asyncio.gather(busy(), quiet())
        asyncio.run(both())
        assert sorted(r.api_calls for r in captured) == [1, 5]

    def test_api_calls_outside_a_tool_are_not_counted_anywhere(self):
        asyncio.run(_client().call("host.get", {}))   # no tool in progress: no error
        assert zlog._api_stats.get() is None

    def test_the_json_line_carries_the_counts(self):
        rec = logging.LogRecord("a", logging.INFO, "", 0, "t", None, None)
        rec.tool, rec.api_calls, rec.api_ms = "t", 3, 120
        entry = json.loads(zlog.AnalyticsFormatter().format(rec))
        assert entry["api_calls"] == 3 and entry["api_ms"] == 120
