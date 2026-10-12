"""Wire tests for configuration export/import (synthetic fixtures)."""

import httpx

from tests.wiretest import CaptureMCP, RecordingClient, StubResolver, run_tool
from zbbx_mcp.tools import WRITE_TOOLS, configuration


def test_export_sends_all_three_option_lists_and_fences_result():
    c = RecordingClient({"configuration.export": "zabbix_export:\n  version: '7.0'"})
    out = run_tool(configuration, "export_configuration", c,
                   host_ids="10, 11", template_ids="20", group_ids="30,31", format="json")
    assert c.calls == [("configuration.export", {
        "options": {"hosts": ["10", "11"], "templates": ["20"], "host_groups": ["30", "31"]},
        "format": "json",
    })]
    assert out == "```json\nzabbix_export:\n  version: '7.0'\n```"


def test_export_requires_a_selector_and_sends_nothing():
    c = RecordingClient()
    out = run_tool(configuration, "export_configuration", c)
    assert out == "At least one of host_ids, template_ids, or group_ids is required."
    assert c.calls == []


def test_export_empty_result():
    c = RecordingClient({"configuration.export": ""})
    out = run_tool(configuration, "export_configuration", c, host_ids="10")
    assert out == "No configuration data returned."
    assert c.sent("configuration.export")["format"] == "yaml"


def test_export_truncates_large_payload():
    c = RecordingClient({"configuration.export": "x" * 12000})
    out = run_tool(configuration, "export_configuration", c, template_ids="20")
    assert out.startswith("```yaml\n" + "x" * 10000 + "\n```")
    assert "x" * 10001 not in out
    assert out.endswith("*... truncated (12000 chars total)*")


def test_export_api_error_is_reported():
    def boom(_):
        raise ValueError("Zabbix API error: no permissions")
    out = run_tool(configuration, "export_configuration",
                   RecordingClient({"configuration.export": boom}), group_ids="30")
    assert out == "Error exporting configuration: Zabbix API error: no permissions"


def test_import_sends_exact_rules_payload():
    c = RecordingClient({"configuration.import": True})
    src = "zabbix_export:\n  version: '7.0'"
    out = run_tool(configuration, "import_configuration", c,
                   source=src, format="yaml", create_new=False, update_existing=True)
    rule = {"createMissing": False, "updateExisting": True}
    assert c.calls == [("configuration.import", {
        "format": "yaml",
        "source": src,
        "rules": {k: rule for k in (
            "hosts", "templates", "host_groups", "template_groups",
            "items", "triggers", "graphs", "valueMaps")},
    })]
    assert out == "Configuration imported successfully."


def test_import_transport_error_is_reported():
    def boom(_):
        raise httpx.ConnectError("connection refused")
    out = run_tool(configuration, "import_configuration",
                   RecordingClient({"configuration.import": boom}), source="{}", format="json")
    assert out == "Error importing configuration: connection refused"


def test_import_is_a_write_tool_and_skipped_when_read_only():
    assert "import_configuration" in WRITE_TOOLS
    assert "export_configuration" not in WRITE_TOOLS
    mcp = CaptureMCP()
    configuration.register(mcp, StubResolver(RecordingClient()), skip=set(WRITE_TOOLS))
    assert set(mcp.fns) == {"export_configuration"}
