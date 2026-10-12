"""Wire tests for maintenance windows: read, create, delete (synthetic fixtures)."""

from tests.wiretest import CaptureMCP, RecordingClient, StubResolver, run_tool
from zbbx_mcp.client import ZabbixClient
from zbbx_mcp.rollback import Action, RollbackLog
from zbbx_mcp.tools import WRITE_TOOLS, maintenance

SINCE = 1_800_000_000
TILL = SINCE + 7200


class JournalClient(RecordingClient):
    """RecordingClient wired to the real rollback journal logic."""

    snapshot = ZabbixClient.snapshot
    snapshot_and_record = ZabbixClient.snapshot_and_record
    record_create = ZabbixClient.record_create

    def __init__(self, responses=None):
        super().__init__(responses)
        self.rollback_log = RollbackLog()


WINDOWS = [
    {"maintenanceid": "5", "name": "Kernel upgrade", "maintenance_type": "0",
     "active_since": str(SINCE), "active_till": str(TILL),
     "hosts": [{"hostid": "1"}, {"hostid": "2"}], "groups": [{"groupid": "7"}],
     "description": "r" * 120},
    {"maintenanceid": "6", "name": "Rack move", "maintenance_type": "1",
     "active_since": str(SINCE), "active_till": str(TILL)},
    {"maintenanceid": "8", "name": "Weird", "maintenance_type": "4"},
]


def test_get_maintenance_by_group_resolves_ids_and_formats():
    c = RecordingClient({"hostgroup.get": [{"groupid": "7"}], "maintenance.get": WINDOWS})
    out = run_tool(maintenance, "get_maintenance", c, host_id="1", group="app_free")
    assert c.sent("hostgroup.get") == {"output": ["groupid"], "filter": {"name": ["app_free"]}}
    p = c.sent("maintenance.get")
    assert p["hostids"] == ["1"] and p["groupids"] == ["7"]
    assert p["selectHosts"] == ["hostid", "host"] and p["selectTimeperiods"] == "extend"
    assert out.startswith("**Found: 3 maintenance windows**")
    assert ("- **Kernel upgrade** (With data collection)\n"
            "  2027-01-15 08:00 UTC → 2027-01-15 10:00 UTC | 2 hosts, 1 groups (id: 5)\n  "
            + "r" * 100) in out
    assert "r" * 101 not in out
    assert "- **Rack move** (Without data collection)" in out
    assert "- **Weird** (?)\n  1970-01-01 00:00 UTC → 1970-01-01 00:00 UTC | 0 hosts, 0 groups (id: 8)" in out


def test_get_maintenance_unknown_group_stops_early():
    c = RecordingClient({"hostgroup.get": []})
    out = run_tool(maintenance, "get_maintenance", c, group="app_paid")
    assert out == "Host group 'app_paid' not found."
    assert [m for m, _ in c.calls] == ["hostgroup.get"]


def test_get_maintenance_empty_and_error():
    assert run_tool(maintenance, "get_maintenance", RecordingClient()) == "No maintenance windows found."

    def boom(_):
        raise ValueError("Zabbix API error: denied")
    out = run_tool(maintenance, "get_maintenance", RecordingClient({"maintenance.get": boom}))
    assert out == "Error querying Zabbix: Zabbix API error: denied"


def test_create_maintenance_exact_payload_and_journal():
    c = JournalClient({"maintenance.create": {"maintenanceids": ["42"]}})
    out = run_tool(maintenance, "create_maintenance", c, name="Patch", active_since=str(SINCE),
                   active_till=str(TILL), host_ids="1, 2,", group_ids="7",
                   collect_data=False, description="planned")
    assert c.calls == [("maintenance.create", {
        "name": "Patch",
        "active_since": SINCE,
        "active_till": TILL,
        "maintenance_type": 1,
        "timeperiods": [{"timeperiod_type": 0, "period": 7200}],
        "hosts": [{"hostid": "1"}, {"hostid": "2"}],
        "groups": [{"groupid": "7"}],
        "description": "planned",
    })]
    assert out == "Maintenance window created. ID: 42"
    [e] = c.rollback_log.entries
    assert (e.action, e.object_type, e.object_id, e.snapshot, e.description) == \
        (Action.CREATE, "maintenance", "42", {}, "Created maintenance 'Patch'")


def test_create_maintenance_iso_dates_minimal_payload():
    c = JournalClient({"maintenance.create": {"maintenanceids": ["43"]}})
    run_tool(maintenance, "create_maintenance", c, name="Day",
             active_since="2027-01-15", active_till="2027-01-16", host_ids="1")
    p = c.sent("maintenance.create")
    assert p["maintenance_type"] == 0
    assert p["timeperiods"] == [{"timeperiod_type": 0, "period": 86400}]
    assert "groups" not in p and "description" not in p
    # deprecated id-list keys must never be sent (removed in 7.2)
    assert "hostids" not in p and "groupids" not in p


def test_create_maintenance_validation_sends_nothing():
    c = JournalClient()
    bad = run_tool(maintenance, "create_maintenance", c, name="x", active_since="soon", active_till="later")
    assert bad.startswith("Invalid timestamps.")
    rev = run_tool(maintenance, "create_maintenance", c, name="x", active_since=str(TILL), active_till=str(SINCE))
    assert rev == "active_till must be after active_since."
    long = run_tool(maintenance, "create_maintenance", c, name="x",
                    active_since=str(SINCE), active_till=str(SINCE + 366 * 86400))
    assert long == "Maintenance window cannot exceed 1 year."
    assert c.calls == [] and c.rollback_log.entries == []


def test_create_maintenance_api_error_records_nothing():
    def boom(_):
        raise ValueError("Zabbix API error: at least one host or group is required")
    c = JournalClient({"maintenance.create": boom})
    out = run_tool(maintenance, "create_maintenance", c, name="x",
                   active_since=str(SINCE), active_till=str(TILL))
    assert out == "Error creating maintenance: Zabbix API error: at least one host or group is required"
    assert c.rollback_log.entries == []


def test_delete_maintenance_snapshots_before_delete():
    snap = {"maintenanceid": "5", "name": "Kernel upgrade", "hosts": [{"hostid": "1"}]}
    c = JournalClient({"maintenance.get": [snap], "maintenance.delete": {"maintenanceids": ["5"]}})
    out = run_tool(maintenance, "delete_maintenance", c, maintenance_id="5")
    assert out == "Maintenance window 5 deleted."
    assert c.calls == [
        ("maintenance.get", {"maintenanceids": ["5"], "output": "extend",
                             "selectHosts": ["hostid"], "selectGroups": ["groupid"],
                             "selectTimeperiods": "extend"}),
        ("maintenance.delete", ["5"]),
    ]
    [e] = c.rollback_log.entries
    assert (e.action, e.object_type, e.object_id, e.snapshot, e.description) == \
        (Action.DELETE, "maintenance", "5", snap, "Deleted maintenance 5")


def test_delete_maintenance_error():
    def boom(_):
        raise ValueError("Zabbix API error: no such object")
    c = JournalClient({"maintenance.delete": boom})
    out = run_tool(maintenance, "delete_maintenance", c, maintenance_id="9")
    assert out == "Error deleting maintenance: Zabbix API error: no such object"


def test_write_tools_gated_by_read_only():
    assert {"create_maintenance", "delete_maintenance"} <= WRITE_TOOLS
    mcp = CaptureMCP()
    maintenance.register(mcp, StubResolver(RecordingClient()), skip=set(WRITE_TOOLS))
    assert set(mcp.fns) == {"get_maintenance"}
