"""ADR 151: a search term is a substring, not an exact match.

With wildcards enabled Zabbix treats a bare term as an exact match, so a
fake client that returns everything never showed the defect; the first run
against a real server did. These tests pin what is SENT.
"""

from __future__ import annotations

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp.tools import hostgroups
from zbbx_mcp.utils import contains_pattern

GROUPS = [
    {"groupid": "1", "name": "app_free",
     "hosts": [{"hostid": "9001", "host": "srv-aq9001"}, {"hostid": "9003", "host": "srv-bv9003"}]},
    {"groupid": "2", "name": "app_paid", "hosts": []},
]


class TestContainsPattern:
    def test_bare_term_is_wrapped(self):
        assert contains_pattern("app_") == "*app_*"

    def test_explicit_wildcards_are_kept(self):
        assert contains_pattern("app_*") == "app_*"
        assert contains_pattern("*free") == "*free"

    def test_whitespace_is_trimmed(self):
        assert contains_pattern("  app ") == "*app*"


class TestHostgroups:
    def test_a_prefix_query_is_sent_as_a_substring_pattern(self):
        c = RecordingClient({"hostgroup.get": GROUPS})
        run_tool(hostgroups, "get_hostgroups", c, query="app_")
        p = c.sent("hostgroup.get")
        assert p["search"] == {"name": "*app_*"} and p["searchWildcardsEnabled"] is True

    def test_include_hosts_lists_the_names(self):
        c = RecordingClient({"hostgroup.get": GROUPS})
        out = run_tool(hostgroups, "get_hostgroups", c, query="app", include_hosts=True)
        assert "- **app_free** (groupid: 1) (2 hosts)" in out
        assert "  srv-aq9001, srv-bv9003" in out
        assert "- **app_paid** (groupid: 2) (0 hosts)" in out

    def test_a_large_group_is_capped_with_a_count(self):
        many = [{"groupid": "3", "name": "app_big",
                 "hosts": [{"hostid": str(9000 + i), "host": f"srv-aq{9000 + i}"} for i in range(30)]}]
        out = run_tool(hostgroups, "get_hostgroups", RecordingClient({"hostgroup.get": many}), include_hosts=True)
        assert "… +5 more" in out and "srv-aq9029" not in out
