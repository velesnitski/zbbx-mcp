import asyncio as _asyncio
import atexit
import functools
import os
import re

from mcp.server.fastmcp import FastMCP

from zbbx_mcp.client import ZabbixClient
from zbbx_mcp.config import load_all_configs, load_global_policy
from zbbx_mcp.logging import INSTANCE_ID, logged, setup_logging, setup_sentry
from zbbx_mcp.resolver import InstanceResolver
from zbbx_mcp.tools import register_all

# Regex to strip Args/Parameters section from docstrings
_ARGS_RE = re.compile(r"\n\s*Args:\s*\n.*", re.DOTALL)

# Pre-compiled regexes for _compress_response (avoid re-compiling on every call)
_RE_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_RE_HEADERS = re.compile(r"^#{1,4}\s+", re.MULTILINE)
_RE_LONG_DASH = re.compile(r"^-{3,}\s*$", re.MULTILINE)
_RE_BLANK_LINES = re.compile(r"\n{3,}")
_RE_TRAILING_SPACES = re.compile(r" +\n")

def _compress_response(text: str) -> str:
    """Compress MCP tool response to save tokens.

    When ZABBIX_COMPACT=true: strips markdown formatting.
    Always: enforces response budget truncation.

    Table separator rows (|---|---|) are preserved: stripping them breaks
    markdown table rendering in MCP clients, which then collapse rows into
    a single paragraph.
    """
    if not text or len(text) < 200:
        return text

    compact = os.environ.get("ZABBIX_COMPACT", "").lower() in ("1", "true", "yes")
    budget = int(os.environ.get("ZABBIX_RESPONSE_BUDGET", "6000"))

    if compact:
        # Strip markdown bold/headers
        text = _RE_BOLD.sub(r"\1", text)
        text = _RE_HEADERS.sub("", text)
        text = _RE_LONG_DASH.sub("", text)
        # Collapse blank lines
        text = _RE_BLANK_LINES.sub("\n\n", text)
        # Strip trailing spaces
        text = _RE_TRAILING_SPACES.sub("\n", text)

    # Budget truncation
    if budget > 0 and len(text) > budget:
        cut = text.rfind("\n", 0, budget)
        if cut < budget * 0.7:
            cut = budget
        text = text[:cut] + f"\n\n[truncated {len(text)} chars]"

    return text


def _compact_descriptions(mcp: FastMCP) -> int:
    """Strip redundant content from tool definitions to save tokens.

    Two passes:

    1. Strip the ``Args:`` section from the tool description — parameter
       names, types, and defaults are already in the JSON schema, so the
       prose copy is redundant.
    2. Drop the ``title`` field from each parameter inside ``inputSchema``.
       FastMCP auto-generates these (e.g., ``"Max Results"`` for the
       ``max_results`` param) for UI hint purposes; the property *key*
       already conveys the parameter name to the LLM. Across 154 tools
       this saved ~22% of total schema chars in measurement.

    Returns number of chars saved across both passes.
    """
    saved = 0
    if not hasattr(mcp, "_tool_manager") or not hasattr(mcp._tool_manager, "_tools"):
        return 0
    for tool in mcp._tool_manager._tools.values():
        desc = tool.description or ""
        trimmed = _ARGS_RE.sub("", desc).strip()
        if len(trimmed) < len(desc):
            saved += len(desc) - len(trimmed)
            tool.description = trimmed
        params = getattr(tool, "parameters", None)
        if isinstance(params, dict):
            props = params.get("properties")
            if isinstance(props, dict):
                for spec in props.values():
                    if isinstance(spec, dict) and "title" in spec:
                        # `,"title":"X"` — about 12 chars + the title length
                        saved += len(spec["title"]) + 11
                        del spec["title"]
    return saved

_logger = setup_logging()
setup_sentry()


