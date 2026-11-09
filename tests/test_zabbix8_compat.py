"""ADR 162: one tool layer across Zabbix 6.2–8.0.

Two rules. Never call a method a supported version removed; never *ask* for
anything only a newer version understands (an older one rejects the whole
call). Values only a newer version returns are recognised when they appear.
Fixtures are synthetic.
"""

from __future__ import annotations

import ast
import pathlib
import time

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp import api_compat
from zbbx_mcp.api_compat import (
    REMOVED_METHODS,
    ack_actions,
    compact_text,
    is_system_ack,
    removed_in,
)
from zbbx_mcp.tools import health, items, media, problems

ROOT = pathlib.Path(__file__).resolve().parents[1]
NOW = int(time.time())
LIVE = str(NOW - 60)


def _string_calls(path: pathlib.Path):
    """(method, lineno) for every ``*.call("method", …)`` with a literal name."""
    for node in ast.walk(ast.parse(path.read_text())):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "call" and node.args
                and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
            yield node.args[0].value, node.lineno


class TestRemovedMethods:
    def test_nothing_calls_a_method_a_supported_version_removed(self):
        files = [*(ROOT / "src").rglob("*.py"), *(ROOT / "tests" / "integration").rglob("*.py")]
        hits = [f"{p.relative_to(ROOT)}:{ln} {m}" for p in files
                for m, ln in _string_calls(p) if m in REMOVED_METHODS]
        assert not hits, hits

    def test_the_8_0_removals_are_known(self):
        assert removed_in("host.massupdate") == (8, 0)
        assert removed_in("hostinterface.create") == (8, 0)
        assert removed_in("host.update") is None

    def test_acknowledges_are_never_asked_for_8_0_only_fields(self):
        """``maintenanceid`` in selectAcknowledges exists only on 8.0; asking
        a 7.x server for it would reject get_problem_detail outright."""
        c = RecordingClient({"problem.get": []})
        run_tool(problems, "get_problem_detail", c, event_id="1")
        assert "maintenanceid" not in c.sent("problem.get")["selectAcknowledges"]


class TestPure:
    def test_ack_action_labels(self):
        assert ack_actions(6) == ["acknowledged", "message"]
        assert ack_actions("512") == ["suppressed by maintenance"]
        assert ack_actions(None) == [] and ack_actions("x") == []

    def test_system_entries_are_maintenance_with_no_user(self):
        assert is_system_ack({"action": "1024", "userid": "0"})
        assert is_system_ack({"action": "512"})
        assert not is_system_ack({"action": "512", "userid": "7"})
        assert not is_system_ack({"action": "6", "userid": "0"})
        assert not is_system_ack({"action": "bad"})

    def test_compact_text_is_one_table_safe_line(self):
        assert compact_text('{"a": 1,\n "b": "x|y"}') == '{"a": 1, "b": "x\\|y"}'
        assert compact_text("z" * 300, limit=10) == "z" * 9 + "…"
        assert compact_text(None) == ""

    def test_type_maps_cover_8_0(self):
        assert api_compat.VALUE_TYPES["6"] == "JSON" and api_compat.VALUE_TYPES["5"] == "binary"
        assert api_compat.ITEM_TYPES["24"] == "Telemetry query"
        assert api_compat.MEDIA_TYPES["5"] == "Push"
        assert items.VALUE_TYPES is api_compat.VALUE_TYPES   # one map, not two copies


class TestWire:
    def test_json_history_renders_one_safe_line(self):
        meta = [{"itemid": "11", "name": "Status doc", "key_": "doc.status",
                 "units": "", "value_type": "6"}]
        hist = [{"clock": LIVE, "value": '{"state": "ok",\n "detail": "a|b"}'}]
        c = RecordingClient({"item.get": meta, "history.get": hist})
        out = run_tool(items, "get_item_history", c, item_id="11")
        assert c.sent("history.get")["history"] == 6
        assert '{"state": "ok", "detail": "a\\|b"}' in out

    def test_value_type_override_accepts_json_and_binary(self):
        meta = [{"itemid": "11", "name": "n", "key_": "k", "units": "", "value_type": "3"}]
        for vt in (5, 6):
            c = RecordingClient({"item.get": meta, "history.get": []})
            run_tool(items, "get_item_history", c, item_id="11", value_type=vt)
            assert c.sent("history.get")["history"] == vt

    def test_numeric_history_keeps_units(self):
        meta = [{"itemid": "11", "name": "In", "key_": "k", "units": "bps", "value_type": "3"}]
        c = RecordingClient({"item.get": meta, "history.get": [{"clock": LIVE, "value": "2048"}]})
        assert "2.00 Kbps" in run_tool(items, "get_item_history", c, item_id="11")

    def test_maintenance_entries_are_named_as_the_system(self):
        detail = [{"eventid": "55", "name": "Agent down", "severity": "3", "clock": "1700000000",
                   "acknowledges": [
                       {"userid": "3", "clock": "1700000100", "message": "looking", "action": "6"},
                       {"userid": "0", "clock": "1700000200", "message": "", "action": "512"},
                       {"userid": "0", "clock": "1700000300", "message": "", "action": "1024"}]}]
        c = RecordingClient({"problem.get": detail, "user.get": [{"userid": "3", "username": "ops-a"}]})
        out = run_tool(problems, "get_problem_detail", c, event_id="55")
        assert "_acknowledged, message_; looking" in out
        assert "- **system** (" in out and "_suppressed by maintenance_" in out
        assert "_released from maintenance_" in out and "user 0" not in out
        assert c.sent("user.get")["userids"] == ["3"]

    def test_push_media_type_is_named(self):
        c = RecordingClient({"mediatype.get": [{"mediatypeid": "9", "name": "mobile", "type": "5",
                                                "status": "0"}]})
        assert "**mobile** (Push)" in run_tool(media, "get_media_types", c)

    def test_version_report_lists_8_0_features(self):
        feats = dict(health._feature_matrix(8, 0))
        assert feats["JSON item value type (value_type 6)"] is True
        assert dict(health._feature_matrix(7, 4))["JSON item value type (value_type 6)"] is False
        assert health._parse_zabbix_version("8.0.0rc2")[:2] == (8, 0)
