"""`get_trends_batch(hosts=...)`: an explicit host list is a set, not a filter (ADR 139).

Before this, the only way to get 7d trends for a named set was a country/tier
filter whose output could exceed what the client shows, or the comparison tool,
which has no min column and no trend. A caller who names the hosts has already
chosen the set: the cap must not trim it, and a name Zabbix does not know must
be reported, not dropped — a missing row in a long table is invisible.
"""

from __future__ import annotations

import time

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp.tools import trends_compare

NOW = int(time.time())


def _host(hid, name):
    return {"hostid": hid, "host": name, "groups": [{"name": "app_free"}]}


def _client(known):
    hosts = [_host(str(i + 1), n) for i, n in enumerate(known)]

    def host_get(p):
        wanted = (p.get("filter") or {}).get("host")
        if wanted:
            return [h for h in hosts if h["host"] in wanted]
        if p.get("hostids"):
            return [h for h in hosts if h["hostid"] in p["hostids"]]
        return hosts

    def item_get(p):
        return [{"itemid": f"i{h}", "hostid": h, "key_": "system.cpu.util[,idle]", "lastvalue": "95",
                 "name": "CPU utilization", "units": "%", "value_type": "0"}
                for h in p.get("hostids", [])]

    def trend_get(p):
        return [{"itemid": i, "clock": str(NOW - 3600), "num": "60",
                 "value_min": "1", "value_avg": "5", "value_max": "9"}
                for i in p.get("itemids", [])]

    return RecordingClient({"host.get": host_get, "item.get": item_get, "trend.get": trend_get})


class TestExplicitHosts:
    def test_the_list_is_sent_as_an_exact_filter(self):
        c = _client(["srv-aq01", "srv-aq02", "srv-bv01"])
        run_tool(trends_compare, "get_trends_batch", c, hosts="srv-aq01, srv-bv01", metrics="cpu")
        sent = c.sent("host.get")
        assert sent["filter"]["host"] == ["srv-aq01", "srv-bv01"]

    def test_max_results_cannot_trim_a_named_set(self):
        names = [f"srv-aq{i:02d}" for i in range(1, 8)]
        c = _client(names)
        out = run_tool(trends_compare, "get_trends_batch", c, hosts=",".join(names),
                       metrics="cpu", max_results=2)
        assert "for 7 servers" in out, out
        for n in names:
            assert n in out

    def test_an_unknown_name_is_reported_not_dropped(self):
        c = _client(["srv-aq01"])
        out = run_tool(trends_compare, "get_trends_batch", c,
                       hosts="srv-aq01,srv-hm99", metrics="cpu")
        assert "1 of 2 requested host(s) are not enabled hosts in Zabbix: srv-hm99" in out, out
        assert "srv-aq01 | cpu" in out

    def test_all_names_unknown_is_an_answer_about_the_names(self):
        c = _client(["srv-aq01"])
        out = run_tool(trends_compare, "get_trends_batch", c, hosts="srv-hm98,srv-hm99")
        assert "srv-hm98, srv-hm99" in out
        assert "No servers match" not in out

    def test_without_hosts_nothing_changes(self):
        c = _client(["srv-aq01", "srv-aq02"])
        run_tool(trends_compare, "get_trends_batch", c, metrics="cpu")
        assert "host" not in c.sent("host.get")["filter"]
