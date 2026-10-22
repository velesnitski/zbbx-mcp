"""Macro tools wire test: reads mask secret values, writes send the exact
``usermacro.*`` payload and leave the right rollback journal entry, and the
bulk tool's dry-run / apply / filter paths. Fixtures are synthetic.

The secret value used below is assembled at runtime so no secret-shaped
literal sits in the repository.
"""

import time

import httpx

from tests.wiretest import JournalingClient, RecordingClient, run_tool
from zbbx_mcp.tools import macros

SECRET = "-".join(["not", "a", "real", "value"])
NOW = str(int(time.time()) - 60)


class JournalClient(RecordingClient):
    """RecordingClient plus the snapshot-taking journal hook update/delete use."""

    async def snapshot_and_record(self, action, kind, obj_id, note=""):
        self.rollback.append((action, kind, obj_id, note))


HOST_MACROS = [
    {"hostmacroid": "71", "macro": "{$BW_LIMIT}", "value": "1000", "type": "0",
     "description": "uplink cap"},
    {"hostmacroid": "72", "macro": "{$DB_PASS}", "value": SECRET, "type": "1"},
    {"hostmacroid": "73", "macro": "{$VAULT_REF}", "value": "path/to:key", "type": "2"},
    {"hostmacroid": "74", "macro": "{$ODD}", "value": "x", "type": "7"},
]


def _boom(exc):
    def f(_p):
        raise exc
    return f


# ---- get_host_macros / get_global_macros ---------------------------------

def test_get_host_macros_masks_secret():
    c = RecordingClient({"usermacro.get": HOST_MACROS})
    out = run_tool(macros, "get_host_macros", c, host_id="1")
    assert c.sent("usermacro.get") == {
        "hostids": ["1"],
        "output": ["hostmacroid", "macro", "value", "type", "description"],
        "sortfield": "macro",
    }
    assert out.startswith("**Found: 4 host macros**")
    assert "- `{$BW_LIMIT}` = 1000 (Text) — uplink cap" in out
    assert "- `{$DB_PASS}` = ****** (Secret)" in out
    assert "- `{$VAULT_REF}` = path/to:key (Vault)" in out
    assert "- `{$ODD}` = x (?)" in out
    assert SECRET not in out


def test_get_host_macros_empty_and_error():
    assert run_tool(macros, "get_host_macros", RecordingClient(), host_id="1") == "No host macros found."
    out = run_tool(macros, "get_host_macros",
                   RecordingClient({"usermacro.get": _boom(ValueError("denied"))}), host_id="1")
    assert out == "Error querying Zabbix: denied"


def test_get_global_macros_masks_secret():
    data = [
        {"globalmacroid": "1", "macro": "{$SNMP_COMMUNITY}", "value": SECRET, "type": "1",
         "description": "shared"},
        {"globalmacroid": "2", "macro": "{$TIMEOUT}", "value": "30", "type": "0"},
        {"globalmacroid": "3", "macro": "{$PLAIN}", "value": "on"},
    ]
    c = RecordingClient({"usermacro.get": data})
    out = run_tool(macros, "get_global_macros", c)
    assert c.sent("usermacro.get") == {
        "globalmacro": True,
        "output": ["globalmacroid", "macro", "value", "type", "description"],
        "sortfield": "macro",
    }
    assert out.startswith("**Found: 3 global macros**")
    assert "- `{$SNMP_COMMUNITY}` = ****** (Secret) — shared" in out
    assert "- `{$TIMEOUT}` = 30 (Text)" in out
    assert "- `{$PLAIN}` = on (Text)" in out
    assert SECRET not in out


def test_get_global_macros_empty_and_error():
    assert run_tool(macros, "get_global_macros", RecordingClient()) == "No global macros found."
    out = run_tool(macros, "get_global_macros",
                   RecordingClient({"usermacro.get": _boom(httpx.ConnectError("down"))}))
    assert out == "Error querying Zabbix: down"


# ---- set_host_macro --------------------------------------------------------

def test_set_host_macro_creates_when_absent():
    c = JournalClient({"usermacro.create": {"hostmacroids": ["81"]}})
    out = run_tool(macros, "set_host_macro", c, host_id="1", macro="{$DB_PASS}",
                   value=SECRET, description="db", macro_type=1)
    assert c.calls == [
        ("usermacro.get", {"hostids": ["1"], "filter": {"macro": "{$DB_PASS}"},
                           "output": ["hostmacroid"]}),
        ("usermacro.create", {"hostid": "1", "macro": "{$DB_PASS}", "value": SECRET,
                              "type": 1, "description": "db"}),
    ]
    assert c.rollback == [("create", "usermacro", "81", "Created macro {$DB_PASS} on host 1")]
    assert out == "Host macro `{$DB_PASS}` created on host 1."
    assert SECRET not in out
    assert all(SECRET not in note for *_, note in c.rollback)


