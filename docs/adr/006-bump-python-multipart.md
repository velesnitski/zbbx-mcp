# ADR 006: Bump python-multipart to 0.0.26 (CVE-2026-40347)

**Status:** Accepted
**Date:** 2026-04-21

## Problem

Dependabot flagged `python-multipart < 0.0.26` in `uv.lock` with
CVE-2026-40347 (moderate severity). The package is a transitive
dependency, pulled in by `mcp 1.25.0` for its streamable HTTP transport.

We do not expose multipart parsing directly — the MCP server runs over
stdio for all shipped deployments — but the vulnerable code path is
still reachable through the bundled `starlette`/`mcp` HTTP transport if
a user ever starts the server in HTTP mode, and the advisory gate in
the public repo will keep firing until the lock is bumped.

## Decision

Pin the lock forward with `uv lock --upgrade-package python-multipart`
(0.0.22 → 0.0.26). No change to `pyproject.toml`: the constraint stays
implicit via `mcp`, and 0.0.26 satisfies mcp's existing requirement.

## Consequences

- CVE-2026-40347 closes; GitHub advisory clears on next push.
- No code changes, no API surface shift; all 177 tests pass.
- Future `mcp` upgrades may re-pin multipart; the lock file will
  re-resolve then without manual intervention.
