# ADR 084: Fix a stdio-shutdown race in the server subprocess test

**Status:** Accepted
**Date:** 2026-07-17

## Problem

After the `mcp` 1.28.1 bump (ADR 083), CI went red on
`test_server.py::TestServerStartup::test_tools_list` — but *only* in CI, and
only on 1.28.1. Locally the test passed on 1.28.1 every time, including ten
back-to-back runs. The failure captured just the `initialize` response; the
`tools/list` reply (id 2) never arrived:

```
AssertionError: No tools/list response found in:
  [{... "serverInfo": {"name": "zabbix v1.16.17", "version": "1.28.1"}}]
```

The cause was in the test harness, not the server. `_run_jsonrpc` built the
whole batch of JSON-RPC messages, handed them to `subprocess.run(input=...)`,
and thereby **closed stdin immediately** — the server saw `initialize`,
`notifications/initialized`, `tools/list`, and then EOF, effectively at once.
The hardened MCP SDK (the same lifecycle tightening that fixed the CVE round)
tears the stdio session down on EOF, and that teardown raced the still-pending
`tools/list` handler. On a fast local machine the handler won; on a slow,
loaded CI runner the shutdown won — deterministically. The passing-locally,
failing-in-CI split is the signature of exactly this kind of race.

## Decision

Rewrite `_run_jsonrpc` to hold stdin **open** until the work is done. It now
spawns the server with `subprocess.Popen`, drains stdout on a reader thread,
writes the messages, and **waits until every request id has a matching
response** (or a timeout) *before* closing stdin. Closing stdin — and the
EOF-driven shutdown — happens only after the responses we asked for are in
hand, so there is nothing left to race. `stderr` is routed to `DEVNULL` so a
chatty log stream can't fill its pipe buffer and deadlock the reader.

This is purely a test-synchronization fix; the server itself was correct. It
also makes the harness robust to any future SDK that defers request handling
further, rather than papering over one version's timing.

## Test approach

The change is to the harness the server tests already exercise. Locally the
suite passes and `test_server.py` is deterministic across repeated runs; the
real proof is CI going green on the loaded Linux runner where the race
reproduced. Full suite unchanged at 759.

## Consequences

- CI is deterministic again on `mcp` 1.28.1.
- The subprocess handshake — the gate that protects the FastMCP private-API
  contract we depend on (ADR 082/083) — no longer produces false negatives, so
  it can be trusted to catch a *real* dispatch break.

## Not included

- **Reusing an official MCP client** for the handshake instead of hand-rolled
  JSON-RPC over pipes. It would be more faithful, but it couples the test to
  more SDK surface than "does the server list its tools over stdio" needs;
  deferred.
