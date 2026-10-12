"""Wire tests for the rollback tools: history, undo-last, undo-by-index.

Each test seeds a real RollbackLog and asserts the exact restoring API call
and the journal state afterwards. Fixtures are synthetic."""

from tests.wiretest import CaptureMCP, RecordingClient, StubResolver, run_tool
from zbbx_mcp.rollback import Action, RollbackLog
from zbbx_mcp.tools import WRITE_TOOLS, rollback_tools


class JournalClient(RecordingClient):
    def __init__(self, responses=None):
        super().__init__(responses)
        self.rollback_log = RollbackLog()


def _seeded(responses=None):
    c = JournalClient(responses)
    log = c.rollback_log
    log.record(Action.CREATE, "maintenance", "42", {}, "Created maintenance 'Patch'")
    log.record(Action.UPDATE, "host", "1",
               {"hostid": "1", "host": "srv-aq9001", "status": "0", "flags": "0",
                "lastchange": "1", "templateid": "0"}, "Updated host srv-aq9001")
    log.record(Action.DELETE, "trigger", "77",
               {"triggerid": "77", "description": "CPU high", "priority": "4",
                "lastchange": "1", "state": "0", "error": "", "flags": "0"},
               "Deleted trigger 77")
    return c


def _ids(c):
    return [(e.action, e.object_id) for e in c.rollback_log.entries]


# --- get_rollback_history -------------------------------------------------

def test_history_lists_newest_first_with_indices():
    out = run_tool(rollback_tools, "get_rollback_history", _seeded())
    assert out.startswith("**Rollback history (3 entries)**")
    i2, i1, i0 = out.index("**#2**"), out.index("**#1**"), out.index("**#0**")
    assert i2 < i1 < i0
    assert "`delete` **trigger** 77\n  Deleted trigger 77 (snapshot: yes)" in out
    assert "`create` **maintenance** 42\n  Created maintenance 'Patch' (snapshot: no)" in out


def test_history_empty():
    out = run_tool(rollback_tools, "get_rollback_history", JournalClient())
    assert out == "No rollback history. Write operations will appear here."


# --- rollback_last --------------------------------------------------------

def test_rollback_last_delete_recreates_without_readonly_fields():
    c = _seeded({"trigger.create": {"triggerids": ["78"]}})
    out = run_tool(rollback_tools, "rollback_last", c)
    assert c.calls == [("trigger.create", {"description": "CPU high", "priority": "4"})]
    assert out == "Rolled back DELETE: re-created trigger as 78 (was 77). (Deleted trigger 77)"
    assert _ids(c) == [(Action.CREATE, "42"), (Action.UPDATE, "1")]


def test_rollback_last_update_restores_snapshot():
    c = _seeded()
    c.rollback_log.pop_last()
    out = run_tool(rollback_tools, "rollback_last", c)
    assert c.calls == [("host.update", {"hostid": "1", "host": "srv-aq9001", "status": "0"})]
    assert out == ("Rolled back UPDATE: restored host 1 to previous state. "
                   "(Updated host srv-aq9001)")
    assert _ids(c) == [(Action.CREATE, "42")]


def test_rollback_last_create_deletes_object():
    c = JournalClient()
    c.rollback_log.record(Action.CREATE, "maintenance", "42", {}, "Created maintenance 'Patch'")
    out = run_tool(rollback_tools, "rollback_last", c)
    assert c.calls == [("maintenance.delete", ["42"])]
    assert out == "Rolled back CREATE: deleted maintenance 42. (Created maintenance 'Patch')"
    assert c.rollback_log.entries == []


def test_rollback_last_empty_and_unsupported_and_missing_snapshot():
    assert run_tool(rollback_tools, "rollback_last", JournalClient()) == "Nothing to roll back."

    c = JournalClient()
    c.rollback_log.record(Action.UPDATE, "dashboard", "3", {"x": 1})
    assert run_tool(rollback_tools, "rollback_last", c) == \
        "Rollback not supported for object type 'dashboard'."

    c = JournalClient()
    c.rollback_log.record(Action.UPDATE, "item", "3", {})
    assert run_tool(rollback_tools, "rollback_last", c) == \
        "Cannot roll back: no snapshot was captured for this update."

    c.rollback_log.record(Action.DELETE, "item", "4", {})
    assert run_tool(rollback_tools, "rollback_last", c) == \
        "Cannot roll back: no snapshot was captured before deletion."
    assert c.calls == []


