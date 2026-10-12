"""Wire tests for the inventory-load tools (CPU / load / memory / disk /
provider views). Real tool functions, recording client, synthetic fixtures:
documentation IPs, invented hostnames, generic group and provider labels.
"""

from __future__ import annotations

import socket
import time

import httpx
import pytest

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp import classify
from zbbx_mcp.tools import inventory_load as inv

NOW = str(int(time.time()) - 60)     # a live reading (ADR 141)
GB = 1_073_741_824
CPU_KEYS = ["system.cpu.util[,idle]", "system.cpu.load[percpu,avg1]", "vm.memory.size[available]"]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ZABBIX_PRODUCT_MAP",
                       '{"app_free": ["App", "Free"], "app_paid": ["App", "Premium"]}')
    monkeypatch.setattr(classify, "_PRODUCT_MAP", None)
    monkeypatch.setenv("ZABBIX_PROVIDER_CIDRS",
                       '{"Provider A": ["192.0.2.0/24"], "Provider B": ["198.51.100.0/24"]}')
    monkeypatch.setattr(classify, "_EXTRA_PROVIDER_NETS", None)
    monkeypatch.delenv("ZABBIX_TRAFFIC_UNIT", raising=False)


def _host(hid, name, group, *ips):
    return {"hostid": hid, "host": name, "name": name, "status": "0",
            "groups": [{"name": group}],
            "interfaces": [{"ip": "127.0.0.1"}] + [{"ip": ip} for ip in ips]}


def _item(hid, key, value, clock=NOW, **extra):
    return {"itemid": f"{hid}-{key}", "hostid": hid, "key_": key,
            "lastvalue": str(value), "lastclock": clock, **extra}


def _raise(exc):
    def f(_p):
        raise exc
    return f


def _calls(client, method):
    return [p for m, p in client.calls if m == method]


# Fleet: a parent with two sub-hosts, one paid host with a stale CPU reading,
# and one host in another country.
HOSTS = [
    _host("1", "srv-aq9001", "app_free", "192.0.2.1"),
    _host("2", "srv-aq9001 aq9002", "app_free", "192.0.2.2"),   # no metrics: inherits all
    _host("3", "srv-aq9001 aq9003", "app_free", "192.0.2.3"),   # own traffic, inherits CPU
    _host("4", "srv-bv9004", "app_paid", "198.51.100.4"),
    _host("5", "srv-hm9005", "app_free", "203.0.113.5"),
]
METRICS = [
    _item("1", "system.cpu.util[,idle]", 10),          # 90% used
    _item("1", "system.cpu.load[percpu,avg1]", 1.5),
    _item("1", "vm.memory.size[available]", 2 * GB),
    _item("4", "system.cpu.util[,idle]", 5, clock="0"),  # never collected → no figure
    _item("4", "vm.memory.size[available]", 4 * GB),
    _item("5", "system.cpu.util[,idle]", 70),          # 30% used
    _item("5", "system.cpu.load[percpu,avg1]", 3.25),
]
TRAFFIC = [
    _item("1", "net.if.in[eth0]", 8_000_000),
    _item("1", "net.if.in[eth1]", 2_000_000),          # not the busiest NIC
    _item("3", "net.if.in[eth0]", 50_000_000),
    _item("4", "net.if.in[eth0]", 1_000_000, clock="0"),
    _item("5", "net.if.in[eth0]", 20_000_000),
]


def _load_client(hosts=HOSTS, metrics=METRICS, traffic=TRAFFIC):
    def item_get(p):
        keys = p["filter"]["key_"]
        src = metrics if (isinstance(keys, list) and keys[0] in CPU_KEYS) or keys in CPU_KEYS else traffic
        return [it for it in src if it["hostid"] in p["hostids"]]
    return RecordingClient({"host.get": hosts, "item.get": item_get})


