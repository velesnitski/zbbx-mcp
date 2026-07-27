"""Audit-log resource/action constant tests (ADR 095).

The audit tables were offset from the Zabbix 6.0+ constants: 4 read as
"Trigger" (it is Host) and 15 as "Host group" (it is Item), so every
rendered row borrowed a neighbouring object class's label, and the
`resource=` filter selected a different class than the caller asked for.
Separately, four modules inlined the host code as a bare `2` — which is
not an assigned resource type, so those filters matched nothing and the
features built on them reported "none found" instead of failing.
"""

import ast
import pathlib

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp.data import AUDIT_ACTION_UPDATE, AUDIT_RESOURCE_HOST
from zbbx_mcp.tools import audit as audit_mod
from zbbx_mcp.tools.audit import _ACTION_NAMES, _RESOURCE_NAMES

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "zbbx_mcp"


class TestAuditConstants:
    def test_host_and_item_codes(self):
        # Both verified live: rows of these types named a host / items.
        assert AUDIT_RESOURCE_HOST == 4
        assert _RESOURCE_NAMES[4] == "Host"
        assert _RESOURCE_NAMES[15] == "Item"
        assert _RESOURCE_NAMES[14] == "Host group"
        assert _RESOURCE_NAMES[13] == "Trigger"

    def test_unassigned_code_two_is_not_mapped(self):
        # `2` is not an assigned resource type — mapping it was the bug.
        assert 2 not in _RESOURCE_NAMES

    def test_login_actions(self):
        # 4 is Logout, not Login; 8/9 are login success/failure.
        assert _ACTION_NAMES[4] == "Logout"
        assert _ACTION_NAMES[8] == "Login"
        assert _ACTION_NAMES[9] == "Failed login"
        assert AUDIT_ACTION_UPDATE == 1

    def test_no_module_inlines_a_bare_resourcetype(self):
        """Every audit filter must use the named constant, not a literal.

        AST-level so a re-inlined number fails here rather than silently
        matching zero rows in production.
        """
        violations = []
        for path in sorted(SRC.rglob("*.py")):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Dict)):
                    continue
                for k, v in zip(node.keys, node.values, strict=False):
                    if (
                        isinstance(k, ast.Constant) and k.value == "resourcetype"
                        and isinstance(v, ast.Constant) and isinstance(v.value, int)
                    ):
                        violations.append(
                            f"{path.relative_to(SRC)}:{node.lineno} — resourcetype "
                            f"inlined as {v.value!r}; use AUDIT_RESOURCE_HOST (ADR 095)"
                        )
        assert not violations, "\n".join(violations)


class TestAuditLogWire:
    def test_resource_filter_uses_corrected_code(self):
        client = RecordingClient({"auditlog.get": []})
        run_tool(audit_mod, "get_audit_log", client, resource="host")
        assert client.sent("auditlog.get")["filter"]["resourcetype"] == 4

    def test_item_filter_is_item_not_hostgroup(self):
        client = RecordingClient({"auditlog.get": []})
        run_tool(audit_mod, "get_audit_log", client, resource="item")
        assert client.sent("auditlog.get")["filter"]["resourcetype"] == 15

    def test_login_filter_is_login_not_logout(self):
        client = RecordingClient({"auditlog.get": []})
        run_tool(audit_mod, "get_audit_log", client, action="login")
        assert client.sent("auditlog.get")["filter"]["action"] == 8

    def test_unknown_type_renders_as_type_n_not_a_wrong_label(self):
        client = RecordingClient({"auditlog.get": [
            {"clock": "1700000000", "username": "u", "action": "0",
             "resourcetype": "999", "resourcename": "x", "details": ""},
        ]})
        out = run_tool(audit_mod, "get_audit_log", client)
        assert "Type 999" in out
