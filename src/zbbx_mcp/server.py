import atexit
import asyncio as _asyncio
import os
import re

from mcp.server.fastmcp import FastMCP
from zbbx_mcp.config import load_all_configs, load_global_policy
from zbbx_mcp.client import ZabbixClient
from zbbx_mcp.resolver import InstanceResolver
from zbbx_mcp.tools import register_all
from zbbx_mcp.logging import setup_logging, setup_sentry, INSTANCE_ID, logged

# Regex to strip Args/Parameters section from docstrings
_ARGS_RE = re.compile(r"\n\s*Args:\s*\n.*", re.DOTALL)


def _compact_descriptions(mcp: FastMCP) -> int:
    """Strip Args section from tool descriptions to save tokens.

    The Args info is redundant — parameter names, types, and defaults
    are already in the JSON schema sent to the LLM.

    Returns number of chars saved.
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
    return saved

_logger = setup_logging()
setup_sentry()


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

    # Compact tool descriptions to save tokens (default: on)
    compact = os.environ.get("ZABBIX_COMPACT_TOOLS", "true").lower() in ("1", "true", "yes")
    if compact:
        saved = _compact_descriptions(mcp)
        if saved:
            _logger.info(f"Compacted tool descriptions: saved {saved} chars (~{saved // 4} tokens)")

    # Wrap all tool functions with analytics logging
    if hasattr(mcp, "_tool_manager") and hasattr(mcp._tool_manager, "_tools"):
        for tool in mcp._tool_manager._tools.values():
            if hasattr(tool, "fn"):
                tool.fn = logged(tool.fn)

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
