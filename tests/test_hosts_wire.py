"""Host tools wire test: search filters, CRUD payloads + rollback journal,
name→id resolution in ``get_host``, cluster detection, IP search and the
location search. Fixtures are synthetic."""

import json
import time

import httpx
import pytest

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp import classify as classify_mod
from zbbx_mcp import fetch as fetch_mod
from zbbx_mcp.tools import hosts

NOW = str(int(time.time()) - 60)


class JournalClient(RecordingClient):
    """RecordingClient plus the snapshot-taking journal hook update/delete use."""

    async def snapshot_and_record(self, action, kind, obj_id, note=""):
        self.rollback.append((action, kind, obj_id, note))


def _boom(exc):
    def f(_p):
        raise exc
    return f


def _h(hid, host, ip, group="app_free", name=None, status="0", **extra):
    return {"hostid": hid, "host": host, "name": name or host, "status": status,
            "groups": [{"name": group}], "interfaces": [{"ip": ip}], **extra}


HOSTS = [
    _h("1", "srv-aq9001", "198.51.100.1", active_available="1"),
    _h("2", "srv-aq9002", "198.51.100.2", group="app_paid", name="srv-aq9001 aq9002"),
    _h("3", "srv-bv9003", "203.0.113.3", active_available="2"),
    _h("4", "srv-hm9004", "192.0.2.4", status="1"),
    # Name says TF, but the strict parser cannot read it — an inventory gap.
    _h("5", "srv-9005-tf", "192.0.2.5", inventory={}),
]


def _groups(p):
    names = p["filter"]["name"]
    return [{"groupid": "11"}] if names == ["app_free"] else []


# ---- search_hosts ----------------------------------------------------------

def test_search_hosts_table_request_and_rows():
    c = RecordingClient({"host.get": HOSTS[:4]})
    out = run_tool(hosts, "search_hosts", c, query="srv", tags="role:edge,env")
    assert c.sent("host.get") == {
        "output": ["hostid", "host", "name", "status", "active_available"],
        "selectInterfaces": ["ip"],
        "selectGroups": ["name"],
        "sortfield": "host",
        "tags": [{"tag": "role", "value": "edge", "operator": 0},
                 {"tag": "env", "value": "", "operator": 4}],
        "evaltype": 0,
        "filter": {"status": "0"},
        "search": {"host": "*srv*", "name": "*srv*"},
        "searchWildcardsEnabled": True,
        "searchByAny": True,
        "limit": 50,
    }
    assert out.startswith("**Found: 4 hosts**\n\n| Host | Name | Host ID | Status | IP |")
    assert "| srv-aq9001 | srv-aq9001 | 1 | Enabled | 198.51.100.1 |" in out
    assert "| srv-hm9004 | srv-hm9004 | 4 | Disabled | 192.0.2.4 |" in out


def test_search_hosts_list_format_group_status_and_cap():
    c = RecordingClient({"host.get": HOSTS[:3], "hostgroup.get": _groups})
    out = run_tool(hosts, "search_hosts", c, query="srv-*", group="app_free",
                   status="disabled", format="list", max_results=2)
    p = c.sent("host.get")
    assert p["groupids"] == ["11"] and p["filter"] == {"status": "1"}
    assert p["search"] == {"host": "srv-*", "name": "srv-*"}  # wildcard kept as given
    assert out.startswith("**Found: 3 hosts** (showing first 2)")
    assert "- **srv-aq9001** (srv-aq9001) (hostid: 1) [Enabled] [available]" in out
    assert "srv-bv9003" not in out


def test_search_hosts_status_all_sends_no_filter():
    c = RecordingClient({"host.get": HOSTS[:1]})
    run_tool(hosts, "search_hosts", c, status="all")
    assert "filter" not in c.sent("host.get")


def test_search_hosts_country_and_product_filter_client_side():
    c = RecordingClient({"host.get": HOSTS})
    out = run_tool(hosts, "search_hosts", c, country="AQ", product="app_paid")
    p = c.sent("host.get")
    assert "limit" not in p  # filtered client-side, so no server-side cap
    assert p["selectInventory"]
    assert out.startswith("**Found: 1 hosts** in AQ")
    assert "| srv-aq9002 |" in out and "| srv-aq9001 |" not in out


