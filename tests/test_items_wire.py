"""Wire tests for the item tools: exact ``item.*`` / ``history.get`` /
``graph.get`` params, rollback journal entries for writes, and rendered
output. Fixtures are synthetic."""

import time

import httpx

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp.tools import items
from zbbx_mcp.tools.items import _collapse_dependent_chain, _parse_delay_seconds

NOW = int(time.time())
LIVE = str(NOW - 60)        # recent lastclock: a live reading (ADR 141)


class JournalClient(RecordingClient):
    """RecordingClient; write tools journal into ``rollback`` via ``journal``."""


class FailingClient(JournalClient):
    async def call(self, method, params):
        self.calls.append((method, params))
        raise httpx.ConnectError("connection refused")


# --- pure helpers ---------------------------------------------------------

class TestHelpers:
    def test_parse_delay(self):
        assert _parse_delay_seconds("") == 300
        assert _parse_delay_seconds("60") == 60
        assert _parse_delay_seconds("30s") == 30
        assert _parse_delay_seconds("5m") == 300
        assert _parse_delay_seconds("1H") == 3600
        assert _parse_delay_seconds("1d") == 86400
        assert _parse_delay_seconds("30s;wd1-5,9:00-18:00/1m") == 30
        assert _parse_delay_seconds("{$DELAY}", default=120) == 120

    def test_collapse_two_hop_chain_and_cycle(self):
        stale = [
            {"itemid": "1", "master_itemid": ""},
            {"itemid": "2", "master_itemid": "1"},
            {"itemid": "3", "master_itemid": "2"},       # grandchild folds into root
            {"itemid": "4", "master_itemid": "99"},      # master not stale: own root
            {"itemid": "5", "master_itemid": "6"},       # 5 <-> 6 cycle
            {"itemid": "6", "master_itemid": "5"},
        ]
        out = {s["itemid"]: s["affected_count"] for s in _collapse_dependent_chain(stale)}
        assert out["1"] == 2 and out["4"] == 0
        assert "2" not in out and "3" not in out
        # A 2-cycle terminates; each member is its own root, nothing folded.
        assert out["5"] == 0 and out["6"] == 0
        assert stale[0] == {"itemid": "1", "master_itemid": ""}   # input not mutated


# --- create / update / delete --------------------------------------------

class TestItemWrites:
    def test_create_payload_and_journal(self):
        c = JournalClient({"item.create": {"itemids": ["9601"]}})
        out = run_tool(items, "create_item", c, host_id="9001", name="Inbound",
                       key="net.if.in[eth0]", value_type=3, item_type=0, delay="5m",
                       units="bps", description="carrier NIC")
        assert out == "Item created. ID: 9601"
        assert c.sent("item.create") == {
            "hostid": "9001", "name": "Inbound", "key_": "net.if.in[eth0]",
            "type": 0, "value_type": 3, "delay": "5m",
            "units": "bps", "description": "carrier NIC",
        }
        assert c.rollback == [("create", "item", "9601", "Created item 'Inbound'")]

    def test_create_defaults(self):
        c = JournalClient({"item.create": {"itemids": ["9602"]}})
        run_tool(items, "create_item", c, host_id="9001", name="n", key="k")
        assert c.sent("item.create") == {
            "hostid": "9001", "name": "n", "key_": "k",
            "type": 7, "value_type": 0, "delay": "60s",
        }

    def test_create_api_error_records_nothing(self):
        c = FailingClient()
        out = run_tool(items, "create_item", c, host_id="9001", name="n", key="k")
        assert out.startswith("Error creating item:") and c.rollback == []

    def test_update_payload_and_journal(self):
        c = JournalClient()
        out = run_tool(items, "update_item", c, item_id="9601", name="In",
                       delay="1m", status=1, description="d")
        assert out == "Item 9601 updated."
        assert c.rollback == [("update", "item", "9601", "Updated item 9601")]
        assert c.sent("item.update") == {
            "itemid": "9601", "name": "In", "delay": "1m", "status": 1, "description": "d",
        }

    def test_update_only_id(self):
        c = JournalClient()
        run_tool(items, "update_item", c, item_id="9601")
        assert c.sent("item.update") == {"itemid": "9601"}

    def test_update_api_error(self):
        assert run_tool(items, "update_item", FailingClient(), item_id="1").startswith(
            "Error updating item:")

    def test_delete_payload_and_journal(self):
        c = JournalClient()
        assert run_tool(items, "delete_item", c, item_id="9601") == "Item 9601 deleted."
        assert c.sent("item.delete") == ["9601"]
        assert c.rollback == [("delete", "item", "9601", "Deleted item 9601")]

    def test_delete_api_error(self):
        assert run_tool(items, "delete_item", FailingClient(), item_id="1").startswith(
            "Error deleting item:")


