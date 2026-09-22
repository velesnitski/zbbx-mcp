"""Wire tests for the infrastructure analysis tools (``tools/analysis.py``).

Role classification, log/IP correlation and the two address audits. Each
test pins the rendered row for a fixture host and the API parameters the
tool sent. Reverse DNS is stubbed: no fixture address is ever resolved.
Fixtures are synthetic.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import time

import pytest

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp import classify
from zbbx_mcp.tools import analysis

NOW = int(time.time())
LIVE = str(NOW)
STALE = str(NOW - 7200)


@pytest.fixture(autouse=True)
def _configured(monkeypatch, tmp_path):
    monkeypatch.setattr(classify, "_PRODUCT_MAP", {})
    # One operator-configured range, so fixtures can show two providers and
    # one datacenter without depending on the built-in tables.
    net = ipaddress.ip_network("198.51.100.0/24")
    monkeypatch.setattr(classify, "_EXTRA_PROVIDER_NETS", [("Provider A", net)])
    monkeypatch.setattr(classify, "_EXTRA_DC_NETS", [("Provider A", "Base, AQ", net)])
    monkeypatch.setenv("ZBBX_FILE_ROOTS", str(tmp_path))


def _host(hid, name, *ips, group="app_free"):
    return {"hostid": hid, "host": name, "name": name, "groups": [{"name": group}],
            "interfaces": [{"ip": ip, "type": "1", "main": "1" if i == 0 else "0"}
                           for i, ip in enumerate(ips)]}


def _item(hid, key, bps, clock=LIVE):
    return {"itemid": f"{hid}:{key}", "hostid": hid, "key_": key,
            "lastvalue": str(bps), "lastclock": clock}


class TestAnalyzeServerRoles:
    HOSTS = [
        _host("1", "srv-aq9001", "192.0.2.10"),
        _host("2", "srv-aq9002", "192.0.2.11"),
        _host("3", "srv-bv9001", "198.51.100.20"),
        _host("4", "srv-hm9001", "203.0.113.30", group="app_paid"),
        _host("5", "srv-tf9001", "203.0.113.40"),
    ]
    ITEMS = [
        _item("1", "net.if.in[eth0]", 50_000_000),                 # carrier only: relay
        _item("1", "net.if.in[lo]", 999_000_000),                  # loopback: ignored
        _item("2", "net.if.in[eth0]", 0),
        _item("2", "net.if.in[tun0]", 30_000_000),                 # tunnel only: endpoint
        _item("3", "net.if.in[eth0]", 40_000_000),
        _item("3", "net.if.in[tun0]", 20_000_000),                 # both: mixed
        _item("5", "net.if.in[eth0]", 90_000_000, clock=STALE),    # stopped reporting: no role
    ]

    def _client(self):
        return RecordingClient({"host.get": self.HOSTS, "item.get": self.ITEMS})

    def test_roles_from_carrier_and_tunnel_traffic(self):
        c = self._client()
        out = run_tool(analysis, "analyze_server_roles", c)
        assert out.startswith("Server Classification (5 hosts): endpoint: 1, idle: 2, mixed: 1, relay: 1\n"), out
        rows = [ln for ln in out.splitlines() if ln.startswith("| srv-")]
        assert rows == [
            "| srv-bv9001 | mixed | 40.0 | 20.0 | Provider A | BV |",
            "| srv-aq9001 | relay | 50.0 | 0.0 | Other | AQ |",
            "| srv-aq9002 | endpoint | 0.0 | 30.0 | Other | AQ |",
            "| srv-hm9001 | idle | 0.0 | 0.0 | Other | HM |",
            "| srv-tf9001 | idle | 0.0 | 0.0 | Other | TF |",
        ]
        sent = c.sent("item.get")
        assert sent["search"] == {"key_": "*net.if.in[*"} and sent["searchWildcardsEnabled"] is True
        assert "lastclock" in sent["output"]

    def test_filters_and_cap(self):
        out = run_tool(analysis, "analyze_server_roles", self._client(), server_type="relay")
        assert out.startswith("Server Classification (1 hosts): relay: 1") and "srv-aq9001" in out, out
        out = run_tool(analysis, "analyze_server_roles", self._client(), country="aq")
        assert out.startswith("Server Classification (2 hosts): endpoint: 1, relay: 1"), out
        out = run_tool(analysis, "analyze_server_roles", self._client(), product="app_paid")
        assert out.startswith("Server Classification (1 hosts): idle: 1") and "srv-hm9001" in out, out
        out = run_tool(analysis, "analyze_server_roles", self._client(), max_results=2)
        assert out.endswith("*3 more hosts omitted*") and "srv-aq9002" not in out, out


LOG = "\n".join([
    json.dumps({"host_id": "srv-aq9001", "r_ip": "192.0.2.10", "d_ip": "203.0.113.9"}),
    json.dumps({"host_id": "srv-aq9001", "r_ip": "192.0.2.10"}),
    json.dumps({"host_id": "srv-aq9002", "r_ip": "192.0.2.99"}),      # same /24 as its interface
    json.dumps({"host_id": "srv-bv9001", "r_ip": "203.0.113.5"}),     # nowhere near it
    json.dumps({"host_id": "ghost-hm9001", "r_ip": "203.0.113.9"}),   # unknown to Zabbix
    "",
    "not json at all",
])


class TestCorrelateLogs:
    HOSTS = [
        _host("1", "srv-aq9001", "192.0.2.10"),
        _host("2", "srv-aq9002", "192.0.2.11"),
        _host("3", "srv-bv9001", "198.51.100.20"),
    ]

    def test_exact_subnet_and_mismatch_verdicts(self):
        out = run_tool(analysis, "correlate_logs", RecordingClient({"host.get": self.HOSTS}), log_data=LOG)
        assert out.startswith("Log Correlation: 4 hosts, 5 events\n"), out
        rows = [ln for ln in out.splitlines() if ln.startswith("| ") and "Log Host" not in ln and "---" not in ln]
        assert rows == [
            "| ghost-hm9001 | NOT FOUND | 203.0.113.9 | — | — | 1 |",
            "| srv-aq9001 | srv-aq9001 | 192.0.2.10 | 192.0.2.10 | ✓ exact | 2 |",
            "| srv-aq9002 | srv-aq9002 | 192.0.2.99 | 192.0.2.11 | ~ /24 | 1 |",
            "| srv-bv9001 | srv-bv9001 | 203.0.113.5 | 198.51.100.20 | ✗ MISMATCH | 1 |",
        ]
        assert out.endswith("**2 IP mismatch(es)** | **1 host(s) not in Zabbix** | 1 unparseable lines skipped")

    def test_line_cap_and_empty_input(self):
        out = run_tool(analysis, "correlate_logs", RecordingClient({"host.get": self.HOSTS}),
                       log_data=LOG, max_lines=2)
        assert out.startswith("Log Correlation: 1 hosts, 2 events\n"), out
        assert "mismatch" not in out
        c = RecordingClient({"host.get": self.HOSTS})
        assert run_tool(analysis, "correlate_logs", c, log_data="\n\nnope\n") == "No valid log entries found."
        assert c.calls == []


class TestAuditHostIps:
    HOSTS = [
        _host("1", "srv-aq9001", "192.0.2.10", "192.0.2.11"),          # one subnet, one provider
        _host("2", "srv-aq9002", "192.0.2.12", "198.51.100.5"),        # two subnets, two providers
        _host("3", "srv-bv9001", "198.51.100.20"),                     # single interface
        _host("4", "srv-hm9001", "203.0.113.30", "127.0.0.1", group="app_paid"),   # loopback ignored
        _host("5", "srv-tf9001", "192.0.2.40", "203.0.113.40"),        # two subnets, same provider
    ]

    def _client(self, groups=()):
        return RecordingClient({"host.get": self.HOSTS, "hostgroup.get": list(groups)})

    def test_flags_name_the_kind_of_split(self):
        c = self._client()
        out = run_tool(analysis, "audit_host_ips", c)
        assert out.startswith("IP Mismatches: 2 hosts with multi-subnet interfaces\n"), out
        rows = [ln for ln in out.splitlines() if ln.startswith("| srv-")]
        assert rows == [
            "| srv-aq9002 | 192.0.2.12, 198.51.100.5 | 2 | Other, Provider A | MULTI-SUBNET + MULTI-PROVIDER |",
            "| srv-tf9001 | 192.0.2.40, 203.0.113.40 | 2 | Other | MULTI-SUBNET |",
        ]
        sent = c.sent("host.get")
        assert sent["selectInterfaces"] == ["ip", "type", "main"] and sent["sortfield"] == "host"

    def test_filters(self):
        out = run_tool(analysis, "audit_host_ips", self._client(), country="tf")
        assert "1 hosts with" in out and "srv-aq9002" not in out, out
        assert run_tool(analysis, "audit_host_ips", self._client(), product="app_paid") == "No IP mismatches found."
        c = self._client(groups=[{"groupid": "7"}])
        run_tool(analysis, "audit_host_ips", c, group="app_free")
        assert c.sent("host.get")["groupids"] == ["7"]
        assert run_tool(analysis, "audit_host_ips", self._client(), group="nope") == "Host group 'nope' not found."


class TestClassifyExternalIps:
    def test_plain_list_grouped_by_provider_and_city(self):
        out = run_tool(analysis, "classify_external_ips", RecordingClient(),
                       input_data="192.0.2.5, 198.51.100.7\n203.0.113.9, bogus, 198.51.100.8")
        assert out.startswith("External IP Distribution: 4 unique IPs, 2 providers\n"), out
        rows = [ln for ln in out.splitlines() if ln.startswith("| ") and "Provider |" not in ln and "---" not in ln]
        assert rows == [
            "| Other | — | 2 | 50% | 192.0.2.5, 203.0.113.9 |",
            "| Provider A | Base, AQ | 2 | 50% | 198.51.100.7, 198.51.100.8 |",
        ]

    def test_json_logs_cap_and_empty(self):
        logs = "\n".join([json.dumps({"d_ip": "198.51.100.7"}), json.dumps({"d_ip": "192.0.2.5"}),
                          json.dumps({"r_ip": "192.0.2.6"}), "{broken"])
        out = run_tool(analysis, "classify_external_ips", RecordingClient(), input_data=logs)
        assert out.startswith("External IP Distribution: 2 unique IPs, 2 providers"), out
        out = run_tool(analysis, "classify_external_ips", RecordingClient(), input_data=logs, max_ips=1)
        assert out.startswith("External IP Distribution: 1 unique IPs, 1 providers") \
            and out.endswith("*1 IPs over limit, not processed*"), out
        assert run_tool(analysis, "classify_external_ips", RecordingClient(),
                        input_data="bogus, also-bogus") == "No valid IPs found in input."


CSV = (
    "ip,billing_name,price_monthly\n"
    "192.0.2.10,srv-aq9001,20\n"      # exactly a Zabbix host
    "192.0.2.77,new aq box,15\n"      # its /24 has hosts
    "198.51.100.9,,8\n"               # another populated /24
    "203.0.113.3,ext,4\n"             # nothing nearby
    "not-an-ip,x,1\n"
)


class TestAuditExternalIps:
    HOSTS = [
        _host("1", "srv-aq9001", "192.0.2.10"),
        _host("2", "srv-aq9002", "192.0.2.11"),
        _host("3", "srv-bv9001", "198.51.100.20"),
    ]

    @pytest.fixture(autouse=True)
    def _stub_rdns(self, monkeypatch):
        self.looked_up = []

        def fake(ip):
            self.looked_up.append(ip)
            return (f"rdns-{ip.replace('.', '-')}.example.net", [], [ip])
        monkeypatch.setattr(socket, "gethostbyaddr", fake)

    def _client(self):
        return RecordingClient({"host.get": self.HOSTS})

    def test_csv_input_is_bucketed_by_neighbourhood(self):
        out = run_tool(analysis, "audit_external_ips", self._client(), input_data=CSV)
        assert out.startswith(
            "**Audit: 4 IPs** ($47/mo)\n\n"
            "- In Zabbix (exact): **1** ($20/mo)\n"
            "- In cluster (/24 has hosts): **2** ($23/mo)\n"
            "- Same provider (/16): **0** ($0/mo)\n"
            "- External / untracked: **1** ($4/mo)\n"), out
        rows = [ln for ln in out.splitlines() if ln.startswith("| ") and "$/mo" not in ln and "---" not in ln]
        assert rows == [
            "| 192.0.2.10 | 20 | srv-aq9001 | 2 | 2 | Other | — | already in Zabbix as srv-aq9001 |",
            "| 192.0.2.77 | 15 | new aq box | 2 | 2 | Other | rdns-192-0-2-77.example.net | add to cluster (sample: srv-aq9001) |",
            "| 198.51.100.9 | 8 | — | 1 | 1 | Provider A | rdns-198-51-100-9.example.net | add to cluster (sample: srv-bv9001) |",
            "| 203.0.113.3 | 4 | ext | 0 | 0 | Other | rdns-203-0-113-3.example.net | external infra OR untracked |",
        ]
        # Only addresses Zabbix does not know are resolved.
        assert sorted(self.looked_up) == ["192.0.2.77", "198.51.100.9", "203.0.113.3"]

    def test_other_input_shapes(self, tmp_path):
        c = self._client()
        out = run_tool(analysis, "audit_external_ips", c,
                       input_data=json.dumps({"192.0.2.77": 15, "203.0.113.3": {"price": 4}}))
        assert out.startswith("**Audit: 2 IPs** ($19/mo)"), out
        out = run_tool(analysis, "audit_external_ips", c, input_data=json.dumps(
            ["192.0.2.77", {"ip": "203.0.113.3", "price_monthly": 4, "billing_name": "ext"}]))
        assert out.startswith("**Audit: 2 IPs** ($4/mo)") and "| 203.0.113.3 | 4 | ext |" in out, out
        out = run_tool(analysis, "audit_external_ips", c, input_data="192.0.2.77\n203.0.113.3, 192.0.2.10")
        assert out.startswith("**Audit: 3 IPs** ($0/mo)"), out
        src = tmp_path / "ips.csv"
        src.write_text(CSV)
        out = run_tool(analysis, "audit_external_ips", c, file_path=str(src), min_value=10)
        assert out.startswith("**Audit: 2 IPs** ($35/mo)"), out
        out = run_tool(analysis, "audit_external_ips", c, input_data=CSV, max_results=1)
        assert out.endswith("*3 more IPs not shown*"), out

    def test_unresolvable_rdns_renders_as_blank(self, monkeypatch):
        def fail(_ip):
            raise socket.herror("no PTR")
        monkeypatch.setattr(socket, "gethostbyaddr", fail)
        out = run_tool(analysis, "audit_external_ips", self._client(), input_data="203.0.113.3")
        assert "| 203.0.113.3 | 0 | — | 0 | 0 | Other | — | external infra OR untracked |" in out, out

    def test_bad_input(self):
        c = self._client()
        assert run_tool(analysis, "audit_external_ips", c) == "Provide input_data or file_path."
        assert run_tool(analysis, "audit_external_ips", c, input_data="bogus") == "No valid IPs in input."
        assert run_tool(analysis, "audit_external_ips", c, input_data="{oops").startswith("Failed to parse input:")
        assert c.calls == []