def test_search_hosts_empty_country_names_the_inventory_gap():
    out = run_tool(hosts, "search_hosts", RecordingClient({"host.get": HOSTS}), country="TF")
    assert out.startswith("No hosts found.")
    assert "1 host(s) look like TF by name but have no resolvable country" in out
    assert "srv-9005-tf" in out


def test_search_hosts_degraded_inputs():
    c = RecordingClient({"hostgroup.get": _groups})
    assert run_tool(hosts, "search_hosts", c, country="Atlantis").startswith("Unknown country: 'Atlantis'")
    assert run_tool(hosts, "search_hosts", c, group="nope") == "Host group 'nope' not found."
    assert not any(m == "host.get" for m, _ in c.calls)
    out = run_tool(hosts, "search_hosts", RecordingClient({"host.get": _boom(ValueError("bad"))}))
    assert out == "Error querying Zabbix: bad"


# ---- create / update / delete ----------------------------------------------

def test_create_host_full_payload_and_journal():
    c = RecordingClient({"host.create": {"hostids": ["9001"]}})
    out = run_tool(hosts, "create_host", c, host="srv-aq9001", group_ids="11, 12",
                   name="Base 1", ip="198.51.100.1", template_ids="501,502",
                   description="new box")
    assert c.calls == [("host.create", {
        "host": "srv-aq9001",
        "groups": [{"groupid": "11"}, {"groupid": "12"}],
        "name": "Base 1",
        "description": "new box",
        "templates": [{"templateid": "501"}, {"templateid": "502"}],
        "interfaces": [{"type": 1, "main": 1, "useip": 1, "ip": "198.51.100.1",
                        "dns": "", "port": "10050"}],
    })]
    assert c.rollback == [("create", "host", "9001", "Created host 'srv-aq9001'")]
    assert out == "Host created. ID: 9001"


def test_create_host_dns_interface_and_minimal():
    c = RecordingClient({"host.create": {"hostids": ["9002"]}})
    run_tool(hosts, "create_host", c, host="srv-bv9002", group_ids="11",
             dns="node.example.com", port="10051")
    assert c.sent("host.create")["interfaces"] == [
        {"type": 1, "main": 1, "useip": 0, "ip": "", "dns": "node.example.com", "port": "10051"}]

    c = RecordingClient({"host.create": {"hostids": ["9003"]}})
    run_tool(hosts, "create_host", c, host="srv-bv9003", group_ids="11")
    assert c.sent("host.create") == {"host": "srv-bv9003", "groups": [{"groupid": "11"}]}


def test_create_host_error_records_nothing():
    c = RecordingClient({"host.create": _boom(ValueError("Host already exists"))})
    out = run_tool(hosts, "create_host", c, host="srv-aq9001", group_ids="11")
    assert out == "Error creating host: Host already exists"
    assert c.rollback == []


def test_update_host_payload_and_journal():
    c = JournalClient()
    out = run_tool(hosts, "update_host", c, host_id="1", name="Renamed", status=1,
                   description="decommission")
    assert c.rollback == [("update", "host", "1", "Updated host 1")]
    assert c.calls == [("host.update", {"hostid": "1", "name": "Renamed", "status": 1,
                                        "description": "decommission"})]
    assert out == "Host 1 updated."

    c = JournalClient()
    run_tool(hosts, "update_host", c, host_id="1", status=0)
    assert c.sent("host.update") == {"hostid": "1", "status": 0}


def test_update_host_error():
    c = JournalClient({"host.update": _boom(httpx.ConnectError("down"))})
    assert run_tool(hosts, "update_host", c, host_id="1", name="x") == "Error updating host: down"


def test_delete_host_payload_journal_and_error():
    c = JournalClient()
    assert run_tool(hosts, "delete_host", c, host_id="4") == "Host 4 deleted."
    assert c.calls == [("host.delete", ["4"])]
    assert c.rollback == [("delete", "host", "4", "Deleted host 4")]

    c = JournalClient({"host.delete": _boom(ValueError("No permissions"))})
    assert run_tool(hosts, "delete_host", c, host_id="4") == "Error deleting host: No permissions"


# ---- get_host ----------------------------------------------------------------

