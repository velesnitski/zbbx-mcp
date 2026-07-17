# ADR 083: `mcp` 1.28.1 + `click` — second CVE round, and a cap correction

**Status:** Accepted
**Date:** 2026-07-17

## Problem

Auditing the lockfile immediately after ADR 082 (which bumped `mcp` to 1.27.2
for CVE-2026-52869) surfaced two more advisories — and one embarrassing
self-inflicted one:

1. **CVE-2026-59950 (High) — `mcp <= 1.28.0`, fixed 1.28.1.** The *deprecated*
   WebSocket server transport (`mcp.server.websocket.websocket_server`)
   accepted the handshake with no `Host`/`Origin` validation, so a malicious
   web page could open a session to a reachable local/LAN server and invoke
   its tools. **We are not actually exploitable:** this transport is not
   reachable through `FastMCP` and we never wire it into an ASGI app — the
   advisory itself states stdio/SSE/streamable-HTTP servers are unaffected.
   But the vulnerable code ships in the pinned version, so audits flag it, and
   the right hygiene is to move off it.

2. **PYSEC-2026-2132 (High) — `click <= 8.3.2`, fixed 8.3.3.** Command
   injection in `click.edit()`. `click` is transitive (via `uvicorn`); we do
   not call `click.edit()`, but the vulnerable version is in the tree.

3. **ADR 082's cap re-committed its own anti-pattern — within the hour.** That
   ADR raised the pin to `mcp>=1.27.2,<1.28.0` and, in the same breath, warned
   that an over-tight upper bound "becomes a security liability". The `<1.28.0`
   bound then immediately blocked the 1.28.1 fix for CVE-2026-59950. The lesson
   was written down and violated in the same commit.

## Decision

- **`mcp>=1.28.1,<2.0.0`.** 1.28.1 clears *both* mcp CVEs (it is ≥ 1.27.2 and
  = 1.28.1). The upper bound is widened to the **major** boundary rather than
  the next minor, so a future security patch on the 1.x line is no longer
  blocked by our own constraint. This is safe because FastMCP's tool-dispatch
  is the public, semver-stable contract we actually rely on, and our only
  private-API coupling — the compression/logging layer that walks
  `_tool_manager._tools` and rebinds `tool.fn` — **already degrades
  gracefully**: `_iter_registered_tools` warns and disables wrapping if those
  internals move, so a future minor's worst case is "compression off", never a
  crash. The `test_server.py` subprocess handshake is the CI gate that catches
  a real dispatch break.
- **`click` → 8.4.2, lockfile-only.** A transitive bump past the fixed 8.3.3,
  no manifest change (the pattern established by ADR 031).

## Test approach

No new tests. `test_server.py` (real JSON-RPC handshake through a subprocess)
and `test_registration.py` pin the FastMCP contract this bump could break; both
green under 1.28.1. A `pip-audit` run over the re-locked tree reports **no
known vulnerabilities**. Full suite unchanged at 759.

## Consequences

- The dependency tree is clean per OSV/PyPI audit.
- The mcp constraint no longer blocks 1.x security patches; the graceful-
  degradation fallback plus the subprocess test are the safety net that makes
  the wider bound defensible.
- Tool count unchanged (163). v1.16.16 (ADR 082) was a partial fix; this
  completes it.

## Not included

- **Migrating off / disabling any deprecated transport in our own code.** We
  never expose `websocket_server`, so there is nothing to migrate; the concern
  is purely the shipped package version, which the bump resolves.
- **A reachability-aware audit gate in CI.** `pip-audit` flags by version, not
  by whether the vulnerable path is reachable (e.g. it flagged 59950 though we
  cannot hit it). A gate that reasoned about reachability would cut noise, but
  that is its own project.
