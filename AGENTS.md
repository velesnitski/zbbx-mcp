# AGENTS.md

This file provides guidance to WARP (warp.dev) when working with code in this repository.

## Commands

**Setup (development):**
```bash
uv pip install -e ".[test]"
# or with pip:
pip install -e ".[test]"
```

**Run all tests (128 tests, ~2s):**
```bash
uv run pytest
```

**Run a specific test file or test by name:**
```bash
uv run pytest tests/test_registration.py
uv run pytest tests/test_server.py
uv run pytest -k "test_name"
```

**Test server startup without a real Zabbix instance:**
```bash
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"capabilities":{},"clientInfo":{"name":"test","version":"1.0.0"},"protocolVersion":"2024-11-05"}}' \
  | ZABBIX_URL="https://test.zabbix.example.com" ZABBIX_TOKEN="test-token" python -m zbbx_mcp.server
```

No linting or type checking is configured. The test suite uses `pytest` with `asyncio_mode = "auto"`.

## Architecture

### Overview
This is a [Model Context Protocol (MCP)](https://modelcontextprotocol.io) server that exposes Zabbix monitoring data as tools callable by LLMs. It uses the `FastMCP` class from the `mcp` library as the server framework. Python 3.10+, async httpx HTTP/2 client.

### Key source modules
- `server.py` — entry point, `create_server()` factory
- `client.py` — `ZabbixClient` wrapping Zabbix JSON-RPC API
- `config.py` — env var loading, multi-instance config
- `resolver.py` — `InstanceResolver` routes calls to the right `ZabbixClient`
- `data.py` — shared data fetching pipeline, `ServerRow`, `extract_country()`, metric key constants
- `classify.py` — standalone host classification (no `tools/` imports — avoids circular import)
- `rollback.py` — `RollbackLog` deque + `SNAPSHOT_CONFIG`
- `logging.py` — structured JSON error log + analytics log, `logged()` decorator, Sentry integration
- `formatters.py` — shared Zabbix entity formatters (`format_host_list`, `format_problem_list`, etc.)
- `utils.py` — `format_results()`, `resolve_group_ids()`, `ROLLBACK_STRIP_FIELDS`
- `excel.py` — Excel workbook generation for `generate_full_report`
- `tools/*.py` — each file exports a single `register(mcp, resolver, skip)` function
- `tools/__init__.py` — `register_all()`, `WRITE_TOOLS` set

### Startup flow
`create_server()` → `load_all_configs()` → one `ZabbixClient` per instance → `InstanceResolver` → `register_all()` → wraps every tool with `logged()` (analytics) and `_compress_response()` (token budget).

### Tool registration pattern
Every file under `tools/` exports `register(mcp, resolver, skip)`. Each tool is conditionally added as `@mcp.tool()` async function gated by `if "tool_name" not in skip`. The `skip` set is built from `WRITE_TOOLS` (when `ZABBIX_READ_ONLY=true`) and `DISABLED_TOOLS`.

### Adding a new tool
1. Add `@mcp.tool()` async function inside `register()` in the appropriate `tools/*.py` file
2. Gate with `if "tool_name" not in skip:`
3. If the tool mutates data, add it to `WRITE_TOOLS` in `tools/__init__.py`
4. Add the tool name to `EXPECTED_TOOLS` in `tests/test_registration.py`
5. Update the tool count assertion in `tests/test_server.py`
6. Update the tool table in `README.md`
7. Run `uv run pytest` — all 128 tests must pass

### ZabbixClient (`client.py`)
Wraps Zabbix's JSON-RPC API via `httpx.AsyncClient` with HTTP/2 and a connection pool. Key methods:
- `call(method, params)` — single JSON-RPC call
- `call_many(calls)` — parallel calls via `asyncio.gather`
- `snapshot_and_record(action, object_type, object_id)` — fetches current object state before a mutation and records it in the `RollbackLog`
- `record_create(object_type, object_id)` — records creates without a snapshot
- TTL cache (`_get_cached`/`_set_cache`) used for the "all enabled hosts" query in report modules

### Rollback system (`rollback.py`)
Each `ZabbixClient` holds its own `RollbackLog` (bounded deque, max 50 entries). Before any write (update/delete), the tool calls `snapshot_and_record()` to capture prior state. `SNAPSHOT_CONFIG` maps object types to their API methods. `rollback_last` and `rollback_by_index` tools undo operations.

### Report data pipeline (`data.py`)
`fetch_all_data()` fetches dashboards and hosts in parallel (phase 1), fires ~13 `item.get` calls simultaneously (phase 2) for CPU, memory, traffic, macros, and templates. Missing traffic data is filled with a fallback name-based search (phase 3). Results are assembled into `ServerRow` dataclasses and sorted (dashboard hosts first).

### Multi-instance support
`config.py` reads `ZABBIX_INSTANCES=prod,staging` and loads per-instance env vars (`ZABBIX_PROD_URL`, `ZABBIX_PROD_TOKEN`, etc.). The first instance falls back to unprefixed `ZABBIX_URL`/`ZABBIX_TOKEN`. `InstanceResolver.resolve(instance)` picks the right client.

### Key environment variables
- `ZABBIX_URL` / `ZABBIX_TOKEN` — required
- `ZABBIX_READ_ONLY=true` — blocks all tools in `WRITE_TOOLS`
- `DISABLED_TOOLS=foo,bar` — removes specific tools entirely
- `ZABBIX_PRODUCT_MAP` — JSON file or inline JSON mapping host group names to `[product, tier]`
- `ZABBIX_COMPACT=true` — strips markdown from responses (~40% token savings)
- `ZABBIX_RESPONSE_BUDGET=6000` — max chars per tool response (0 = unlimited)
- `ZABBIX_COMPACT_TOOLS=true` (default on) — strips `Args:` sections from tool docstrings at runtime
- `ZABBIX_ALLOW_HTTP=1` — allows non-HTTPS URLs
- `ZABBIX_INSTANCES=prod,staging` — enables multi-instance mode

### Test notes
- `test_registration.py` — fast unit test; update `EXPECTED_TOOLS` and run this when adding/removing tools
- `test_server.py` — spawns the real server as a subprocess and sends JSON-RPC messages over stdio; asserts exactly **96 tools** are registered
- Tests use `unittest.mock.patch.dict(os.environ, ..., clear=True)` to isolate config loading from the ambient environment

## Rules
- **Never commit `tasks.md`** — it's in `.gitignore` and contains internal planning notes
- **No sensitive data in code or git** — no real hostnames, company names, product names, or server naming patterns; use generic examples like `srv-nl01`, `srv-us01` in docstrings and tests
- **No hardcoded service identifiers** — use generic labels (Primary, Secondary, Tertiary) in public output
- **Country filter** — always use `extract_country(hostname)` for exact 2-letter match; never use substring `country in hostname`
- **Compact by default** — tools should return concise output; use `max_results` with sensible defaults, group repetitive entries, show omitted count
- **Error handling** — catch `(httpx.HTTPError, ValueError)`, return a user-friendly string, never raise
- **Token budget** — responses are truncated by `ZABBIX_RESPONSE_BUDGET` (default 6000 chars); design output to fit
- **`classify.py` must not import from `tools/`** — circular import risk
- **Args in docstrings** — keep `Args:` blocks in code even though `ZABBIX_COMPACT_TOOLS=true` strips them at runtime; the JSON schema still needs them

## Branching
- `dev` — active development branch
- `main` — stable, always fast-forward merged from `dev`
- Always confirm before `git push`

## Code style
- No linter configured — follow existing patterns
- All tool implementations must be async functions with type hints on signatures
- Minimal docstrings: tool description + Args block (no Returns/Raises sections)
