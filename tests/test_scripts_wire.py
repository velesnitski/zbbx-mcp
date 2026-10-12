"""Wire tests for script listing and execution (synthetic fixtures)."""

from tests.wiretest import CaptureMCP, RecordingClient, StubResolver, run_tool
from zbbx_mcp.tools import WRITE_TOOLS, scripts

SCRIPTS = [
    {"scriptid": "1", "name": "Ping", "type": "0", "execute_on": "1", "description": ""},
    {"scriptid": "2", "name": "Traceroute", "type": "0", "execute_on": "0",
     "description": "d" * 150},
    {"scriptid": "3", "name": "Notify", "type": "5", "execute_on": "2"},
    {"scriptid": "4", "name": "Odd", "type": "9", "execute_on": "7"},
]


def test_get_scripts_filters_and_formats():
    c = RecordingClient({"script.get": SCRIPTS})
    out = run_tool(scripts, "get_scripts", c, host_id="10", search="P*")
    assert c.calls == [("script.get", {
        "output": ["scriptid", "name", "type", "execute_on", "command", "description", "scope"],
        "sortfield": "name",
        "hostids": ["10"],
        "search": {"name": "P*"},
        "searchWildcardsEnabled": True,
    })]
    assert out.startswith("**Found: 4 scripts**\n\n")
    assert "- **Ping** (Script, runs on: Zabbix server) — scriptid: 1" in out
    assert "- **Traceroute** (Script, runs on: Zabbix agent) — scriptid: 2\n  " + "d" * 100 in out
    assert "d" * 101 not in out
    assert "- **Notify** (Webhook, runs on: Zabbix server (relay)) — scriptid: 3" in out
    assert "- **Odd** (?, runs on: ?) — scriptid: 4" in out


def test_get_scripts_unfiltered_sends_no_filters():
    c = RecordingClient({"script.get": []})
    assert run_tool(scripts, "get_scripts", c) == "No scripts found."
    p = c.sent("script.get")
    assert "hostids" not in p and "search" not in p


def test_get_scripts_api_error():
    def boom(_):
        raise ValueError("Zabbix API error: denied")
    out = run_tool(scripts, "get_scripts", RecordingClient({"script.get": boom}))
    assert out == "Error querying Zabbix: Zabbix API error: denied"


def test_execute_script_sends_exact_payload_and_shows_output():
    c = RecordingClient({"script.execute": {"response": "success", "value": "line1\nline2"}})
    out = run_tool(scripts, "execute_script", c, script_id="1", host_id="10")
    assert c.calls == [("script.execute", {"scriptid": "1", "hostid": "10"})]
    assert out == "**Script execution: success**\n\n```\nline1\nline2\n```"
    assert c.rollback == []  # nothing to undo for an execution


def test_execute_script_without_output():
    c = RecordingClient({"script.execute": {}})
    out = run_tool(scripts, "execute_script", c, script_id="1", host_id="10")
    assert out == "**Script execution: success**"


def test_execute_script_error():
    def boom(_):
        raise ValueError("Zabbix API error: timeout while executing")
    out = run_tool(scripts, "execute_script", RecordingClient({"script.execute": boom}),
                   script_id="1", host_id="10")
    assert out == "Error executing script: Zabbix API error: timeout while executing"


def test_execute_is_gated_by_read_only():
    assert "execute_script" in WRITE_TOOLS
    mcp = CaptureMCP()
    scripts.register(mcp, StubResolver(RecordingClient()), skip=set(WRITE_TOOLS))
    assert set(mcp.fns) == {"get_scripts"}
