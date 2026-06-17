# ADR 061: Surface the version in the `/mcp` dialog (`--version` + label sync)

**Status:** Accepted
**Date:** 2026-06-16

## Problem

ADR 038 embedded the version in the server name (`FastMCP("zabbix
v<version>")`) so it shows in the MCP instructions header. But Claude
Code's **`/mcp` dialog labels each server by its config *key* in
`~/.claude.json`, not by the `serverInfo.name`** the server reports. The
zbbx-mcp entry is keyed plainly as `"zabbix"`, so the running version is
invisible exactly where an operator looks to confirm what's loaded —
which bit us this fleet repeatedly (e.g. running v1.13.0 while v1.14.0
was already on `main`). The sibling MCPs solved this (slk-mcp ADR 024,
gl-mcp's versioned key); this brings zbbx-mcp to parity.

## Decision

Two pieces, reusing the slk-mcp pattern adapted for the uv-run Python
entry:

1. **`--version` flag** on the server (`argparse action="version"`,
   prints the bare `__version__` and exits). Parity with the other MCPs'
   version CLIs, a manual check (`uv run zbbx-mcp --version`), and the
   thing the sync script asks.

2. **`scripts/sync-mcp-label.py`** — locates the entry by the path
   fragment `zbbx-mcp` in its **command or args** (the entry is invoked
   as `uv run --directory <dir> zbbx-mcp`, so unlike slk's Go binary the
   fragment is in args, and `command` is `uv`), asks that exact wired
   invocation `--version`, and renames the key to `zabbix v<version>`
   (matching `serverInfo.name`). If the subprocess can't answer (no uv on
   PATH), it falls back to the version in the wired `--directory`'s
   `pyproject.toml`. Idempotent, atomic write, keeps a `.bak`, and walks
   both the root and every per-project `mcpServers` block — the zabbix
   entry currently lives in two project containers.

Run it after a release bump, then reconnect `/mcp`. Stdlib only, so it
runs without the venv.

### Why ask the wired invocation rather than read our own pyproject

Faithful to slk's reasoning: the label should reflect what is actually
wired, not the tree the script happens to run from. Querying
`<command> <args> --version` reports the version of the exact checkout
the config points at; the pyproject fallback reads that same wired
`--directory`. Either way the label tracks reality, never a hand-typed
string that goes stale on the next bump.

## Test approach

`tests/test_sync_label.py` loads the hyphen-named script via importlib
and unit-tests the pure logic with the version lookup **dependency-
injected**, so no subprocess runs and `~/.claude.json` is never touched:
semver parsing (incl. noisy uv output and `+unknown` suffix), entry
matching (fragment in args vs command), `--directory` extraction,
pyproject parsing, container discovery, and `rename_in` (rename,
order-preservation, idempotence, skip-on-no-version). 18 tests.

## Consequences

- Tool count unchanged (162). Tests +18 (588 → 606).
- `/mcp` shows `zabbix v<version>` after a sync + reconnect; the version
  is finally visible where operators look.
- `uv run zbbx-mcp --version` available as a standalone check.
- The script edits `~/.claude.json` (user-local) at run time; it is never
  invoked by the server and carries no secrets, so it is safe in the
  public repo.

## Not included

- **Auto-running the sync on release.** Left manual (one command) — it
  mutates user-local config and the operator should reconnect `/mcp`
  deliberately. A `make`/CI hook can wrap it later.
- **`.mcp.json` project files.** The zabbix entry lives in
  `~/.claude.json`; the gl-mcp-style `~/Downloads/.mcp.json` path can be
  added to the scan if a project-file registration appears.
