# CLAUDE.md

Instructions for Claude Code when working in this repository.

## Project

Zabbix MCP server — 96 tools across 33 modules. Python 3.10+, FastMCP framework, async httpx HTTP/2 client.

## Commands

```bash
uv run pytest                              # run all tests (128 tests, ~2s)
uv run pytest tests/test_registration.py   # tool registration only
uv run pytest tests/test_server.py         # JSON-RPC subprocess test
uv run pytest -k "test_name"               # single test by name
```

## Architecture

- `src/zbbx_mcp/server.py` — entry point, `create_server()` factory
- `src/zbbx_mcp/client.py` — `ZabbixClient` wrapping JSON-RPC API
- `src/zbbx_mcp/config.py` — env var loading, multi-instance config
- `src/zbbx_mcp/data.py` — shared data fetching, `ServerRow`, `extract_country()`, metric key constants
- `src/zbbx_mcp/classify.py` — standalone host classification (no tools/ imports)
- `src/zbbx_mcp/tools/*.py` — each file exports `register(mcp, resolver, skip)`
- `src/zbbx_mcp/tools/__init__.py` — `register_all()`, `WRITE_TOOLS` set

## Adding a new tool

1. Add `@mcp.tool()` async function inside `register()` in the appropriate `tools/*.py` file
2. Gate with `if "tool_name" not in skip:`
3. If the tool mutates data, add it to `WRITE_TOOLS` in `tools/__init__.py`
4. Add the tool name to `EXPECTED_TOOLS` in `tests/test_registration.py`
5. Update the tool count assertion in `tests/test_server.py`
6. Update the tool table in `README.md`
7. Run `uv run pytest` — all tests must pass

## Rules

- **Never commit `tasks.md`** — it's in `.gitignore` and contains internal planning notes
- **No sensitive data in code or git** — no real hostnames, company names, product names, or server naming patterns. Use generic examples like `srv-nl01`, `srv-us01` in docstrings and tests
- **No VPN protocol names** — use generic labels (Primary, Secondary, Tertiary) in public output
- **Country filter** — always use `extract_country(hostname)` for exact 2-letter match, never substring `country in hostname`
- **Compact by default** — tools should return concise output. Use `max_results` with sensible defaults, group repetitive entries, show omitted count
- **Error handling** — catch `(httpx.HTTPError, ValueError)`, return user-friendly string, never raise
- **Token budget** — responses are truncated by `ZABBIX_RESPONSE_BUDGET` (default 6000 chars). Design output to fit
- **Imports** — `classify.py` must not import from `tools/` (circular import risk)
- **Tests** — 128 tests must pass. Tool count assertions must match actual registered tools

## Branching

- `dev` — active development branch
- `main` — stable, always fast-forward merged from `dev`
- Always confirm before `git push`

## Code style

- No linter configured — follow existing patterns
- Async functions for all tool implementations
- Type hints on function signatures
- Minimal docstrings: tool description + Args block (no Returns/Raises)
- `ZABBIX_COMPACT_TOOLS=true` strips Args from descriptions at runtime, so keep them in code