class TestServerLoad:
    def test_table_with_parent_inheritance(self):
        c = _load_client()
        out = run_tool(inv, "get_server_load", c)
        assert out.startswith("**Server Load (5 servers, sorted by cpu)**")
        assert ("| srv-aq9001 | AQ | App | Free | 192.0.2.1 | Provider A | 90.0% | 1.5 | 2.0 GB | 7.63 Mbps |"
                in out)
        # sub-host with no data of its own takes the parent's figures
        assert "| srv-aq9001 aq9002 | AQ | App | Free | 192.0.2.2 | Provider A | 90.0% | 1.5 | 2.0 GB |" in out
        # sub-host keeps its own traffic, inherits CPU
        assert "| srv-aq9001 aq9003 | AQ | App | Free | 192.0.2.3 | Provider A | 90.0% | 1.5 | 2.0 GB | 47.68 Mbps |" in out
        # a never-collected CPU reading is N/A, not 95%
        assert "| srv-bv9004 | BV | App | Premium | 198.51.100.4 | Provider B | N/A | N/A | 4.0 GB | N/A |" in out
        assert "| srv-hm9005 | HM | App | Free | 203.0.113.5 | Other | 30.0% | 3.25 | N/A |" in out
        metric_call, traffic_call = _calls(c, "item.get")
        assert metric_call["filter"] == {"key_": CPU_KEYS}
        assert metric_call["hostids"] == ["1", "2", "3", "4", "5"]
        assert traffic_call["filter"]["status"] == "0"
        assert c.sent("host.get")["filter"] == {"status": "0"}

    def test_sort_by_traffic_and_load_and_cap(self):
        out = run_tool(inv, "get_server_load", _load_client(), sort_by="traffic", max_results=2)
        assert "(2 servers, sorted by traffic)" in out
        rows = [ln for ln in out.splitlines() if ln.startswith("| srv-")]
        assert [r.split(" | ")[0] for r in rows] == ["| srv-aq9001 aq9003", "| srv-hm9005"]
        out = run_tool(inv, "get_server_load", _load_client(), sort_by="load", max_results=1)
        assert "| srv-hm9005 |" in out and "srv-aq9001" not in out

    def test_filters(self):
        c = _load_client()
        out = run_tool(inv, "get_server_load", c, tier="Premium")
        assert "(1 servers" in out and "srv-bv9004" in out
        out = run_tool(inv, "get_server_load", _load_client(), country="hm")
        assert "(1 servers" in out and "srv-hm9005" in out
        out = run_tool(inv, "get_server_load", _load_client(), product="Other")
        assert out.startswith("No servers match the filters. Among 5 enabled hosts")
        assert "product labels present: App" in out

    def test_named_hosts_and_unknown_names(self):
        c = _load_client()
        out = run_tool(inv, "get_server_load", c, hosts="srv-aq9001 aq9002,srv-tf9999", max_results=1)
        assert "_1 of 2 requested host(s) are not enabled hosts in Zabbix: srv-tf9999_" in out
        assert "| srv-aq9001 aq9002 |" in out and "90.0%" in out
        # the parent's metrics were fetched even though only the child was named
        assert _calls(c, "item.get")[0]["hostids"] == ["2", "1"]
        out = run_tool(inv, "get_server_load", _load_client(), hosts="srv-tf9998,srv-tf9999")
        assert out == "2 of 2 requested host(s) are not enabled hosts in Zabbix: srv-tf9998, srv-tf9999."

    def test_empty_fleet_and_api_error(self):
        out = run_tool(inv, "get_server_load", RecordingClient({"host.get": []}))
        assert out == "No servers match the filters: no enabled hosts in scope."
        c = RecordingClient({"host.get": _raise(httpx.ConnectError("refused"))})
        assert run_tool(inv, "get_server_load", c) == "Error: refused"


def _cpu_client(cpu, traffic=()):
    def item_get(p):
        src = cpu if p["filter"]["key_"] == "system.cpu.util[,idle]" else traffic
        return [it for it in src if it["hostid"] in p["hostids"]]
    return RecordingClient({"host.get": HOSTS, "item.get": item_get})


CPU = [
    _item("1", "system.cpu.util[,idle]", 5),     # 95% used — parent
    _item("3", "system.cpu.util[,idle]", 15),    # 85% used — sub-host of 1, folded away
    _item("4", "system.cpu.util[,idle]", 1, clock="0"),   # stale: never flagged
    _item("5", "system.cpu.util[,idle]", 95),    # 5% used
]


