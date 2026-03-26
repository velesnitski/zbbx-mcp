# AGENTS.md

This file provides guidance to WARP (warp.dev) when working with code in this repository.

## Commands

**Setup (development):**
```bash
pip install -e ".[test]"
# or with uv:
uv pip install -e ".[test]"
```

**Run all tests:**
```bash
pytest
```

**Run a single test file:**
```bash
pytest tests/test_config.py
```

**Run a single test by name:**
```bash
pytest tests/test_server.py::TestServerStartup::test_tools_list
```

**Run the server locally (stdio mode, requires env vars):**
```bash
ZABBIX_URL=https://your-zabbix.example.com ZABBIX_TOKEN=your_token zbbx-mcp
```

**Run the server in HTTP mode (for n8n/Langchain):**
```bash
ZABBIX_URL=... ZABBIX_TOKEN=... zbbx-mcp --transport sse --port 8000
```

**Test server startup without a real Zabbix instance:**
```bash
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"capabilities":{},"clientInfo":{"name":"test","version":"1.0.0"},"protocolVersion":"2024-11-05"}}' \
  | ZABBIX_URL="https://test.zabbix.example.com" ZABBIX_TOKEN="test-token" python -m zbbx_mcp.server
```

There is no linting or type checking configured in `pyproject.toml`. The test suite uses `pytest` with `asyncio_mode = "auto"`.

## Architecture

### Overview
This is a [Model Context Protocol (MCP)](https://modelcontextprotocol.io) server that exposes Zabbix monitoring data as tools callable by LLMs. It uses the `FastMCP` class from the `mcp` library as the server framework.

### Startup flow (`server.py`)
`create_server()` → loads all configs → creates one `ZabbixClient` per instance → creates an `InstanceResolver` → calls `register_all()` to attach all tools → wraps every tool function with `logged()` (analytics) and `_compress_response()` (token budget).

### Multi-instance support
`config.py` reads `ZABBIX_INSTANCES=prod,staging` and loads per-instance env vars (`ZABBIX_PROD_URL`, `ZABBIX_PROD_TOKEN`, etc.). The first instance falls back to unprefixed `ZABBIX_URL`/`ZABBIX_TOKEN`. `InstanceResolver` routes calls to the correct `ZabbixClient` based on the optional `instance` parameter present on every tool.

### Tool registration pattern
Every file under `src/zbbx_mcp/tools/` exports a single `register(mcp, resolver, skip)` function. Inside it, each tool is conditionally registered as a `@mcp.tool()` async function gated by `if "tool_name" not in skip`. The `skip` set is built from `WRITE_TOOLS` (when `ZABBIX_READ_ONLY=true`) and `DISABLED_TOOLS`. Adding a new tool means adding its `@mcp.tool()` block inside `register()` and listing it in `WRITE_TOOLS` in `tools/__init__.py` if it mutates data.

### ZabbixClient (`client.py`)
Wraps Zabbix's JSON-RPC API via `httpx.AsyncClient` with HTTP/2 and a connection pool. Key methods:
- `call(method, params)` — single JSON-RPC call
- `call_many(calls)` — parallel calls via `asyncio.gather`
- `snapshot_and_record(action, object_type, object_id)` — fetches current object state before a mutation and records it in the `RollbackLog`
- `record_create(object_type, object_id)` — records creates without a snapshot
- Simple TTL cache (`_get_cached`/`_set_cache`) used for the "all enabled hosts" query in report modules

### Rollback system (`rollback.py`)
Each `ZabbixClient` holds its own `RollbackLog` (bounded deque, max 50 entries). Before any write operation (update/delete), the tool calls `snapshot_and_record()` to capture the prior state. `SNAPSHOT_CONFIG` maps object types to their API get/create/update/delete methods. The `rollback_last` and `rollback_by_index` tools read from this log to undo operations.

### Report data pipeline (`data.py`)
`fetch_all_data()` is the central data-fetch function used by all report tools. It fetches dashboards and hosts in parallel (phase 1), then fires ~13 `item.get` calls simultaneously (phase 2) for CPU, memory, traffic, service check items, macros, and templates. Missing traffic data is filled with a fallback name-based search (phase 3). Results are assembled into `ServerRow` dataclasses and sorted (dashboard hosts first).

### Host classification (`classify.py`)
Standalone module (no imports from `tools/`) to avoid circular imports. `classify_host(groups)` maps Zabbix host group names to `(product, tier)` tuples using an optional `ZABBIX_PRODUCT_MAP` JSON config. `detect_provider(ip)` matches IPs against hardcoded CIDR ranges for known hosting providers (OVH, Hetzner, AWS, Scaleway, Vultr, etc.).

### Key environment variables
- `ZABBIX_URL` / `ZABBIX_TOKEN` — required
- `ZABBIX_READ_ONLY=true` — blocks all tools in `WRITE_TOOLS`
- `DISABLED_TOOLS=foo,bar` — removes specific tools entirely
- `ZABBIX_PRODUCT_MAP` — JSON file or inline JSON mapping host group names to `[product, tier]`
- `ZABBIX_COMPACT=true` — strips markdown from responses (~40% token savings)
- `ZABBIX_RESPONSE_BUDGET=6000` — max chars per tool response (0 = unlimited)
- `ZABBIX_COMPACT_TOOLS=true` (default on) — strips `Args:` sections from tool docstrings
- `ZABBIX_ALLOW_HTTP=1` — allows non-HTTPS URLs
- `ZABBIX_INSTANCES=prod,staging` — enables multi-instance mode

### Test notes
- `test_server.py` spawns the real server as a subprocess and sends JSON-RPC messages over stdio. It asserts exactly **88 tools** are registered — update this assertion when adding/removing tools.
- Tests use `unittest.mock.patch.dict(os.environ, ..., clear=True)` to isolate config loading from the ambient environment.