def test_get_host_resolves_name_via_search_fallback_then_brief():
    def host_get(p):
        if "filter" in p:
            return []                          # exact technical name misses
        if "search" in p:
            return [{"hostid": "3"}]           # visible-name search hits
        return [HOSTS[2]]

    c = RecordingClient({"host.get": host_get})
    out = run_tool(hosts, "get_host", c, host_id="bv9003", brief=True)
    sent = [p for m, p in c.calls if m == "host.get"]
    assert sent[0] == {"output": ["hostid"], "filter": {"host": ["bv9003"]}}
    assert sent[1]["search"] == {"host": "bv9003", "name": "bv9003"} and sent[1]["limit"] == 1
    assert sent[2]["hostids"] == ["3"]
    assert "# Host: srv-bv9003" in out
    assert [m for m, _ in c.calls] == ["host.get"] * 3  # brief: no enrichment calls


def test_get_host_not_found_paths():
    assert run_tool(hosts, "get_host", RecordingClient(), host_id="ghost") == "Host 'ghost' not found."
    assert run_tool(hosts, "get_host", RecordingClient(), host_id="77") == "Host '77' not found."
    out = run_tool(hosts, "get_host", RecordingClient({"host.get": _boom(httpx.ReadTimeout("slow"))}),
                   host_id="1")
    assert out == "Error querying Zabbix: slow"


@pytest.fixture
def _dc(monkeypatch):
    monkeypatch.setenv("ZABBIX_DATACENTER_CIDRS",
                       json.dumps({"Provider A": [["198.51.100.0/24", "Southport, AQ"]]}))
    classify_mod._EXTRA_DC_NETS = None
    yield
    classify_mod._EXTRA_DC_NETS = None


def test_get_host_enrichment_location_fallback_cost_and_service(monkeypatch, _dc):
    monkeypatch.setattr(fetch_mod, "KEY_service_PRIMARY", "svc.check")

    def items(p):
        key = p.get("filter", {}).get("key_")
        if key == hosts.COST_FALLBACK_KEY:
            return [{"itemid": "a", "key_": key, "lastvalue": ""},     # never collected
                    {"itemid": "b", "key_": key, "lastvalue": "40"}]
        if key == ["svc.check"]:
            return [{"hostid": "1", "key_": "svc.check", "lastvalue": "1",
                     "state": "0", "lastclock": NOW}]
        return [{"hostid": "1", "key_": "net.if.in[eth0]", "lastvalue": "25000000",
                 "lastclock": NOW}]

    c = RecordingClient({
        "host.get": [HOSTS[0]],
        "usermacro.get": [{"macro": "{$COST_MONTH}", "value": "n/a"},  # unparseable
                          {"macro": "{$BW_LIMIT}", "value": "1000"}],
        "item.get": items,
    })
    out = run_tool(hosts, "get_host", c, host_id="1")
    assert "**Location:** Southport, AQ" in out
    assert "**Traffic in:** 25.0 Mbps" in out
    assert "**Service check:** OK" in out
    assert f"**Cost/month:** 40 _(via item `{hosts.COST_FALLBACK_KEY}`)_" in out
    assert "**BW limit:** 1000 Mbps" in out


def test_get_host_enrichment_failures_are_disclosed(monkeypatch):
    monkeypatch.setattr(fetch_mod, "KEY_service_PRIMARY", "svc.check")
    c = RecordingClient({"host.get": [HOSTS[0]], "usermacro.get": [],
                         "item.get": _boom(httpx.ConnectError("down"))})
    out = run_tool(hosts, "get_host", c, host_id="1")
    assert "# Host: srv-aq9001" in out
    assert "service-check state — `item.get` failed (ConnectError)" in out
    assert "Traffic in:" not in out


def test_get_host_traffic_map_value_error_is_disclosed(monkeypatch):
    async def bad_map(_client, _hids):
        raise ValueError("bad payload")

    monkeypatch.setattr(hosts, "fetch_traffic_map", bad_map)
    out = run_tool(hosts, "get_host", RecordingClient({"host.get": [HOSTS[0]]}), host_id="1")
    assert "current traffic — `item.get` failed (ValueError)" in out


# ---- get_server_clusters ----------------------------------------------------

CLUSTER_HOSTS = [
    _h("1", "srv-aq9001", "198.51.100.1"),
    _h("2", "srv-aq9001-b", "198.51.100.2", name="srv-aq9001 aq9002"),
    _h("3", "srv-aq9001-c", "198.51.100.3", name="srv-aq9001 aq9003"),
    _h("4", "srv-bv9004", "203.0.113.4", group="app_paid"),
    _h("5", "srv-bv9004-b", "203.0.113.5", group="app_paid", name="srv-bv9004 bv9005"),
    _h("6", "srv-hm9006", "192.0.2.6"),
]


