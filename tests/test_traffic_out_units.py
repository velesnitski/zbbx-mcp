"""ADR 153: outbound traffic in the trend batch is in Mbps, like inbound.

ADR 147 added outbound discovery but converted only the inbound metric, so
outbound rows printed bits per second under an "Mbps" header.
"""

from __future__ import annotations

import asyncio
import time

from tests.wiretest import RecordingClient
from zbbx_mcp.fetch import fetch_trends_batch

MBPS = 1_000_000


def _client():
    now = int(time.time())
    items = [
        {"itemid": "in", "hostid": "9001", "key_": "net.if.in[eth0]", "lastvalue": str(40 * MBPS),
         "lastclock": str(now - 60), "value_type": "3"},
        {"itemid": "out", "hostid": "9001", "key_": "net.if.out[eth0]", "lastvalue": str(25 * MBPS),
         "lastclock": str(now - 60), "value_type": "3"},
    ]

    def item_get(p):
        pat = (p.get("search") or {}).get("key_", "").strip("*")
        return [i for i in items if pat and i["key_"].startswith(pat)]

    def trend_get(p):
        rows = []
        for iid in p["itemids"]:
            base = 40 if iid == "in" else 25
            for k in range(8):
                rows.append({"itemid": iid, "clock": str(now - 3600 * (k + 1)),
                             "value_avg": str(base * MBPS), "value_max": str(2 * base * MBPS),
                             "value_min": str(base * MBPS // 2)})
        return rows

    return RecordingClient({"host.get": [{"hostid": "9001", "host": "srv-aq9001"}],
                            "item.get": item_get, "trend.get": trend_get})


def test_outbound_rows_are_in_mbps():
    rows, _ = asyncio.run(fetch_trends_batch(_client(), ["9001"], ["traffic", "traffic_out"], "1d"))
    got = {r.metric: r for r in rows}
    out = got["traffic_out"]
    assert (out.avg, out.peak, out.min_val, out.current) == (25.0, 50.0, 12.5, 25.0)
    assert got["traffic"].avg == 40.0


def test_outbound_daily_breakdown_is_in_mbps():
    rows, _ = asyncio.run(fetch_trends_batch(_client(), ["9001"], ["traffic_out"], "1d"))
    assert rows and all(v == 25.0 for v in rows[0].daily.values())