def test_set_host_macro_updates_existing_with_snapshot():
    c = JournalClient({"usermacro.get": [{"hostmacroid": "72"}]})
    out = run_tool(macros, "set_host_macro", c, host_id="1", macro="{$DB_PASS}",
                   value=SECRET, macro_type=1)
    # Journal is written BEFORE the mutation, and holds no value.
    assert c.rollback == [("update", "usermacro", "72", "Updated macro {$DB_PASS} on host 1")]
    assert c.calls[-1] == ("usermacro.update", {"hostmacroid": "72", "value": SECRET, "type": 1})
    assert out == "Host macro `{$DB_PASS}` updated on host 1."
    assert SECRET not in out


def test_set_host_macro_update_with_description():
    c = JournalClient({"usermacro.get": [{"hostmacroid": "71"}]})
    run_tool(macros, "set_host_macro", c, host_id="1", macro="{$BW_LIMIT}", value="500",
             description="new cap")
    assert c.sent("usermacro.update") == {"hostmacroid": "71", "value": "500", "type": 0,
                                          "description": "new cap"}


def test_set_host_macro_error_records_nothing():
    c = JournalClient({"usermacro.get": _boom(ValueError("no access"))})
    out = run_tool(macros, "set_host_macro", c, host_id="1", macro="{$X}", value=SECRET)
    assert out == "Error setting macro: no access"
    assert c.rollback == []
    assert SECRET not in out


# ---- delete_host_macro -----------------------------------------------------

def test_delete_host_macro():
    c = JournalClient({"usermacro.get": [{"hostmacroid": "71"}]})
    out = run_tool(macros, "delete_host_macro", c, host_id="1", macro="{$BW_LIMIT}")
    assert c.calls[-1] == ("usermacro.delete", ["71"])
    assert c.rollback == [("delete", "usermacro", "71", "Deleted macro {$BW_LIMIT} from host 1")]
    assert out == "Macro `{$BW_LIMIT}` deleted from host 1."


def test_delete_host_macro_missing_and_error():
    c = JournalClient()
    out = run_tool(macros, "delete_host_macro", c, host_id="1", macro="{$NOPE}")
    assert out == "Macro `{$NOPE}` not found on host 1."
    assert [m for m, _ in c.calls] == ["usermacro.get"] and c.rollback == []

    c = JournalClient({"usermacro.get": [{"hostmacroid": "71"}],
                       "usermacro.delete": _boom(httpx.ReadTimeout("slow"))})
    out = run_tool(macros, "delete_host_macro", c, host_id="1", macro="{$BW_LIMIT}")
    assert out == "Error deleting macro: slow"


# ---- set_bulk_macro --------------------------------------------------------

HOSTS = [
    {"hostid": "1", "host": "srv-aq9001", "groups": [{"name": "app_free"}]},
    {"hostid": "2", "host": "srv-aq9002", "groups": [{"name": "app_paid"}]},
    {"hostid": "3", "host": "srv-bv9003", "groups": [{"name": "App_Free"}]},
    {"hostid": "4", "host": "srv-hm9004", "groups": [{"name": "app_free"}]},
]


def _traffic(p):
    rows = {"1": 120_000_000, "3": 5_000_000, "4": 80_000_000}
    return [{"hostid": h, "key_": "net.if.in[eth0]", "lastvalue": str(v), "lastclock": NOW}
            for h, v in rows.items() if h in p["hostids"]]


def test_bulk_dry_run_filters_by_group_country_traffic():
    c = RecordingClient({"host.get": HOSTS, "item.get": _traffic})
    out = run_tool(macros, "set_bulk_macro", c, macro="{$BW_LIMIT}", value="100",
                   group="APP_FREE", country="aq", min_traffic_mbps=50)
    # group (case-insensitive) keeps 1,3,4; country AQ keeps 1; traffic 120 Mbps passes.
    assert out.startswith("**Dry run:** would set `{$BW_LIMIT}` = `100` on 1 hosts")
    assert "| srv-aq9001 | 1 |" in out
    assert "srv-bv9003" not in out and "srv-hm9004" not in out
    assert "Run with `dry_run=False` to apply." in out
    assert not any(m in ("usermacro.update", "usermacro.create") for m, _ in c.calls)
    assert c.sent("item.get")["hostids"] == ["1"]