def test_server_clusters_groups_and_roles():
    c = RecordingClient({"host.get": CLUSTER_HOSTS, "hostgroup.get": _groups})
    out = run_tool(hosts, "get_server_clusters", c, group="app_free")
    p = c.sent("host.get")
    assert p["groupids"] == ["11"] and p["filter"] == {"status": "0"}
    assert out.startswith("**2 clusters** (5 hosts)")
    assert "**srv-aq9001** (AQ, app_free) — 3 members" in out
    assert "  srv-aq9001 — primary 198.51.100.1" in out
    assert "  srv-aq9001-b — secondary 198.51.100.2" in out
    assert "**srv-bv9004** (BV, app_paid) — 2 members" in out
    assert "srv-hm9006" not in out
    # Bigger cluster listed first.
    assert out.index("srv-aq9001**") < out.index("srv-bv9004**")


def test_server_clusters_country_filter_and_cap():
    c = RecordingClient({"host.get": CLUSTER_HOSTS})
    out = run_tool(hosts, "get_server_clusters", c, country="BV")
    assert c.sent("host.get")["selectInventory"]
    assert out.startswith("**1 clusters** (2 hosts)") and "srv-aq9001" not in out

    out = run_tool(hosts, "get_server_clusters", RecordingClient({"host.get": CLUSTER_HOSTS}),
                   max_results=1)
    assert "*1 more clusters omitted*" in out


def test_server_clusters_empty_and_degraded():
    out = run_tool(hosts, "get_server_clusters", RecordingClient({"host.get": CLUSTER_HOSTS[3:4]}))
    assert out == "No clusters found (1 standalone hosts)."
    c = RecordingClient({"hostgroup.get": _groups})
    assert run_tool(hosts, "get_server_clusters", c, group="nope") == "Host group 'nope' not found."
    assert run_tool(hosts, "get_server_clusters", c, country="ZZZZ").startswith("Unknown country")
    out = run_tool(hosts, "get_server_clusters",
                   RecordingClient({"host.get": _boom(httpx.ConnectError("down"))}))
    assert out == "Error: down"


# ---- search_hosts_by_ip -----------------------------------------------------

IP_HOSTS = [
    {"hostid": "1", "host": "srv-aq9001", "name": "a", "groups": [{"name": "app_free"}],
     "interfaces": [{"ip": ""}, {"ip": "198.51.100.1"}]},
    {"hostid": "2", "host": "srv-aq9002", "name": "b", "groups": [],
     "interfaces": [{"ip": "198.51.100.20"}]},
    {"hostid": "3", "host": "srv-bv9003", "name": "c", "groups": [{"name": "app_paid"}],
     "interfaces": [{"ip": "not-an-ip"}, {"ip": "203.0.113.3"}]},
    {"hostid": "4", "host": "srv-hm9004", "name": "d", "groups": [],
     "interfaces": [{"ip": "192.0.2.4"}]},
]


def test_by_ip_exact_cidr_prefix():
    c = RecordingClient({"host.get": IP_HOSTS})
    out = run_tool(hosts, "search_hosts_by_ip", c, query="198.51.100.1")
    assert c.sent("host.get")["output"] == ["hostid", "host", "name", "status"]
    assert out.startswith("**1 hosts** matching `198.51.100.1`")
    assert "| srv-aq9001 | a | 1 | 198.51.100.1 | AQ | app_free |" in out

    out = run_tool(hosts, "search_hosts_by_ip", RecordingClient({"host.get": IP_HOSTS}),
                   query="203.0.113.0/24")
    assert "| srv-bv9003 | c | 3 | 203.0.113.3 | BV | app_paid |" in out

    out = run_tool(hosts, "search_hosts_by_ip", RecordingClient({"host.get": IP_HOSTS}),
                   query="198.51.100.", max_results=1)
    assert out.startswith("**2 hosts** matching `198.51.100.` (showing 1)")
    assert "*1 more hosts omitted*" in out


