import atexit
import asyncio as _asyncio

from mcp.server.fastmcp import FastMCP
from zbbx_mcp.config import load_all_configs, load_global_policy
from zbbx_mcp.client import ZabbixClient
from zbbx_mcp.resolver import InstanceResolver
from zbbx_mcp.tools import register_all
from zbbx_mcp.logging import setup_logging, setup_sentry, INSTANCE_ID, logged

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
