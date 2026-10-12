"""End-to-end: the real MCP over stdio against a real, seeded Zabbix 7.0.

The wire-contract suite (``tests/wiretest.py``) proves which parameters a tool
*sends*; only a live server proves Zabbix *accepts* them. Every call below
fails on a JSON-RPC error, an ``isError`` result or a surfaced ``Zabbix API
error`` (see ``McpProcess.call``), which is the -32602 bug class.

The fleet is the synthetic one in ``tests/integration/seed.py``.
"""

from __future__ import annotations

import re
import time

import pytest

from tests.integration.seed import TRIGGER_HOST

pytestmark = pytest.mark.integration

TRIGGER_TEXT = "CPU idle below 5% on srv-aq9001"

READ_TOOLS = {
    "check_connection", "get_zabbix_version", "search_hosts", "get_host",
    "get_host_items", "get_problems", "get_trends_batch", "get_traffic_report",
    "diagnose_host", "get_hostgroups", "get_maintenance",
}
WRITE_TOOLS = {"create_maintenance", "delete_maintenance", "rollback_last"}


def _number_after(line: str, label: str) -> float:
    """First number in a markdown table cell sequence after ``label``."""
    tail = line.split(label, 1)[1]
    m = re.search(r"(\d+(?:\.\d+)?)", tail)
    assert m, line
    return float(m.group(1))


def _mbps(line: str) -> float:
    m = re.search(r"(\d+(?:\.\d+)?) Mbps", line)
    assert m, line
    return float(m.group(1))


def _line_for(text: str, *needles: str) -> str:
    for line in text.splitlines():
        if all(n in line for n in needles):
            return line
    raise AssertionError(f"no line with {needles!r} in:\n{text}")


class TestHandshake:
    def test_initialize_names_the_server(self, mcp):
        assert mcp.server_info["serverInfo"]["name"]
        assert "tools" in mcp.server_info["capabilities"]

    def test_every_exercised_tool_is_listed(self, mcp):
        names = mcp.tool_names()
        assert names >= READ_TOOLS | WRITE_TOOLS, sorted((READ_TOOLS | WRITE_TOOLS) - names)

    def test_bad_arguments_are_a_protocol_error_not_a_crash(self, mcp):
        reply = mcp.call_raw("get_host")  # host_id is required
        bad = "error" in reply or reply.get("result", {}).get("isError")
        assert bad, reply
        # the process survives and still answers
        assert mcp.call("check_connection").startswith("Connected.")


class TestConnection:
    def test_check_connection_reports_the_live_version(self, mcp, seed_state):
        out = mcp.call("check_connection")
        assert out.startswith(f"Connected. Zabbix version: {seed_state['version']}"), out
        assert seed_state["version"].startswith("7.0")

    def test_version_matrix_comes_from_the_server(self, mcp, seed_state):
        out = mcp.call("get_zabbix_version")
        assert seed_state["version"] in out, out


class TestInventory:
    def test_hostgroups_include_both_seeded_groups(self, mcp):
        out = mcp.call("get_hostgroups", query="app_")
        assert "Found: 2 host groups" in out, out
        assert "app_free" in out and "app_paid" in out

    def test_hostgroups_with_hosts(self, mcp):
        out = mcp.call("get_hostgroups", query="app_free", include_hosts=True)
        for h in ("srv-aq9003", "srv-aq9004", "srv-bv9001"):
            assert h in out, out

    def test_search_hosts_by_name(self, mcp):
        out = mcp.call("search_hosts", query="srv-aq900")
        for h in ("srv-aq9001", "srv-aq9002", "srv-aq9003", "srv-aq9004"):
            assert h in out, out
        assert "srv-bv9001" not in out

    def test_search_hosts_by_group(self, mcp):
        out = mcp.call("search_hosts", group="app_free")
        assert "srv-bv9001" in out and "srv-aq9003" in out, out
        assert "srv-aq9002" not in out

    def test_search_hosts_unknown_name_is_empty_not_an_error(self, mcp):
        out = mcp.call("search_hosts", query="srv-hm9009")
        assert out.startswith("No hosts found."), out

    def test_get_host_by_name(self, mcp):
        out = mcp.call("get_host", host_id="srv-aq9001")
        assert "srv-aq9001" in out and "192.0.2.11" in out, out
        assert "app_paid" in out

    def test_get_host_by_id_brief(self, mcp, seed_state):
        out = mcp.call("get_host", host_id=seed_state["hostids"]["srv-bv9001"], brief=True)
        assert "srv-bv9001" in out and "192.0.2.15" in out, out

    def test_get_host_items_shows_the_quoted_key(self, mcp, seed_state):
        out = mcp.call("get_host_items", host_id=seed_state["hostids"]["srv-bv9001"])
        assert 'net.if.in["enp3s0"]' in out, out
        assert "system.cpu.util[,idle]" in out

    def test_get_host_items_search(self, mcp, seed_state):
        out = mcp.call("get_host_items", host_id=seed_state["hostids"]["srv-aq9003"], search="net.if")
        assert "Found: 1 items" in out and "net.if.in[eth0]" in out, out


