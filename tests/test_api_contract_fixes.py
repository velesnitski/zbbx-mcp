"""Wire-contract tests for the ADR 088 silent-degradation fixes.

These fields were removed/renamed in modern Zabbix and, on this instance,
silently returned nothing rather than erroring — so the column just went blank.
Each fix is pinned at the wire so it can't regress.
"""

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp.tools import discovery as discovery_mod
from zbbx_mcp.tools import domains as domains_mod
from zbbx_mcp.tools import hosts as hosts_mod


class TestSearchHostsAvailability:
    HOST = {"hostid": "1", "host": "w1", "name": "W1", "status": "0",
            "active_available": "1", "interfaces": [{"ip": "10.0.0.1"}],
            "groups": [{"name": "Web"}]}

    def test_requests_active_available_not_available(self):
        client = RecordingClient({"host.get": [self.HOST]})
        run_tool(hosts_mod, "search_hosts", client, query="w")
        out = client.sent("host.get")["output"]
        assert "available" not in out       # removed from the host object in 6.0
        assert "active_available" in out

    def test_available_flag_still_renders_in_list_format(self):
        client = RecordingClient({"host.get": [self.HOST]})
        out = run_tool(hosts_mod, "search_hosts", client, query="w", format="list")
        assert "[available]" in out         # format_host_list reads active_available


class TestDiscoveryNoLastclock:
    def test_output_has_no_lastclock_and_no_last_column(self):
        rule = {"itemid": "5", "name": "disc", "key_": "k", "type": "0",
                "status": "0", "state": "0", "hosts": [{"host": "h1"}]}
        client = RecordingClient({"discoveryrule.get": [rule]})
        out = run_tool(discovery_mod, "get_discovery_rules", client)
        assert "lastclock" not in client.sent("discoveryrule.get")["output"]
        assert "last:" not in out            # the permanent "1970" column is gone
        assert "disc" in out


class TestDomainGroupsResolvedSeparately:
    def test_selecthosts_drops_groups_and_resolves_via_host_get(self):
        cert_key = "web_cert_check.sh[{HOST.NAME}]"
        # Dotless host -> skipped in the render loop, so no live domain lookup;
        # the item.get + host.get contract still fires and is what we assert.
        item = {"itemid": "1", "hostid": "9", "key_": cert_key,
                "lastvalue": "30", "hosts": [{"hostid": "9", "host": "nodot"}]}
        client = RecordingClient({
            "item.get": [item],
            "host.get": [{"hostid": "9", "groups": [{"name": "Web Check"}]}],
        })
        run_tool(domains_mod, "get_domain_status", client)
        # item.get selectHosts must NOT nest groups (the -32602 class).
        assert client.sent("item.get")["selectHosts"] == ["hostid", "host"]
        assert "groups" not in client.sent("item.get")["selectHosts"]
        # groups are resolved by a separate host.get with selectGroups instead.
        assert client.sent("host.get").get("selectGroups") == ["name"]