def test_bulk_dry_run_caps_listing_at_twenty():
    many = [{"hostid": str(i), "host": f"srv-aq{9000 + i}", "groups": []} for i in range(1, 26)]
    out = run_tool(macros, "set_bulk_macro", RecordingClient({"host.get": many}),
                   macro="{$X}", value="1")
    assert "on 25 hosts" in out
    assert "| srv-aq9020 | 20 |" in out and "srv-aq9021" not in out
    assert "*+5 more*" in out


def test_bulk_no_match():
    c = RecordingClient({"host.get": HOSTS, "item.get": _traffic})
    out = run_tool(macros, "set_bulk_macro", c, macro="{$X}", value="1", min_traffic_mbps=500)
    assert out == "No hosts match the filters."


def test_bulk_apply_updates_creates_counts_errors_and_journals():
    def get_macro(p):
        hid = p["hostids"][0]
        if hid == "4":
            raise ValueError("denied")
        return [{"hostmacroid": "m1", "type": "0"}] if hid == "1" else []

    c = RecordingClient({"host.get": HOSTS, "usermacro.get": get_macro,
                         "usermacro.create": lambda p: {"hostmacroids": ["n" + p["hostid"]]}})
    out = run_tool(macros, "set_bulk_macro", c, macro="{$BW_LIMIT}", value="100",
                   dry_run=False)
    assert out == ("**Bulk macro set:** `{$BW_LIMIT}` = `100`\n"
                   "Updated: 1 | Created: 2 | Errors: 1 | Total: 4")
    assert ("usermacro.update", {"hostmacroid": "m1", "value": "100"}) in c.calls
    creates = sorted(p["hostid"] for m, p in c.calls if m == "usermacro.create")
    assert creates == ["2", "3"]
    assert c.sent("usermacro.create")["macro"] == "{$BW_LIMIT}"
    assert c.sent("usermacro.get")["output"] == ["hostmacroid", "type"]
    # One journal entry per host changed; the failed host leaves none.
    assert sorted(c.rollback) == [
        ("create", "usermacro", "n2", "Created macro {$BW_LIMIT} on host 2"),
        ("create", "usermacro", "n3", "Created macro {$BW_LIMIT} on host 3"),
        ("update", "usermacro", "m1", "Updated macro {$BW_LIMIT} on host 1"),
    ]


def test_bulk_apply_failed_write_journals_nothing():
    c = JournalingClient({"host.get": HOSTS[:1],
                          "usermacro.get": [{"hostmacroid": "m1", "type": "0"}],
                          "usermacro.update": _boom(ValueError("denied"))})
    out = run_tool(macros, "set_bulk_macro", c, macro="{$X}", value="1", dry_run=False)
    assert "Errors: 1" in out
    assert c.rollback_log.entries == []


def test_bulk_host_fetch_error():
    out = run_tool(macros, "set_bulk_macro",
                   RecordingClient({"host.get": _boom(httpx.ConnectError("down"))}),
                   macro="{$X}", value="1")
    assert out == "Error: down"


def test_bulk_apply_does_not_echo_value_over_secret_macro():
    def get_macro(p):
        if "hostmacroids" in p:  # the journal snapshot
            return [{"hostmacroid": "72", "macro": "{$DB_PASS}", "value": SECRET, "type": "1"}]
        return [{"hostmacroid": "72", "type": "1"}]

    c = JournalingClient({"host.get": HOSTS[:1], "usermacro.get": get_macro})
    out = run_tool(macros, "set_bulk_macro", c, macro="{$DB_PASS}", value=SECRET,
                   dry_run=False)
    assert SECRET not in out
    assert out.startswith("**Bulk macro set:** `{$DB_PASS}` = <secret>\n")
    assert c.sent("usermacro.update") == {"hostmacroid": "72", "value": SECRET}
    [e] = c.rollback_log.entries
    assert (e.object_type, e.object_id) == ("usermacro", "72")
    assert e.snapshot["value"] == "[REDACTED]"  # the snapshot path redacts secrets


def test_bulk_dry_run_does_not_echo_value_over_secret_macro():
    c = RecordingClient({"host.get": HOSTS[:2],
                         "usermacro.get": [{"hostid": "2", "hostmacroid": "72", "type": "1"}]})
    out = run_tool(macros, "set_bulk_macro", c, macro="{$DB_PASS}", value=SECRET)
    assert SECRET not in out
    assert out.startswith("**Dry run:** would set `{$DB_PASS}` = <secret> on 2 hosts")
    assert c.sent("usermacro.get") == {"hostids": ["1", "2"], "filter": {"macro": "{$DB_PASS}"},
                                       "output": ["hostid", "type"]}
    assert not any(m in ("usermacro.update", "usermacro.create") for m, _ in c.calls)