# --- get_host_items -------------------------------------------------------

HOST_ITEMS = [
    {"itemid": "1", "name": "CPU idle", "key_": "system.cpu.util[,idle]", "lastvalue": "62.5",
     "units": "%", "lastclock": LIVE, "state": "0"},
    {"itemid": "2", "name": "Inbound", "key_": "net.if.in[eth0]", "lastvalue": "",
     "units": "bps", "lastclock": "0", "state": "0"},
    {"itemid": "3", "name": "Broken", "key_": "custom.check", "lastvalue": "1",
     "units": "", "lastclock": LIVE, "state": "1"},
]


class TestGetHostItems:
    def test_happy_path(self):
        c = RecordingClient({"item.get": HOST_ITEMS})
        out = run_tool(items, "get_host_items", c, host_id="9001")
        p = c.sent("item.get")
        assert p["hostids"] == ["9001"] and p["filter"] == {"status": "0"}
        assert p["limit"] == 50 and "search" not in p
        assert out.startswith("**Found: 3 items**")
        assert "**Inbound** = N/A" in out
        assert "id: 2 | updated: never" in out and "1970" not in out
        assert "**Broken** = 1 [UNSUPPORTED]" in out
        assert "key: `system.cpu.util[,idle]` | id: 1" in out

    def test_search_wrapped_and_limit_note(self):
        c = RecordingClient({"item.get": HOST_ITEMS[:1]})
        out = run_tool(items, "get_host_items", c, host_id="9001", search="cpu", max_results=1)
        p = c.sent("item.get")
        assert p["search"] == {"name": "*cpu*", "key_": "*cpu*"}
        assert p["searchByAny"] is True and p["searchWildcardsEnabled"] is True
        assert "(showing first 1, more may exist)" in out
        c2 = RecordingClient({"item.get": []})
        assert run_tool(items, "get_host_items", c2, host_id="9001", search="net.*") == \
            "No items found."
        assert c2.sent("item.get")["search"] == {"name": "net.*", "key_": "net.*"}

    def test_api_error(self):
        assert run_tool(items, "get_host_items", FailingClient(), host_id="1").startswith(
            "Error querying Zabbix:")


# --- search_items ---------------------------------------------------------

HOSTS = [
    {"hostid": "1", "host": "srv-aq9001", "groups": [{"name": "app_free"}]},
    {"hostid": "2", "host": "srv-aq9002", "groups": [{"name": "app_paid"}]},
    {"hostid": "3", "host": "srv-bv9001", "groups": [{"name": "app_free"}]},
    {"hostid": "4", "host": "srv-aq9003-test", "groups": [{"name": "app_free"}]},
]
FOUND = [
    {"itemid": "11", "hostid": "1", "name": "Inbound", "key_": "net.if.in[eth0]",
     "lastvalue": "8000000", "lastclock": LIVE, "units": "bps"},
    {"itemid": "12", "hostid": "2", "name": "Inbound", "key_": "net.if.in[eth0]",
     "lastvalue": "", "lastclock": "0", "units": "bps"},
]


