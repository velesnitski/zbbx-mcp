"""Journal ordering for every update/delete write tool.

The snapshot must be TAKEN before the write (so it holds the prior state), but
the rollback entry must only be COMMITTED after the write succeeded: a failed
write leaves nothing in the journal for `rollback_last` to "undo".
Fixtures are synthetic.
"""

import pytest

from tests.wiretest import JournalingClient, run_tool
from zbbx_mcp.rollback import Action
from zbbx_mcp.tools import hostgroups, hosts, items, macros, maintenance, triggers


def _macro_get(p):
    return [{"hostmacroid": "71", "macro": "{$X}", "value": "1", "type": "0"}]


# (module, tool, kwargs, snapshot get method, write method, object type, id, extra responses)
DELETES = [
    (hosts, "delete_host", {"host_id": "9001"}, "host.get", "host.delete", "host", "9001", {}),
    (items, "delete_item", {"item_id": "9601"}, "item.get", "item.delete", "item", "9601", {}),
    (triggers, "delete_trigger", {"trigger_id": "9003"}, "trigger.get", "trigger.delete",
     "trigger", "9003", {}),
    (hostgroups, "delete_hostgroup", {"group_id": "21"}, "hostgroup.get", "hostgroup.delete",
     "hostgroup", "21", {}),
    (maintenance, "delete_maintenance", {"maintenance_id": "5"}, "maintenance.get",
     "maintenance.delete", "maintenance", "5", {}),
    (macros, "delete_host_macro", {"host_id": "1", "macro": "{$X}"}, "usermacro.get",
     "usermacro.delete", "usermacro", "71", {"usermacro.get": _macro_get}),
]

UPDATES = [
    (hosts, "update_host", {"host_id": "9001", "name": "srv-aq9001"}, "host.get", "host.update",
     "host", "9001", {}),
    (items, "update_item", {"item_id": "9601", "name": "Inbound"}, "item.get", "item.update",
     "item", "9601", {}),
    (triggers, "update_trigger", {"trigger_id": "9003", "priority": 4}, "trigger.get",
     "trigger.update", "trigger", "9003", {}),
    (macros, "set_host_macro", {"host_id": "1", "macro": "{$X}", "value": "2"}, "usermacro.get",
     "usermacro.update", "usermacro", "71", {"usermacro.get": _macro_get}),
]


def _snap_responses(get_method, obj_type, obj_id, extra):
    snap = {SNAP_ID[obj_type]: obj_id, "name": "before"}
    return {get_method: [snap], **extra}, snap


SNAP_ID = {"host": "hostid", "item": "itemid", "trigger": "triggerid",
           "hostgroup": "groupid", "maintenance": "maintenanceid", "usermacro": "hostmacroid"}


def _ids(obj_type, obj_id):
    return {f"{SNAP_ID[obj_type]}s": [obj_id]}


@pytest.mark.parametrize("case", DELETES + UPDATES, ids=lambda c: c[1])
def test_success_snapshots_before_write_and_journals_once(case):
    module, tool, kwargs, get_m, write_m, obj_type, obj_id, extra = case
    responses, snap = _snap_responses(get_m, obj_type, obj_id, extra)
    c = JournalingClient(responses)
    run_tool(module, tool, c, **kwargs)
    methods = [m for m, _ in c.calls]
    snap_idx = next(i for i, (m, p) in enumerate(c.calls)
                    if m == get_m and _ids(obj_type, obj_id).items() <= p.items())
    assert snap_idx < methods.index(write_m)
    [e] = c.rollback_log.entries
    action = Action.DELETE if write_m.endswith(".delete") else Action.UPDATE
    assert (e.action, e.object_type, e.object_id) == (action, obj_type, obj_id)
    assert e.snapshot == (extra[get_m]({})[0] if extra else snap)


@pytest.mark.parametrize("case", DELETES + UPDATES, ids=lambda c: c[1])
def test_failed_write_leaves_no_journal_entry(case):
    module, tool, kwargs, get_m, write_m, obj_type, obj_id, extra = case
    responses, _ = _snap_responses(get_m, obj_type, obj_id, extra)

    def boom(_p):
        raise ValueError("Zabbix API error: denied")

    c = JournalingClient({**responses, write_m: boom})
    out = run_tool(module, tool, c, **kwargs)
    assert out.startswith("Error") and "denied" in out
    assert write_m in [m for m, _ in c.calls]
    assert c.rollback_log.entries == []
