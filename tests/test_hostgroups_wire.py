"""Host-group tools wire test: query shape, create/delete payloads and the
rollback journal each write leaves behind. Fixtures are synthetic."""

import httpx

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp.tools import hostgroups


class JournalClient(RecordingClient):
    """RecordingClient; write tools journal into ``rollback`` via ``journal``."""


GROUPS = [
    {"groupid": "11", "name": "app_free", "hosts": [{"hostid": "1"}, {"hostid": "2"}]},
    {"groupid": "12", "name": "app_paid", "hosts": [{"hostid": "3"}]},
    {"groupid": "13", "name": "Templates", "hosts": []},
]


def test_get_hostgroups_plain_request_and_output():
    c = RecordingClient({"hostgroup.get": [{"groupid": "11", "name": "app_free"},
                                           {"groupid": "12", "name": "app_paid"}]})
    out = run_tool(hostgroups, "get_hostgroups", c)
    assert c.sent("hostgroup.get") == {"output": ["groupid", "name"], "sortfield": "name"}
    assert out == ("**Found: 2 host groups**\n\n"
                   "- **app_free** (groupid: 11)\n- **app_paid** (groupid: 12)")


def test_get_hostgroups_query_and_hosts():
    c = RecordingClient({"hostgroup.get": GROUPS})
    out = run_tool(hostgroups, "get_hostgroups", c, query="app_*", include_hosts=True)
    assert c.sent("hostgroup.get") == {
        "output": ["groupid", "name"], "sortfield": "name",
        "search": {"name": "app_*"}, "searchWildcardsEnabled": True,
        "selectHosts": ["hostid", "host", "name"],
    }
    assert "**Found: 3 host groups**" in out
    assert "- **app_free** (groupid: 11) (2 hosts)" in out
    assert "- **Templates** (groupid: 13) (0 hosts)" in out


def test_get_hostgroups_empty():
    out = run_tool(hostgroups, "get_hostgroups", RecordingClient(), query="nothing")
    assert out == "No host groups found."


def test_get_hostgroups_error():
    def boom(_p):
        raise ValueError("Zabbix API error: no permissions")

    out = run_tool(hostgroups, "get_hostgroups", RecordingClient({"hostgroup.get": boom}))
    assert out == "Error querying Zabbix: Zabbix API error: no permissions"


def test_create_hostgroup_payload_and_journal():
    c = RecordingClient({"hostgroup.create": {"groupids": ["21"]}})
    out = run_tool(hostgroups, "create_hostgroup", c, name="app_free")
    assert c.calls == [("hostgroup.create", {"name": "app_free"})]
    assert c.rollback == [("create", "hostgroup", "21", "Created host group 'app_free'")]
    assert out == "Host group created. ID: 21"


def test_create_hostgroup_error_leaves_no_journal_entry():
    def boom(_p):
        raise ValueError("Host group already exists")

    c = RecordingClient({"hostgroup.create": boom})
    out = run_tool(hostgroups, "create_hostgroup", c, name="app_free")
    assert out == "Error creating host group: Host group already exists"
    assert c.rollback == []


def test_delete_hostgroup_snapshots_before_delete():
    c = JournalClient()
    out = run_tool(hostgroups, "delete_hostgroup", c, group_id="21")
    assert c.calls == [("hostgroup.delete", ["21"])]
    assert c.rollback == [("delete", "hostgroup", "21", "Deleted host group 21")]
    assert out == "Host group 21 deleted."


def test_delete_hostgroup_error():
    def boom(_p):
        raise httpx.ReadTimeout("timed out")

    c = JournalClient({"hostgroup.delete": boom})
    out = run_tool(hostgroups, "delete_hostgroup", c, group_id="21")
    assert out == "Error deleting host group: timed out"