class TestSearchItems:
    def test_happy_path_excludes_test_hosts(self):
        c = RecordingClient({"host.get": HOSTS, "item.get": FOUND})
        out = run_tool(items, "search_items", c, search="net.if.in")
        hp = c.sent("host.get")
        assert hp["selectGroups"] == ["name"] and hp["filter"] == {"status": "0"}
        ip = c.sent("item.get")
        assert ip["hostids"] == ["1", "2", "3"]
        assert ip["search"] == {"name": "*net.if.in*", "key_": "*net.if.in*"}
        assert ip["limit"] == 30
        assert "**2 items** matching 'net.if.in' across 3 hosts" in out
        assert "| srv-aq9001 | Inbound | `net.if.in[eth0]` |" in out
        # lastclock "0" means never collected (ADR 141): "never", not the epoch.
        assert "| srv-aq9002 | Inbound | `net.if.in[eth0]` | N/A | never |" in out
        assert "1970" not in out
        assert "srv-aq9003-test" in out          # excluded host is named, not hidden

    def test_country_group_and_include_test(self):
        c = RecordingClient({
            "hostgroup.get": [{"groupid": "21"}],
            "host.get": HOSTS,
            "item.get": FOUND[:1],
        })
        out = run_tool(items, "search_items", c, search="net*", group="app_free",
                       country="AQ", include_test=True, max_results=1)
        assert c.sent("host.get")["groupids"] == ["21"]
        ip = c.sent("item.get")
        assert ip["hostids"] == ["1", "2", "4"]
        assert ip["search"] == {"name": "net*", "key_": "net*"}
        assert "(limit 1)" in out and "across 3 hosts" in out

    def test_unknown_group(self):
        c = RecordingClient({"hostgroup.get": []})
        assert run_tool(items, "search_items", c, search="x", group="app_paid") == \
            "Host group 'app_paid' not found."
        assert "host.get" not in [m for m, _ in c.calls]

    def test_no_hosts_and_no_items(self):
        c = RecordingClient({"host.get": HOSTS[3:]})
        out = run_tool(items, "search_items", c, search="x")
        assert out.startswith("No hosts match the filter.") and "srv-aq9003-test" in out
        assert "item.get" not in [m for m, _ in c.calls]
        c2 = RecordingClient({"host.get": HOSTS[:3], "item.get": []})
        assert run_tool(items, "search_items", c2, search="x") == \
            "No items matching 'x' found across 3 hosts."

    def test_api_error(self):
        out = run_tool(items, "search_items", FailingClient(), search="x")
        assert out.startswith("Error:") and "connection refused" in out


# --- get_item_history -----------------------------------------------------

class TestItemHistory:
    META = [{"itemid": "11", "name": "Inbound", "key_": "net.if.in[eth0]",
             "units": "bps", "value_type": "3"}]

    def test_happy_path_uses_item_value_type(self):
        hist = [{"clock": LIVE, "value": "2000"}, {"clock": str(NOW - 120), "value": "1000"}]
        c = RecordingClient({"item.get": self.META, "history.get": hist})
        out = run_tool(items, "get_item_history", c, item_id="11", limit=2,
                       time_from="1700000000", time_till="1700086400")
        assert c.sent("item.get")["itemids"] == ["11"]
        assert c.sent("history.get") == {
            "itemids": ["11"], "history": 3, "sortfield": "clock", "sortorder": "DESC",
            "limit": 2, "output": "extend",
            "time_from": 1700000000, "time_till": 1700086400,
        }
        assert "# History: Inbound" in out and "**Records:** 2" in out
        assert out.count("\n| ") == 3       # header + 2 rows

    def test_an_explicit_value_type_overrides_the_item(self):
        # ADR 158: the parameter used to be silently ignored.
        c = RecordingClient({"item.get": self.META, "history.get": []})
        run_tool(items, "get_item_history", c, item_id="11", value_type=4)
        assert c.sent("history.get")["history"] == 4

    def test_an_out_of_range_value_type_falls_back_to_the_item(self):
        c = RecordingClient({"item.get": self.META, "history.get": []})
        run_tool(items, "get_item_history", c, item_id="11", value_type=9)
        assert c.sent("history.get")["history"] == 3

    def test_not_found_empty_and_bad_times(self):
        assert run_tool(items, "get_item_history", RecordingClient(), item_id="404") == \
            "Item '404' not found."
        c = RecordingClient({"item.get": self.META, "history.get": []})
        assert run_tool(items, "get_item_history", c, item_id="11") == \
            "No history data for item 'Inbound'."
        assert "time_from" not in c.sent("history.get")
        c3 = RecordingClient({"item.get": self.META})
        assert run_tool(items, "get_item_history", c3, item_id="11",
                        time_from="garbage").startswith("Invalid time_from:")
        assert run_tool(items, "get_item_history", c3, item_id="11",
                        time_till="garbage").startswith("Invalid time_till:")
        assert "history.get" not in [m for m, _ in c3.calls]

    def test_api_error(self):
        assert run_tool(items, "get_item_history", FailingClient(), item_id="1").startswith(
            "Error querying Zabbix:")


# --- get_stale_items ------------------------------------------------------

def _it(iid, host, name, lastclock, state="0", delay="60s", master="", triggers=None, error=""):
    return {"itemid": iid, "hostid": "h", "name": name, "key_": f"k.{iid}", "state": state,
            "lastclock": str(lastclock), "delay": delay, "error": error,
            "master_itemid": master, "hosts": [{"host": host}], "triggers": triggers or []}


