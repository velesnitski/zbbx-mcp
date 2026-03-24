from mcp.server.fastmcp import FastMCP
from zbbx_mcp.config import load_all_configs, load_global_policy
from zbbx_mcp.client import ZabbixClient
from zbbx_mcp.resolver import InstanceResolver
from zbbx_mcp.tools import register_all


def create_server() -> tuple[FastMCP, dict[str, ZabbixClient]]:
    """Build and configure the MCP server.

    Returns:
        Tuple of (mcp server, clients dict) so callers can manage lifecycle.
    """
    mcp = FastMCP("zabbix")

    configs = load_all_configs()
    clients = {name: ZabbixClient(cfg) for name, cfg in configs.items()}
    resolver = InstanceResolver(clients)

    read_only, disabled_tools = load_global_policy()
    register_all(mcp, resolver, read_only=read_only, disabled_tools=disabled_tools)

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