class TestHighCpu:
    def test_overloaded_folded_to_machine(self):
        c = _cpu_client(CPU)
        out = run_tool(inv, "get_high_cpu_servers", c, threshold=80)
        assert out.startswith("**1 servers above 80% CPU**")
        assert "| srv-aq9001 | App | Free | 192.0.2.1 | **95.0%** |" in out
        assert "aq9003" not in out and "srv-bv9004" not in out
        p = c.sent("item.get")
        assert p["filter"] == {"key_": "system.cpu.util[,idle]"}
        assert p["output"] == ["hostid", "lastvalue", "lastclock"]

    def test_child_inherits_parent_cpu(self):
        out = run_tool(inv, "get_high_cpu_servers", _cpu_client([CPU[0]]), threshold=90)
        # host 2/3 have no reading; they inherit 95% but fold into the parent row
        assert out.count("| srv-aq9001") == 1

    def test_none_above_and_no_match(self):
        out = run_tool(inv, "get_high_cpu_servers", _cpu_client(CPU), threshold=99)
        assert out == "No servers above 99% CPU usage."
        out = run_tool(inv, "get_high_cpu_servers", _cpu_client(CPU), product="Other")
        assert out == "No servers match the filter."
        out = run_tool(inv, "get_high_cpu_servers", _cpu_client(CPU), country="bv", threshold=0)
        assert out == "No servers above 0% CPU usage."   # its only reading is stale

    def test_api_error(self):
        c = RecordingClient({"host.get": _raise(ValueError("bad json"))})
        assert run_tool(inv, "get_high_cpu_servers", c) == "Error: bad json"


class TestUnderloaded:
    def test_idle_servers_with_traffic(self):
        traffic = [_item("5", "net.if.in[eth0]", 3_000_000), _item("5", "net.if.in[eth1]", 1_000_000)]
        c = _cpu_client(CPU, traffic)
        out = run_tool(inv, "get_underloaded_servers", c, threshold=10)
        assert out.startswith("**1 servers below 10% CPU**")
        assert "| srv-hm9005 | App | Free | 203.0.113.5 | 5.0% | 3.0 |" in out
        assert len(_calls(c, "item.get")) == 2

    def test_traffic_falls_back_to_parent_and_na(self):
        cpu = [_item("1", "system.cpu.util[,idle]", 99)]
        traffic = [_item("1", "net.if.in[eth0]", 12_000_000)]
        out = run_tool(inv, "get_underloaded_servers", _cpu_client(cpu, traffic), country="aq")
        assert out.startswith("**1 servers below 10.0% CPU**")
        assert "| srv-aq9001 | App | Free | 192.0.2.1 | 1.0% | 12.0 |" in out
        out = run_tool(inv, "get_underloaded_servers", _cpu_client(cpu, []), country="aq")
        assert "| 1.0% | N/A |" in out

    def test_empty_paths(self):
        out = run_tool(inv, "get_underloaded_servers", _cpu_client(CPU), threshold=1)
        assert out == "No servers below 1% CPU usage."
        out = run_tool(inv, "get_underloaded_servers", _cpu_client(CPU), product="Other")
        assert out == "No servers match the filter."
        c = RecordingClient({"host.get": _raise(httpx.ReadTimeout("slow"))})
        assert run_tool(inv, "get_underloaded_servers", c) == "Error: slow"


class TestProviderSummary:
    def test_matrix(self):
        hosts = HOSTS + [_host("6", "srv-sj9006", "app_paid"),             # no IP: skipped
                         _host("7", "srv-pn9007", "infra", "10.0.0.7")]     # Unknown product
        c = RecordingClient({"host.get": hosts})
        out = run_tool(inv, "get_provider_summary", c)
        assert out.startswith("**Provider Summary: 5 servers**")
        assert "| Provider A | 3 |" in out and "| Provider B | 1 |" in out and "| Other | 1 |" in out
        assert "| Product / Tier | Provider A | Provider B | Other |" in out
        assert "| App / Free | 3 |  | 1 |" in out
        assert "| App / Premium |  | 1 |  |" in out
        assert "srv-sj9006" not in out and "matched no known range" not in out

    def test_poor_coverage_note(self):
        hosts = [_host(str(30 + i), f"srv-pn{9030 + i}", "app_free", f"10.4.0.{i}") for i in range(1, 4)]
        out = run_tool(inv, "get_provider_summary", RecordingClient({"host.get": hosts}))
        assert "| Other | 3 |" in out
        assert "3 of 3 addresses matched no known range" in out

    def test_product_filter_and_empty(self):
        out = run_tool(inv, "get_provider_summary", RecordingClient({"host.get": HOSTS}), product="Other")
        assert out == "No servers match the filter."
        c = RecordingClient({"host.get": _raise(httpx.ConnectError("down"))})
        assert run_tool(inv, "get_provider_summary", c) == "Error: down"


