"""get_users wire-contract tests (ADR 085).

user.get was requesting the removed `type` (and `rows_per_page`) fields, which
Zabbix 7.x rejects with -32602 — get_users was dead on every call. It now
requests `roleid` and resolves role names via role.get.
"""

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp.tools import users as users_mod

LEGAL_USER_OUTPUT = {
    "userid", "username", "name", "surname", "url", "autologin", "autologout",
    "lang", "refresh", "theme", "roleid", "attempt_failed", "attempt_ip",
    "attempt_clock",
}


def _run(users, roles):
    client = RecordingClient({"user.get": users, "role.get": roles})
    out = run_tool(users_mod, "get_users", client)
    return client, out


def _user(**extra):
    base = {"userid": "3", "username": "ops", "name": "Op", "surname": "Erator",
            "roleid": "2", "usrgrps": [{"name": "Admins"}], "medias": []}
    base.update(extra)
    return base


class TestGetUsersContract:
    def test_output_excludes_removed_fields(self):
        client, _ = _run([_user()], [{"roleid": "2", "name": "Admin role"}])
        sent = client.sent("user.get")["output"]
        assert "type" not in sent           # the -32602 carrier
        assert "rows_per_page" not in sent
        assert "roleid" in sent
        assert set(sent) <= LEGAL_USER_OUTPUT

    def test_role_name_resolved_and_rendered(self):
        client, out = _run([_user(roleid="2")], [{"roleid": "2", "name": "Admin role"}])
        assert client.sent("role.get")["roleids"] == ["2"]
        assert "**ops** (Admin role)" in out

    def test_role_lookup_falls_back_to_id(self):
        # role.get returns nothing (e.g. no permission) -> show the raw id, never crash.
        _, out = _run([_user(roleid="7")], [])
        assert "role 7" in out

    def test_no_role_get_when_no_roleids(self):
        client, _ = _run([_user(roleid="")], [])
        assert not [m for m, _ in client.calls if m == "role.get"]

    def test_empty_users(self):
        _, out = _run([], [])
        assert out == "No users found."
