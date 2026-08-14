"""API token hygiene (ADR 112).

Tokens are the one object whose ABSENCE of use is the finding, and Zabbix
encodes "never" as 0 in both `lastaccess` and `expires_at` — the trap this
module exists to not fall into, since 0 sorts as the oldest possible time.
"""

import time

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp.tools import tokens as tok_mod
from zbbx_mcp.tools.tokens import (
    DISABLED,
    EXPIRED,
    NEVER_EXPIRES,
    NEVER_USED,
    STALE,
    classify_token,
    token_risk,
)

NOW = 1_700_000_000
D = 86400


class TestClassify:
    def test_zero_lastaccess_is_never_used_not_ancient(self):
        # 0 is Zabbix's "never", and it is also 1970 — treating it as a
        # timestamp makes an unused token look 19,000 days idle, or (worse,
        # after a sort) like the freshest one.
        i = classify_token({"name": "t", "lastaccess": 0, "expires_at": NOW + D}, NOW)
        assert NEVER_USED in i.flags
        assert STALE not in i.flags
        assert i.idle_days is None

    def test_zero_expiry_is_never_expires_not_long_expired(self):
        i = classify_token({"name": "t", "expires_at": 0, "lastaccess": NOW}, NOW)
        assert NEVER_EXPIRES in i.flags
        assert EXPIRED not in i.flags

    def test_past_expiry_is_expired(self):
        i = classify_token({"name": "t", "expires_at": NOW - 5 * D,
                            "lastaccess": NOW - D}, NOW)
        assert EXPIRED in i.flags
        assert i.expires_in_days is not None and i.expires_in_days < 0

    def test_idle_beyond_the_window_is_stale(self):
        i = classify_token({"name": "t", "expires_at": NOW + D,
                            "lastaccess": NOW - 120 * D}, NOW, stale_days=90)
        assert STALE in i.flags and i.idle_days == 120

    def test_healthy_token_raises_nothing(self):
        i = classify_token({"name": "t", "expires_at": NOW + 30 * D,
                            "lastaccess": NOW - D}, NOW)
        assert i.flags == []

    def test_unparseable_fields_do_not_raise(self):
        i = classify_token({"name": "t", "expires_at": "x", "lastaccess": None}, NOW)
        assert NEVER_EXPIRES in i.flags and NEVER_USED in i.flags


class TestRisk:
    def test_expired_outranks_everything_live(self):
        exp = classify_token({"name": "a", "expires_at": NOW - D, "lastaccess": NOW}, NOW)
        perm = classify_token({"name": "b", "expires_at": 0, "lastaccess": 0}, NOW)
        assert token_risk(exp) < token_risk(perm)

    def test_permanent_and_unused_outranks_merely_stale(self):
        perm = classify_token({"name": "b", "expires_at": 0, "lastaccess": 0}, NOW)
        stale = classify_token({"name": "c", "expires_at": NOW + D,
                                "lastaccess": NOW - 200 * D}, NOW)
        assert token_risk(perm) < token_risk(stale)

    def test_disabled_ranks_below_every_live_token(self):
        # It cannot be used, so however alarming its other flags look it must
        # not push a real key off the top of the list.
        dis = classify_token({"name": "d", "status": "1", "expires_at": 0,
                              "lastaccess": 0}, NOW)
        live = classify_token({"name": "e", "expires_at": NOW + D,
                               "lastaccess": NOW - 200 * D}, NOW)
        assert DISABLED in dis.flags
        assert token_risk(dis) > token_risk(live)


class TestWire:
    def test_denied_token_get_is_reported_as_denied(self):
        # The whole point: "no tokens" and "you may not list tokens" must not
        # render the same (ADR 103).
        class Denied(RecordingClient):
            async def call(self, method, params):
                if method == "token.get":
                    raise ValueError("No permissions")
                return await super().call(method, params)
        out = run_tool(tok_mod, "get_api_tokens", Denied({}))
        assert "permissions answer" in out
        assert "NOT 'there are no tokens'" in out

    def test_empty_instance_says_so_plainly(self):
        out = run_tool(tok_mod, "get_api_tokens", RecordingClient({"token.get": []}))
        assert "No API tokens exist" in out

    def test_risky_tokens_render_with_owner(self):
        now = int(time.time())
        c = RecordingClient({
            "token.get": [
                {"tokenid": "1", "name": "ansible", "userid": "7",
                 "expires_at": 0, "lastaccess": 0, "status": "0"},
                {"tokenid": "2", "name": "fresh", "userid": "7",
                 "expires_at": now + 30 * 86400, "lastaccess": now, "status": "0"},
            ],
            "user.get": [{"userid": "7", "username": "svc-account"}],
        })
        out = run_tool(tok_mod, "get_api_tokens", c)
        assert "ansible" in out and "svc-account" in out
        assert "never expires" in out and "never used" in out
        assert "| fresh |" not in out          # only_risky hides the healthy one

    def test_owner_lookup_failure_still_lists_tokens(self):
        # Names are a nicety; a failed user.get must not blank the audit.
        class NoUsers(RecordingClient):
            async def call(self, method, params):
                if method == "user.get":
                    raise ValueError("denied")
                return await super().call(method, params)
        c = NoUsers({"token.get": [
            {"tokenid": "1", "name": "orphan", "userid": "7",
             "expires_at": 0, "lastaccess": 0, "status": "0"}]})
        out = run_tool(tok_mod, "get_api_tokens", c)
        assert "orphan" in out