class TestStaleItems:
    def test_reasons_and_trigger_section(self):
        trig_active = {"triggerid": "t1", "description": "Service down", "value": "1", "status": "0"}
        trig_ok = {"triggerid": "t2", "description": "Fine", "value": "0", "status": "0"}
        data = [
            _it("1", "srv-aq9001", "Live", NOW - 60),
            _it("2", "srv-aq9002", "Frozen", NOW - 3600, triggers=[trig_active]),
            _it("3", "srv-bv9001", "Never", 0),
            _it("4", "srv-hm9001", "Unsup", NOW - 30, state="1", error="bad key",
                triggers=[trig_ok]),
        ]
        c = RecordingClient({"item.get": data})
        out = run_tool(items, "get_stale_items", c, host_id="9001")
        p = c.sent("item.get")
        assert p["hostids"] == ["9001"] and p["limit"] == 5000
        assert p["selectTriggers"] == ["triggerid", "description", "value", "status"]
        assert "master_itemid" not in p["output"]
        assert out.startswith("**3 stale items** (checked 4; threshold 3.0x delay)")
        assert "Live" not in out
        assert "| srv-aq9002 | Frozen | `k.2` | frozen 60m (expected ≤ 3m) |" in out
        assert "| srv-bv9001 | Never | `k.3` | no data | never |" in out
        assert "| srv-hm9001 | Unsup | `k.4` | UNSUPPORTED |" in out
        assert "**Triggers depending on stale items:**" in out
        assert "  - [PROBLEM] Service down (triggerid: t1) — monitoring broken, not service" in out
        assert "Fine" not in out

    def test_collapse_group_and_overflow(self):
        data = [
            _it("1", "srv-aq9001", "Master", 0),
            _it("2", "srv-aq9001", "Child", 0, master="1"),
            _it("3", "srv-aq9002", "Other", 0),
            _it("4", "srv-aq9003", "Third", 0),
        ]
        c = RecordingClient({"hostgroup.get": [{"groupid": "31"}], "item.get": data})
        out = run_tool(items, "get_stale_items", c, group="app_free", include_triggers=False,
                       collapse_dependencies=True, max_results=2)
        p = c.sent("item.get")
        assert p["groupids"] == ["31"] and "selectTriggers" not in p
        assert "master_itemid" in p["output"]
        assert "**3 stale items** (1 downstream collapsed)" in out
        assert "| Last value | Affected |" in out
        assert "| srv-aq9001 | Master | `k.1` | no data | never | +1 |" in out
        assert "| — |" in out
        assert "*1 more stale items omitted*" in out
        assert "Triggers depending" not in out

    def test_unknown_group_and_nothing_stale(self):
        c = RecordingClient({"hostgroup.get": []})
        assert run_tool(items, "get_stale_items", c, group="app_paid") == \
            "Host group 'app_paid' not found."
        c2 = RecordingClient({"item.get": [_it("1", "srv-aq9001", "Live", NOW - 10)]})
        assert run_tool(items, "get_stale_items", c2) == \
            "No stale items found (checked 1 items, threshold 3.0x delay)."

    def test_api_error(self):
        out = run_tool(items, "get_stale_items", FailingClient())
        assert out.startswith("Error:") and "connection refused" in out


# --- get_graphs -----------------------------------------------------------

class TestGetGraphs:
    def test_happy_path_and_search(self):
        g = [
            {"graphid": "71", "name": "Traffic", "graphtype": "1",
             "gitems": [{"itemid": "1"}, {"itemid": "2"}]},
            {"graphid": "72", "name": "CPU", "graphtype": "0", "gitems": [{"itemid": "3"}]},
            {"graphid": "73", "name": "Share", "graphtype": "9"},
        ]
        c = RecordingClient({"graph.get": g})
        out = run_tool(items, "get_graphs", c, host_id="9001", search="T*", max_results=3)
        p = c.sent("graph.get")
        assert p["hostids"] == ["9001"] and p["search"] == {"name": "T*"}
        assert p["selectGraphItems"] == ["itemid", "color", "drawtype"]
        assert "**Found: 3 graphs** (showing first 3, more may exist)" in out
        assert "- **Traffic** (Stacked, 2 items) — graphid: 71" in out
        assert "- **CPU** (Normal, 1 items) — graphid: 72" in out
        assert "- **Share** (?, 0 items) — graphid: 73" in out

    def test_empty_and_error(self):
        c = RecordingClient({"graph.get": []})
        assert run_tool(items, "get_graphs", c, host_id="9001") == "No graphs found."
        assert "search" not in c.sent("graph.get")
        assert run_tool(items, "get_graphs", FailingClient(), host_id="1").startswith(
            "Error querying Zabbix:")
