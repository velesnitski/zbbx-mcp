"""Zabbix MCP server for Claude Code, Codex CLI, and any MCP-compatible client."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

__all__ = ["__version__", "create_server", "ZabbixClient", "ZabbixConfig"]

try:
    __version__ = _pkg_version("zbbx-mcp")
except PackageNotFoundError:
    # Editable / source checkout where the dist isn't installed; fall back so
    # imports still work but the value will be obviously placeholder.
    __version__ = "0.0.0+unknown"


def create_server():  # noqa: ANN201 – deferred import
    """Create and return the MCP server instance."""
    from zbbx_mcp.server import create_server as _create

    return _create()
