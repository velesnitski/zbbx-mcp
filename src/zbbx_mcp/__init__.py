"""Zabbix MCP server for Claude Code, Codex CLI, and any MCP-compatible client."""

__all__ = ["__version__", "create_server", "ZabbixClient", "ZabbixConfig"]

__version__ = "1.3.0"


def create_server():  # noqa: ANN201 – deferred import
    """Create and return the MCP server instance."""
    from zbbx_mcp.server import create_server as _create

    return _create()
