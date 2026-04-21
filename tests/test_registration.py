
from mcp.server.fastmcp import FastMCP

from zbbx_mcp.client import ZabbixClient
from zbbx_mcp.config import ZabbixConfig
from zbbx_mcp.resolver import InstanceResolver
from zbbx_mcp.tools import WRITE_TOOLS, register_all


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
    "get_server_clusters",
    "search_hosts_by_ip",
    "search_hosts_by_location",
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
    "get_agent_unreachable",
    "get_active_problems",
    "get_stale_servers",
    # dashboards.py
    "get_dashboards",
    "get_dashboard_detail",
    "find_host_dashboard",
    # items.py
    "create_item",
    "update_item",
    "delete_item",
    "get_host_items",
    "search_items",
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
    "get_event_frequency",
    "get_correlated_events",
    "get_error_rate",
    "get_incident_report",
    # geo.py
    "get_expansion_report",
    "get_regional_density_map",
    "get_latency_estimate",
    "get_servers_by_ping",
    # availability.py
    "get_host_availability",
    "get_recent_changes",
    # audit.py
    "get_audit_log",
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
    "set_bulk_macro",
    "delete_host_macro",
    # web_scenarios.py
    "get_web_scenarios",
    "get_web_scenario_status",
    # domains.py
    "get_domain_status",
    "get_ssl_expiry",
    "get_domain_list",
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
    "get_unknown_providers",
    "identify_providers",
    "get_low_disk_servers",
    "get_low_memory_servers",
    "generate_product_map",
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
    "import_costs_by_ip",
    "import_cluster_ip_fees",
    "analyze_cost_import",
    "reconcile_billing_audit",
    "find_stale_billing_ips",
    "set_bulk_cost",
    "fill_cost_median",
    "detect_cost_anomalies",
    "export_cost_audit",
    "import_from_xlsx",
    "get_cost_summary",
    "get_cost_gaps",
    "get_cost_efficiency",
    # traffic.py
    "detect_traffic_anomalies",
    "get_traffic_report",
    "detect_traffic_drops",
    # dashboard_report.py
    "export_dashboard",
    # full_report.py
    "generate_full_report",
    # trends.py
    "get_trends_batch",
    "get_server_dashboard",
    "compare_servers",
    "get_health_assessment",
    "get_shutdown_candidates",
    "get_capacity_planning",
    # html_report.py
    "generate_html_report",
    # geo.py
    "detect_regional_anomalies",
    "get_geo_traffic_trends",
    "get_service_uptime_report",
    "get_service_health_matrix",
    "get_traffic_drop_timeline",
    # executive.py
    "get_executive_dashboard",
    "get_month_over_month",
    "get_fleet_risk_score",
    "get_sla_dashboard",
    "get_report_snapshot",
    "get_peak_analysis",
    "get_product_audit",
    "get_predictive_alerts",
    # ceo_report.py
    "generate_ceo_report",
    # service_brief.py
    "generate_service_brief",
    # analysis.py
    "analyze_server_roles",
    "correlate_logs",
    "audit_host_ips",
    "classify_external_ips",
    "audit_external_ips",
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
