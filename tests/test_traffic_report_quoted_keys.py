"""ADR 154: the traffic report and anomaly detector see quoted interface keys.

Both read traffic through an exact list of unquoted keys, so a host on the
stock agent template (``net.if.in["enp3s0"]``) had no traffic at all — the
gap ADR 147 closed for the trend tools.
"""

from __future__ import annotations

import time

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp.tools import traffic

LIVE = str(int(time.time()) - 60)
HOSTS = [
    {"hostid": "1", "host": "srv-aq9001", "groups": [{"name": "app_free"}],
     "interfaces": [{"ip": "192.0.2.1", "main": "1", "type": "1"}]},
    {"hostid": "2", "host": "srv-aq9002", "groups": [{"name": "app_free"}],
     "interfaces": [{"ip": "192.0.2.2", "main": "1", "type": "1"}]},
]


def _client():
    def items(p):
        if "search" in p:
            return [
                {"itemid": "a", "hostid": "1", "key_": "net.if.in[eth0]", "lastvalue": "10000000", "lastclock": LIVE},
                {"itemid": "b", "hostid": "2", "key_": 'net.if.in["enp3s0"]', "lastvalue": "30000000",
                 "lastclock": LIVE},
                {"itemid": "c", "hostid": "2", "key_": 'net.if.in["vmbr0"]', "lastvalue": "90000000",
                 "lastclock": LIVE},   # a bridge: never the carrier
            ]
        return []
    return RecordingClient({"host.get": HOSTS, "item.get": items})


def test_the_quoted_key_host_has_its_traffic():
    c = _client()
    out = run_tool(traffic, "get_traffic_report", c)
    row = next(line for line in out.splitlines() if line.startswith("| srv-aq9002 |"))
    assert "30.0 Mbps" in row, out
    assert any("search" in p for m, p in c.calls if m == "item.get")
    assert not any((p.get("filter") or {}).get("key_") and isinstance(p["filter"]["key_"], list)
                   and any(k.startswith("net.if") for k in p["filter"]["key_"])
                   for m, p in c.calls if m == "item.get")