def test_rollback_last_delete_with_empty_create_result():
    c = JournalClient({"item.create": {"itemids": []}})
    c.rollback_log.record(Action.DELETE, "item", "4", {"itemid": "4", "name": "x", "lastvalue": "1"})
    out = run_tool(rollback_tools, "rollback_last", c)
    assert c.sent("item.create") == {"name": "x"}
    assert out.startswith("Rolled back DELETE: re-created item as ? (was 4).")


def test_rollback_last_api_error():
    def boom(_):
        raise ValueError("Zabbix API error: object exists")
    c = JournalClient({"maintenance.delete": boom})
    c.rollback_log.record(Action.CREATE, "maintenance", "42", {})
    out = run_tool(rollback_tools, "rollback_last", c)
    assert out == "Rollback failed: Zabbix API error: object exists"


# --- rollback_by_index ----------------------------------------------------

def test_by_index_update_middle_entry_and_removes_only_it():
    c = _seeded()
    out = run_tool(rollback_tools, "rollback_by_index", c, index=1)
    assert c.calls == [("host.update", {"hostid": "1", "host": "srv-aq9001", "status": "0"})]
    assert out == "Rolled back UPDATE #1: restored host 1."
    assert _ids(c) == [(Action.CREATE, "42"), (Action.DELETE, "77")]


def test_by_index_create_and_delete():
    c = _seeded({"trigger.create": {"triggerids": ["78"]}})
    out = run_tool(rollback_tools, "rollback_by_index", c, index=0)
    assert out == "Rolled back CREATE #0: deleted maintenance 42."
    assert c.calls[-1] == ("maintenance.delete", ["42"])
    out = run_tool(rollback_tools, "rollback_by_index", c, index=1)  # trigger delete is now #1
    assert c.calls[-1] == ("trigger.create", {"description": "CPU high", "priority": "4"})
    assert out == "Rolled back DELETE #1: re-created trigger as 78."
    assert _ids(c) == [(Action.UPDATE, "1")]


def test_by_index_guards():
    assert run_tool(rollback_tools, "rollback_by_index", JournalClient(), index=0) == "No rollback history."
    c = _seeded()
    assert run_tool(rollback_tools, "rollback_by_index", c, index=3) == "Invalid index 3. Valid range: 0–2."
    assert run_tool(rollback_tools, "rollback_by_index", c, index=-1) == "Invalid index -1. Valid range: 0–2."

    c = JournalClient()
    c.rollback_log.record(Action.CREATE, "dashboard", "3", {})
    c.rollback_log.record(Action.UPDATE, "item", "4", {})
    c.rollback_log.record(Action.DELETE, "item", "5", {})
    assert run_tool(rollback_tools, "rollback_by_index", c, index=0) == "Rollback not supported for 'dashboard'."
    assert run_tool(rollback_tools, "rollback_by_index", c, index=1) == "Cannot roll back #1: no snapshot."
    assert run_tool(rollback_tools, "rollback_by_index", c, index=2) == "Cannot roll back #2: no snapshot."
    assert c.calls == [] and len(c.rollback_log) == 3


def test_by_index_api_error_keeps_entry():
    def boom(_):
        raise ValueError("Zabbix API error: denied")
    c = _seeded({"host.update": boom})
    out = run_tool(rollback_tools, "rollback_by_index", c, index=1)
    assert out == "Rollback failed: Zabbix API error: denied"
    assert len(c.rollback_log) == 3


def test_rollback_tools_gated_by_read_only():
    assert {"rollback_last", "rollback_by_index"} <= WRITE_TOOLS
    mcp = CaptureMCP()
    rollback_tools.register(mcp, StubResolver(JournalClient()), skip=set(WRITE_TOOLS))
    assert set(mcp.fns) == {"get_rollback_history"}