def _register_resources(mcp: FastMCP) -> None:
    """Register MCP resources — static reference data clients can read without tool calls."""

    @mcp.resource("zabbix://tools")
    def tools_catalog() -> str:
        """List of all available Zabbix MCP tools grouped by category."""
        from zbbx_mcp.tools import WRITE_TOOLS
        categories = {
            "Hosts": ["search_hosts", "get_host", "create_host", "update_host", "delete_host", "get_server_clusters", "search_hosts_by_location"],
            "Problems": ["get_problems", "get_problem_detail", "acknowledge_problem"],
            "Host Groups": ["get_hostgroups", "create_hostgroup", "delete_hostgroup"],
            "Triggers": ["get_triggers", "create_trigger", "update_trigger", "delete_trigger"],
            "Templates": ["get_templates", "link_template", "unlink_template"],
            "Items & Metrics": ["get_host_items", "create_item", "update_item", "delete_item", "get_item_history", "get_graphs"],
            "Events & Trends": ["get_events", "get_trends", "get_event_frequency", "get_correlated_events", "get_error_rate", "get_incident_report"],
            "Dashboards": ["get_dashboards", "get_dashboard_detail", "find_host_dashboard"],
            "Maintenance": ["get_maintenance", "create_maintenance", "delete_maintenance"],
            "Availability": ["get_host_availability", "get_recent_changes"],
            "Discovery": ["get_discovery_rules"],
            "Configuration": ["export_configuration", "import_configuration"],
            "Scripts": ["get_scripts", "execute_script"],
            "Services & SLA": ["get_services", "get_sla"],
            "Macros": ["get_host_macros", "get_global_macros", "set_host_macro", "delete_host_macro"],
            "Inventory": ["get_server_map", "get_product_summary", "get_server_load", "get_high_cpu_servers", "get_underloaded_servers", "get_provider_summary", "get_unknown_providers", "identify_providers", "generate_product_map"],
            "Rollback": ["get_rollback_history", "rollback_last", "rollback_by_index"],
            "Alerts": ["get_alerts", "get_alert_summary"],
            "Users": ["get_users"],
            "Proxies": ["get_proxies"],
            "Maps": ["get_maps", "get_map_detail"],
            "Media & Actions": ["get_media_types", "get_actions"],
            "Slack": ["send_slack_message", "send_slack_report"],
            "Costs": ["import_server_costs", "set_bulk_cost", "get_cost_summary"],
            "Traffic": ["detect_traffic_anomalies", "detect_traffic_drops", "get_traffic_report"],
            "Trends & Analysis": ["get_trends_batch", "get_server_dashboard", "compare_servers", "get_health_assessment", "get_shutdown_candidates", "get_capacity_planning"],
            "Geo": ["detect_regional_anomalies", "get_geo_traffic_trends", "get_service_uptime_report", "get_service_health_matrix", "get_traffic_drop_timeline", "get_expansion_report", "get_regional_density_map", "get_latency_estimate"],
            "Executive": ["get_executive_dashboard", "get_month_over_month", "get_fleet_risk_score", "get_sla_dashboard", "get_report_snapshot", "get_peak_analysis", "get_product_audit"],
            "Reports": ["generate_server_report", "generate_infra_report", "export_dashboard", "generate_full_report", "generate_html_report", "generate_ceo_report"],
            "Health": ["check_connection"],
        }
        lines = []
        for cat, tools in categories.items():
            write = [t for t in tools if t in WRITE_TOOLS]
            read = [t for t in tools if t not in WRITE_TOOLS]
            lines.append(f"## {cat}")
            if read:
                lines.append(f"Read: {', '.join(read)}")
            if write:
                lines.append(f"Write: {', '.join(write)}")
            lines.append("")
        return "\n".join(lines)

    @mcp.resource("zabbix://regions")
    def regions_resource() -> str:
        """Region-to-country mapping used by geo tools."""
        from zbbx_mcp.data import REGION_MAP
        lines = []
        for region, codes in sorted(REGION_MAP.items()):
            lines.append(f"{region}: {', '.join(sorted(codes))}")
        return "\n".join(lines)

    @mcp.resource("zabbix://env")
    def env_config_resource() -> str:
        """Current Zabbix MCP configuration (non-sensitive)."""
        safe_keys = [
            "ZABBIX_READ_ONLY", "DISABLED_TOOLS", "ZABBIX_COMPACT_TOOLS",
            "ZABBIX_COMPACT", "ZABBIX_RESPONSE_BUDGET", "ZABBIX_INSTANCES",
            "ZABBIX_HIDE_PRODUCTS", "ZABBIX_ALLOW_HTTP",
        ]
        lines = []
        for k in safe_keys:
            v = os.environ.get(k, "")
            if v:
                lines.append(f"{k}={v}")
        if not lines:
            lines.append("All defaults (no overrides set)")
        return "\n".join(lines)


def create_server() -> tuple[FastMCP, dict[str, ZabbixClient]]:
    """Build and configure the MCP server.

    Returns:
        Tuple of (mcp server, clients dict) so callers can manage lifecycle.
    """
    _logger.info("Starting zbbx-mcp", extra={"instance": INSTANCE_ID})

    mcp = FastMCP("zabbix")

    configs = load_all_configs()
    clients = {name: ZabbixClient(cfg) for name, cfg in configs.items()}
    resolver = InstanceResolver(clients)

    read_only, disabled_tools = load_global_policy()
    register_all(mcp, resolver, read_only=read_only, disabled_tools=disabled_tools)

    # Register MCP resources (static data clients can reference without tool calls)
    _register_resources(mcp)

    # Compact tool descriptions to save tokens (default: on)
    compact = os.environ.get("ZABBIX_COMPACT_TOOLS", "true").lower() in ("1", "true", "yes")
    if compact:
        saved = _compact_descriptions(mcp)
        if saved:
            _logger.info(f"Compacted tool descriptions: saved {saved} chars (~{saved // 4} tokens)")

    # Wrap all tool functions with analytics logging + response compression
    if hasattr(mcp, "_tool_manager") and hasattr(mcp._tool_manager, "_tools"):
        for tool in mcp._tool_manager._tools.values():
            if hasattr(tool, "fn"):
                original_fn = tool.fn

                @functools.wraps(original_fn)
                async def _compressed_wrapper(*args, _fn=original_fn, **kwargs):
                    result = await _fn(*args, **kwargs)
                    if isinstance(result, str):
                        return _compress_response(result)
                    return result

                tool.fn = logged(_compressed_wrapper)

    # Register cleanup for connection pools
    def _cleanup() -> None:
        try:
            loop = _asyncio.get_event_loop()
            if not loop.is_closed():
                for c in clients.values():
                    loop.run_until_complete(c.close())
        except (RuntimeError, OSError):
            pass  # Event loop closed or OS error during shutdown

    atexit.register(_cleanup)

    return mcp, clients


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Zabbix MCP server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default="stdio",
        help="Transport protocol (default: stdio)",
    )
    parser.add_argument(
        "--host", default="0.0.0.0", help="Host to bind to (default: 0.0.0.0)"
    )
    parser.add_argument(
        "--port", type=int, default=8000, help="Port to bind to (default: 8000)"
    )
    args = parser.parse_args()

    mcp, _clients = create_server()

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport=args.transport, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
