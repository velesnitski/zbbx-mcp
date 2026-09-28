"""ADR 147: trend traffic is found by NIC-key prefix, not by an exact key list.

The stock agent template writes ``net.if.in["enp3s0"]`` (quoted); the in-house
one writes ``net.if.in[eth0]``. The batch fetch used an exact list of the
unquoted spellings, so every host on the stock template reported no traffic
at all — indistinguishable from a host with no NIC. Fixtures are neutral.
"""

from __future__ import annotations

import asyncio
import time

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp.fetch import fetch_trends_batch
from zbbx_mcp.tools import trends_compare

NOW = int(time.time())


def _item(hid, key, last, itemid):
    return {"itemid": itemid, "hostid": hid, "key_": key, "lastvalue": str(last),
            "lastclock": str(NOW - 60), "value_type": "3"}


def _trend(itemid, k, avg):
    return {"itemid": itemid, "clock": str(NOW - 3600 * (k + 1)),
            "value_avg": str(avg), "value_max": str(avg * 2), "value_min": str(avg // 2)}


HOSTS = [{"hostid": "9001", "host": "srv-aq9001"}, {"hostid": "9002", "host": "srv-bv9002"}]

ITEMS = [
    _item("9001", 'net.if.in["enp3s0"]', 4_000_000, "i-q-phys"),     # stock template, quoted
    _item("9001", 'net.if.in["vmbr0"]', 9_000_000, "i-q-bridge"),    # bridge: busier, must lose
    _item("9001", 'net.if.out["enp3s0"]', 1_000_000, "o-q-phys"),
    _item("9002", "net.if.in[eth0]", 2_000_000, "i-u-phys"),         # in-house template, unquoted
    _item("9002", "system.cpu.util[,idle]", 80, "c-u"),
]


def _client():
    def item_get(p):
        if "search" in p:
            pat = p["search"]["key_"].strip("*")          # "net.if.in[" / "net.if.out["
            return [i for i in ITEMS if i["key_"].startswith(pat) and i["hostid"] in p["hostids"]]
        wanted = p["filter"]["key_"]
        return [i for i in ITEMS if i["key_"] in wanted and i["hostid"] in p["hostids"]]

    def trend_get(p):
        return [_trend(i, k, 3_000_000) for i in p["itemids"] for k in range(3)]

    return RecordingClient({"host.get": HOSTS, "item.get": item_get, "trend.get": trend_get})


def _rows(metrics):
    rows, _ = asyncio.run(fetch_trends_batch(_client(), ["9001", "9002"], metrics, "1d"))
    return {(r.hostid, r.metric): r for r in rows}


class TestPrefixDiscovery:
    def test_quoted_and_unquoted_hosts_both_get_a_traffic_row(self):
        rows = _rows(["traffic"])
        assert ("9001", "traffic") in rows, "the quoted-key host was invisible before ADR 147"
        assert ("9002", "traffic") in rows

    def test_the_bridge_is_not_the_carrier_even_when_busier(self):
        c = _client()
        asyncio.run(fetch_trends_batch(c, ["9001"], ["traffic"], "1d"))
        assert c.sent("trend.get")["itemids"] == ["i-q-phys"]

    def test_outbound_is_discovered_the_same_way(self):
        rows = _rows(["traffic", "traffic_out"])
        assert ("9001", "traffic_out") in rows

    def test_exact_keys_still_serve_the_other_metrics(self):
        c = _client()
        asyncio.run(fetch_trends_batch(c, ["9002"], ["cpu", "traffic"], "1d"))
        exact = next(p for m, p in c.calls if m == "item.get" and "filter" in p and "search" not in p)
        assert "system.cpu.util[,idle]" in exact["filter"]["key_"]
        assert not any(k.startswith("net.if") for k in exact["filter"]["key_"])
        searches = [p["search"]["key_"] for m, p in c.calls if m == "item.get" and "search" in p]
        assert searches == ["*net.if.in[*"]

    def test_traffic_only_makes_no_exact_query(self):
        c = _client()
        asyncio.run(fetch_trends_batch(c, ["9001"], ["traffic"], "1d"))
        assert not any(m == "item.get" and "search" not in p for m, p in c.calls)


class TestThroughTheTool:
    def test_batch_tool_shows_the_quoted_host(self):
        c = _client()
        out = run_tool(trends_compare, "get_trends_batch", c, hosts="srv-aq9001,srv-bv9002",
                       metrics="traffic", period="1d")
        assert "srv-aq9001" in out and "srv-bv9002" in out
        assert "3 Mbps" in out or "3.0" in out, out