class TestProblems:
    def test_problem_names_its_host(self, mcp):
        out = mcp.call("get_problems", host=TRIGGER_HOST)
        assert f"`{TRIGGER_HOST}` · {TRIGGER_TEXT}" in out, out

    def test_group_listing_carries_host_column(self, mcp):
        out = mcp.call("get_problems", group="app_paid")
        assert f"`{TRIGGER_HOST}`" in out and TRIGGER_TEXT in out, out

    def test_sub_host_query_includes_its_parent(self, mcp):
        out = mcp.call("get_problems", host="srv-aq9001 aq9002")
        assert "is a sub-host; problems of its parent `srv-aq9001` are included" in out, out
        assert TRIGGER_TEXT in out

    def test_a_quiet_host_has_no_problems(self, mcp):
        out = mcp.call("get_problems", host="srv-aq9004")
        assert TRIGGER_TEXT not in out, out

    def test_resolved_window_view(self, mcp):
        out = mcp.call("get_problems", include_resolved=True, time_from="24h")
        assert TRIGGER_TEXT in out, out


class TestTrafficAndTrends:
    def test_trends_batch_reads_the_quoted_key_host(self, mcp):
        """ADR 147: a host whose only NIC item is ``net.if.in["enp3s0"]`` has traffic."""
        out = mcp.call("get_trends_batch", hosts="srv-bv9001,srv-aq9003", metrics="traffic", period="7d")
        bv = _number_after(_line_for(out, "srv-bv9001", "traffic"), "traffic")
        aq = _number_after(_line_for(out, "srv-aq9003", "traffic"), "traffic")
        assert bv == pytest.approx(250, rel=0.05), out
        assert aq == pytest.approx(200, rel=0.05), out

    def test_trends_batch_names_unknown_hosts(self, mcp):
        out = mcp.call("get_trends_batch", hosts="srv-aq9003,srv-hm9009", metrics="traffic")
        assert "srv-hm9009" in out and "srv-aq9003" in out, out

    def test_trends_batch_cpu(self, mcp):
        out = mcp.call("get_trends_batch", hosts="srv-aq9004", metrics="cpu", period="7d")
        assert "srv-aq9004" in out and "cpu" in out, out

    def test_traffic_report_folds_the_sub_host_into_its_machine(self, mcp):
        out = mcp.call("get_traffic_report", group="app_paid")
        row = _line_for(out, "srv-aq9001")
        assert "srv-aq9001 aq9002" not in out, out
        # 400 Mbps on the parent + 100 Mbps on the sub-host's own interface
        assert _mbps(row) == pytest.approx(500, rel=0.05), out
        assert "srv-aq9002" in out

    def test_traffic_report_named_hosts(self, mcp):
        out = mcp.call("get_traffic_report", hosts="srv-aq9003,srv-aq9004")
        assert "srv-aq9003" in out and "srv-aq9004" in out, out
        assert "srv-aq9002" not in out


class TestDiagnose:
    def test_diagnose_a_healthy_host(self, mcp):
        out = mcp.call("diagnose_host", host="srv-aq9003")
        assert "## Diagnosis: srv-aq9003" in out and "**Verdict:**" in out, out
        assert "### Active problems (0)" in out
        assert "200.0 Mbps" in out  # the seeded trend baseline

    def test_diagnose_a_host_with_a_problem(self, mcp):
        out = mcp.call("diagnose_host", host=TRIGGER_HOST)
        assert "**Verdict:**" in out and TRIGGER_TEXT in out, out

    def test_diagnose_unknown_host(self, mcp):
        out = mcp.call("diagnose_host", host="srv-hm9009")
        assert out == "Host not found or disabled: srv-hm9009", out


class TestMaintenanceWritePath:
    def _window(self, mcp, seed_state, name: str) -> str:
        now = int(time.time())
        out = mcp.call(
            "create_maintenance", name=name, active_since=str(now), active_till=str(now + 3600),
            host_ids=seed_state["hostids"]["srv-aq9004"], description="integration test window",
        )
        m = re.fullmatch(r"Maintenance window created\. ID: (\d+)", out)
        assert m, out
        return m.group(1)

    def test_create_then_rollback_last(self, mcp, seed_state):
        name = f"it-rollback-{int(time.time())}"
        mid = self._window(mcp, seed_state, name)

        listed = mcp.call("get_maintenance", host_id=seed_state["hostids"]["srv-aq9004"])
        assert name in listed and f"(id: {mid})" in listed, listed
        assert "1 hosts" in _line_for(listed, f"(id: {mid})")

        undone = mcp.call("rollback_last")
        assert undone.startswith(f"Rolled back CREATE: deleted maintenance {mid}."), undone
        after = mcp.call("get_maintenance")
        assert name not in after, after

    def test_create_then_delete(self, mcp, seed_state):
        name = f"it-delete-{int(time.time())}"
        mid = self._window(mcp, seed_state, name)
        assert name in mcp.call("get_maintenance", group="app_free") or name in mcp.call("get_maintenance")

        assert mcp.call("delete_maintenance", maintenance_id=mid) == f"Maintenance window {mid} deleted."
        assert name not in mcp.call("get_maintenance")

    def test_inverted_window_is_refused_before_the_api(self, mcp, seed_state):
        now = int(time.time())
        out = mcp.call("create_maintenance", name="it-bad", active_since=str(now + 60),
                       active_till=str(now), host_ids=seed_state["hostids"]["srv-aq9004"])
        assert out == "active_till must be after active_since."


class TestReadOnly:
    def test_read_only_hides_every_write_tool(self, start_mcp):
        ro = start_mcp(ZABBIX_READ_ONLY="1")
        names = ro.tool_names()
        assert not (WRITE_TOOLS & names), sorted(WRITE_TOOLS & names)
        assert names >= READ_TOOLS
        reply = ro.call_raw("create_maintenance", name="it-ro", active_since="1", active_till="2")
        assert "error" in reply or reply.get("result", {}).get("isError"), reply
        assert ro.call("check_connection").startswith("Connected.")