OTHER_HOSTS = [
    _host("11", "srv-aq9011", "app_free", "10.1.0.1"),
    _host("12", "srv-aq9012", "app_free", "10.1.0.2"),
    _host("13", "srv-aq9013", "app_free", "10.1.5.3"),
    _host("14", "srv-aq9014", "app_paid", "10.1.6.4"),
    _host("15", "srv-bv9015", "app_free", "10.2.0.5"),
    _host("16", "srv-bv9016", "app_free", "192.0.2.16"),   # Provider A: not unclassified
    _host("17", "srv-bv9017", "app_free"),                 # no IP
]


class TestUnknownProviders:
    def test_grouped_by_16(self):
        out = run_tool(inv, "get_unknown_providers", RecordingClient({"host.get": OTHER_HOSTS}))
        assert out.startswith("**5 unclassified servers** in 2 /16 prefixes")
        assert ("| 10.1.0.0/16 | 4 | 10.1.0.1 | srv-aq9011 (App), srv-aq9012 (App), srv-aq9013 (App) |"
                in out)
        assert "| 10.2.0.0/16 | 1 | 10.2.0.5 | srv-bv9015 (App) |" in out

    def test_cap_and_empty(self):
        out = run_tool(inv, "get_unknown_providers", RecordingClient({"host.get": OTHER_HOSTS}),
                       max_results=1)
        assert "10.2.0.0/16" not in out and "*1 more prefixes omitted*" in out
        out = run_tool(inv, "get_unknown_providers", RecordingClient({"host.get": HOSTS[:4]}))
        assert out == "No unclassified servers."
        c = RecordingClient({"host.get": _raise(ValueError("x"))})
        assert run_tool(inv, "get_unknown_providers", c) == "Error: x"


class TestIdentifyProviders:
    def test_rdns_suggestions(self, monkeypatch):
        names = {"10.1.0.1": "node1.rack.example.com", "10.2.0.5": "example.net"}

        def fake(ip):
            if ip in names:
                return names[ip], [], [ip]
            raise socket.herror("no PTR")
        monkeypatch.setattr(socket, "gethostbyaddr", fake)
        hosts = OTHER_HOSTS + [_host("18", "srv-bv9018", "app_free", "10.2.0.6"),
                               _host("19", "srv-hm9019", "app_free", "10.3.0.1"),
                               _host("20", "srv-hm9020", "app_free", "10.3.0.2")]
        c = RecordingClient({"host.get": hosts})
        out = run_tool(inv, "identify_providers", c)
        assert out.startswith("**Provider identification** (3 prefixes)")
        assert '| 10.1.0.0/16 | 4 | rack.example.com | "10.1.0.0/16" |' in out
        assert "| 10.2.0.0/16 | 2 | example.net | low count |" in out
        assert "| 10.3.0.0/16 | 2 | no rDNS | low count |" in out
        assert '- **rack.example.com** (4 servers): ["10.1.0.0/16"]' in out
        assert "example.net** (" not in out      # under 3 servers: no suggestion
        assert c.sent("host.get")["filter"] == {"status": "0"}

    def test_nothing_to_identify(self, monkeypatch):
        monkeypatch.setattr(socket, "gethostbyaddr", _raise(AssertionError("no lookups expected")))
        out = run_tool(inv, "identify_providers", RecordingClient({"host.get": OTHER_HOSTS}),
                       min_servers=5)
        assert out == "No unclassified prefixes with 5+ servers."
        c = RecordingClient({"host.get": _raise(httpx.ConnectError("down"))})
        assert run_tool(inv, "identify_providers", c) == "Error: down"


def _disk_client(vfs, custom, hosts=HOSTS):
    def item_get(p):
        return custom if p["filter"].get("key_") == "disk.fs.root" else vfs
    return RecordingClient({"item.get": item_get,
                            "host.get": lambda p: [h for h in hosts if h["hostid"] in p["hostids"]]})


