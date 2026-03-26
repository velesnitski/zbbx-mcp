# REVIEW.md

PR review guidelines for Claude Code.

## Automated checks

Before approving any PR, verify:

1. **Tests pass** — `uv run pytest` (all 128 tests, ~2s)
2. **Tool count matches** — `EXPECTED_TOOLS` in `test_registration.py` and count in `test_server.py`
3. **No sensitive data** — grep for real hostnames, company names, product naming patterns
4. **No `tasks.md` in diff** — must never be committed

## What to check

### Security
- No hardcoded tokens, passwords, or API keys
- No real server hostnames or internal domain names in code, docstrings, or tests
- No VPN protocol names (use generic labels)
- Write operations must be in `WRITE_TOOLS` and gated by `ZABBIX_READ_ONLY`
- Rollback: mutations must call `snapshot_and_record()` before changing data

### Code quality
- Country filtering uses `extract_country()` exact match, not substring
- New tools follow the `register(mcp, resolver, skip)` pattern
- Error handling: `(httpx.HTTPError, ValueError)` caught, returns string
- No circular imports (especially `classify.py` must not import from `tools/`)
- Output respects token budget — uses `max_results`, groups repetitive data

### Documentation
- README tool table updated if tools added/removed
- Tool count in README matches actual count
- CHANGELOG updated for user-facing changes
- Docstrings have description + Args block

## Common issues

- **Forgot to update tool count** — tests will fail with "Expected N tools, got M"
- **Substring country filter** — `country in hostname` matches partial strings (e.g., "in" matches "portainer")
- **Sensitive data in examples** — use `srv-nl01`, `srv-us01`, never real naming patterns
- **Missing skip gate** — every tool must be wrapped in `if "tool_name" not in skip:`
