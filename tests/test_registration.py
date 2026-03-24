import os
from unittest.mock import patch

from mcp.server.fastmcp import FastMCP

from zbbx_mcp.config import ZabbixConfig
from zbbx_mcp.client import ZabbixClient
from zbbx_mcp.resolver import InstanceResolver
from zbbx_mcp.tools import register_all, WRITE_TOOLS


def _setup(read_only=False, disabled_tools=frozenset()):
    mcp = FastMCP("test")
    cfg = ZabbixConfig(url="https://z.com", token="t")
    client = ZabbixClient(cfg)
    resolver = InstanceResolver({"default": client})
    register_all(mcp, resolver, read_only=read_only, disabled_tools=disabled_tools)
    return mcp


# All tools that should be registered when nothing is skipped
EXPECTED_TOOLS = {
    # hosts.py
    "search_hosts",
    "get_host",
    "create_host",
    "update_host",
    "delete_host",
    # problems.py
    "get_problems",
    "get_problem_detail",
    "acknowledge_problem",
    # hostgroups.py
    "get_hostgroups",
    "create_hostgroup",
    "delete_hostgroup",
    # health.py
    "check_connection",
    # dashboards.py
    "get_dashboards",
    "get_dashboard_detail",
    # items.py
    "create_item",
    "update_item",
    "delete_item",
    "get_host_items",
    "get_item_history",
    "get_graphs",
    # triggers.py
    "get_triggers",
    "create_trigger",
    "update_trigger",
    "delete_trigger",
    # templates.py
    "get_templates",
    "link_template",
    "unlink_template",
    # maintenance.py
    "get_maintenance",
    "create_maintenance",
    "delete_maintenance",
    # events.py
    "get_events",
    "get_trends",
    # discovery.py
    "get_discovery_rules",
    # configuration.py
    "export_configuration",
    "import_configuration",
    # scripts.py
    "get_scripts",
    "execute_script",
    # services.py
    "get_services",
    "get_sla",
    # macros.py
    "get_host_macros",
    "get_global_macros",
    "set_host_macro",
    "delete_host_macro",
    # rollback_tools.py
    "get_rollback_history",
    "rollback_last",
    "rollback_by_index",
    # inventory.py
    "get_server_map",
    "get_product_summary",
    "get_server_load",
    "get_high_cpu_servers",
    "get_underloaded_servers",
    "get_provider_summary",
    # report.py
    "generate_server_report",
    # infra_report.py
    "generate_infra_report",
    # alerts.py
    "get_alerts",
    "get_alert_summary",
    # users.py
    "get_users",
    # proxies.py
    "get_proxies",
    # maps.py
    "get_maps",
    "get_map_detail",
    # media.py
    "get_media_types",
    "get_actions",
    # slack.py
    "send_slack_message",
    "send_slack_report",
    # costs.py
    "import_server_costs",
    "set_bulk_cost",
    "get_cost_summary",
    # traffic.py
    "detect_traffic_anomalies",
    "get_traffic_report",
    "detect_traffic_drops",
    # dashboard_report.py
    "export_dashboard",
}


class TestToolRegistration:
    def test_all_tools_registered(self):
        mcp = _setup()
        tools = set(mcp._tool_manager._tools.keys())
        assert tools == EXPECTED_TOOLS

    def test_read_only_removes_write_tools(self):
        mcp = _setup(read_only=True)
        tools = set(mcp._tool_manager._tools.keys())
        for wt in WRITE_TOOLS:
            assert wt not in tools, f"Write tool '{wt}' should be blocked in read-only mode"
        # Read tools still present
        assert "search_hosts" in tools
        assert "get_problems" in tools
        assert "get_triggers" in tools
        assert "get_templates" in tools
        assert "get_maintenance" in tools
        assert "get_events" in tools
        assert "get_trends" in tools
        assert "get_discovery_rules" in tools
        assert "export_configuration" in tools
        assert "get_scripts" in tools
        assert "get_services" in tools
        assert "get_sla" in tools
        assert "get_host_macros" in tools
        assert "get_global_macros" in tools

    def test_disabled_tools(self):
        mcp = _setup(disabled_tools=frozenset({"get_host", "get_hostgroups", "get_triggers"}))
        tools = set(mcp._tool_manager._tools.keys())
        assert "get_host" not in tools
        assert "get_hostgroups" not in tools
        assert "get_triggers" not in tools
        assert "search_hosts" in tools

    def test_tool_count(self):
        mcp = _setup()
        assert len(mcp._tool_manager._tools) == len(EXPECTED_TOOLS), (
            f"Expected {len(EXPECTED_TOOLS)} tools, got {len(mcp._tool_manager._tools)}: "
            f"{sorted(mcp._tool_manager._tools.keys())}"
        )

    def test_read_only_tool_count(self):
        mcp = _setup(read_only=True)
        expected_read = len(EXPECTED_TOOLS) - len(WRITE_TOOLS)
        actual = len(mcp._tool_manager._tools)
        assert actual == expected_read, (
            f"Expected {expected_read} read-only tools, got {actual}"
        )