class TestLowDisk:
    VFS = [
        _item("1", "vfs.fs.size[/,pused]", 92.5),
        _item("1", "vfs.fs.size[/data,pfree]", 40),     # 60% used: lower than / on the same host
        _item("3", "vfs.fs.size[/,pused]", 88),         # sub-host of 1: folded away
        _item("4", "vfs.fs.size[/var,pfree]", 15),      # 85% used
        _item("5", "vfs.fs.size[/,total]", 1000),       # neither pused nor pfree
        _item("5", "vfs.fs.size[/,pused]", 99, clock="0"),   # stale
    ]
    CUSTOM = [_item("5", "disk.fs.root", 75), _item("4", "disk.fs.root", 10),
              _item("1", "disk.fs.root", 99, clock="0")]          # stale: ignored

    def test_flagged_and_folded(self):
        c = _disk_client(self.VFS, self.CUSTOM)
        out = run_tool(inv, "get_low_disk_servers", c)
        assert out.startswith("**3 servers above 70.0% disk usage**")
        assert "| srv-aq9001 | **92.5%** (CRITICAL) | / | App | Provider A |" in out
        assert "| srv-bv9004 | **85.0%** (WARNING) | /var | App | Provider B |" in out
        assert "| srv-hm9005 | **75.0%** (ALERT) | / | App | Other |" in out
        assert "aq9003" not in out
        vfs_p, custom_p = _calls(c, "item.get")
        assert vfs_p["search"] == {"key_": "*vfs.fs.size*"} and vfs_p["searchWildcardsEnabled"] is True
        assert custom_p["filter"] == {"key_": "disk.fs.root", "status": "0"}
        assert sorted(c.sent("host.get")["hostids"]) == ["1", "3", "4", "5"]

    def test_cap_and_unknown_host(self):
        out = run_tool(inv, "get_low_disk_servers", _disk_client(self.VFS, self.CUSTOM, hosts=[]),
                       max_results=1)
        # host names unavailable: fall back to the hostid, provider unknown
        assert "| 1 | **92.5%** (CRITICAL) | / | Unknown | ? |" in out
        assert "*3 more not shown*" in out

    def test_empty_paths(self):
        out = run_tool(inv, "get_low_disk_servers", _disk_client([], []))
        assert out == "No disk utilization data found."
        out = run_tool(inv, "get_low_disk_servers", _disk_client(self.VFS, self.CUSTOM), threshold=95)
        assert out == "No servers above 95% disk usage."
        c = RecordingClient({"item.get": _raise(httpx.ConnectError("down"))})
        assert run_tool(inv, "get_low_disk_servers", c) == "Error: down"


class TestLowMemory:
    MEM = [
        _item("1", "vm.memory.size[available]", 0.05 * GB),
        _item("3", "vm.memory.size[available]", 0.02 * GB),   # sub-host of 1, worst: wins the fold
        _item("4", "vm.memory.size[available]", 0.25 * GB),
        _item("4", "vm.memory.size[available]", 0.4 * GB),    # second item: the lower one counts
        _item("5", "vm.memory.size[available]", 0.4 * GB),
        _item("2", "vm.memory.size[available]", 0, clock="0"),  # never collected, not 0 GB
    ]

    def _client(self, items, hosts=HOSTS):
        return RecordingClient({"item.get": items,
                                "host.get": lambda p: [h for h in hosts if h["hostid"] in p["hostids"]]})

    def test_flagged(self):
        c = self._client(self.MEM)
        out = run_tool(inv, "get_low_memory_servers", c)
        assert out.startswith("**3 servers below 0.5 GB free**")
        assert "| srv-aq9001 aq9003 | **0.02 GB** (CRITICAL) | App | Provider A |" in out
        assert "| srv-bv9004 | **0.25 GB** (WARNING) | App | Provider B |" in out
        assert "| srv-hm9005 | **0.4 GB** (LOW) | App | Other |" in out
        assert "| srv-aq9001 |" not in out and "aq9002" not in out
        assert c.sent("item.get")["filter"] == {"key_": "vm.memory.size[available]", "status": "0"}

    def test_cap_and_missing_host(self):
        out = run_tool(inv, "get_low_memory_servers", self._client(self.MEM, hosts=[]), max_results=2)
        assert "| 3 | **0.02 GB** (CRITICAL) | Unknown | ? |" in out
        assert "*2 more not shown*" in out

    def test_empty_paths(self):
        out = run_tool(inv, "get_low_memory_servers", self._client([self.MEM[-1]]))
        assert out == "No memory data found."
        out = run_tool(inv, "get_low_memory_servers", self._client(self.MEM), threshold_gb=0.01)
        assert out == "No servers below 0.01 GB free memory."
        c = RecordingClient({"item.get": _raise(ValueError("broken"))})
        assert run_tool(inv, "get_low_memory_servers", c) == "Error: broken"