def test_by_ip_no_match_and_bad_inputs():
    out = run_tool(hosts, "search_hosts_by_ip", RecordingClient({"host.get": IP_HOSTS}),
                   query="192.0.2.99")
    assert out == "No hosts with IP matching '192.0.2.99'."
    c = RecordingClient()
    assert run_tool(hosts, "search_hosts_by_ip", c, query="  ").startswith("Provide an IP")
    assert run_tool(hosts, "search_hosts_by_ip", c, query="10.0.0.0/99") == "Invalid CIDR: 10.0.0.0/99"
    assert run_tool(hosts, "search_hosts_by_ip", c, query="node.example.com") == \
        "Invalid IP/CIDR/prefix: node.example.com"
    assert c.calls == []
    out = run_tool(hosts, "search_hosts_by_ip",
                   RecordingClient({"host.get": _boom(ValueError("denied"))}), query="192.0.2.1")
    assert out == "Error: denied"


# ---- search_hosts_by_location ------------------------------------------------

LOC_HOSTS = [
    _h("1", "srv-aq9001", "198.51.100.1"),
    _h("2", "srv-aq9002", "198.51.100.2", name="srv-aq9001 aq9002"),
    _h("3", "srv-aq9003", "198.51.100.3", group="app_paid"),
    _h("4", "srv-bv9004", "203.0.113.4"),
]


def _loc_traffic(p):
    mbps = {"1": 120, "2": 30, "3": 75, "4": 10}
    return [{"hostid": h, "key_": "net.if.in[eth0]", "lastvalue": str(v * 1_000_000),
             "lastclock": NOW} for h, v in mbps.items() if h in p["hostids"]]


def test_by_location_country_traffic_role(_dc):
    c = RecordingClient({"host.get": LOC_HOSTS, "item.get": _loc_traffic})
    out = run_tool(hosts, "search_hosts_by_location", c, country="AQ",
                   min_traffic_mbps=50, show_cluster_role=True)
    assert c.sent("host.get")["selectInventory"]
    assert out.startswith("**2 hosts**")
    assert "| Host | Country | Product | IP | City | Traffic Mbps | Role |" in out
    assert "| srv-aq9001 | AQ | app_free | 198.51.100.1 | Southport, AQ | 120.0 | primary |" in out
    assert "| srv-aq9003 | AQ | app_paid | 198.51.100.3 | Southport, AQ | 75.0 | primary |" in out
    assert "srv-aq9002" not in out and "srv-bv9004" not in out


def test_by_location_product_and_cap():
    c = RecordingClient({"host.get": LOC_HOSTS, "item.get": _loc_traffic})
    out = run_tool(hosts, "search_hosts_by_location", c, product="app_free", max_results=2)
    assert "selectInventory" not in c.sent("host.get")
    assert out.startswith("**3 hosts** (showing 2)")
    assert "*1 more hosts omitted*" in out


def test_by_location_group_filter_matches_the_named_group():
    c = RecordingClient({"host.get": LOC_HOSTS, "item.get": _loc_traffic,
                         "hostgroup.get": _groups})
    out = run_tool(hosts, "search_hosts_by_location", c, group="app_free")
    assert out.startswith("**3 hosts**")
    assert "srv-aq9003" not in out  # the app_paid host is excluded


def test_by_location_empty_paths():
    c = RecordingClient({"host.get": LOC_HOSTS, "item.get": _loc_traffic,
                         "hostgroup.get": _groups})
    assert run_tool(hosts, "search_hosts_by_location", c, group="nope") == "Host group 'nope' not found."
    assert run_tool(hosts, "search_hosts_by_location", c, country="Atlantis").startswith("Unknown country")
    assert run_tool(hosts, "search_hosts_by_location", c, country="HM") == "No hosts match the filters."
    assert run_tool(hosts, "search_hosts_by_location", c, min_traffic_mbps=500) == \
        "No hosts with traffic >= 500 Mbps."


def test_by_location_stale_traffic_is_not_counted():
    stale = [{"hostid": "1", "key_": "net.if.in[eth0]", "lastvalue": "120000000",
              "lastclock": "0"}]
    c = RecordingClient({"host.get": LOC_HOSTS[:1], "item.get": stale})
    out = run_tool(hosts, "search_hosts_by_location", c)
    assert "| srv-aq9001 | AQ | app_free | 198.51.100.1 |  | 0.0 |" in out


def test_by_location_error():
    out = run_tool(hosts, "search_hosts_by_location",
                   RecordingClient({"host.get": _boom(httpx.ConnectError("down"))}))
    assert out == "Error: down"
