# ADR 024: `get_telemetry_summary` tool + defensive FastMCP hook

**Status:** Accepted
**Date:** 2026-05-05

## Problem

The architecture review (post-v1.7.0) surfaced two items that ship
together cleanly:

1. **No tool-invocation telemetry surface.** The server has been
   logging per-call analytics to `~/.zbbx-mcp/analytics.log` since
   v1.4 (via the `logged()` decorator in `zbbx_mcp/logging.py`),
   but there is no in-product way to summarise the data. Operators
   wanting evidence for "which tools do users actually call?" had
   to grep / `jq` the JSONL log manually — high enough friction
   that the data sits unused.

2. **The startup hook that wraps registered tool functions reaches
   into private FastMCP state.** `_compact_descriptions` and the
   tool-wrapping loop both touched `mcp._tool_manager._tools` with
   inline `hasattr` checks duplicated at each site. A FastMCP minor
   bump that renames the private attribute would silently disable
   both compaction and per-call logging, with no warning surfaced
   at startup.

## Decision

### #7 — `get_telemetry_summary` MCP tool

New module `tools/telemetry.py` with one tool that reads the existing
analytics log (`~/.zbbx-mcp/analytics.log`, override via
`ZABBIX_ANALYTICS_FILE`) and renders:

```
| Tool | Calls | Errors | Err % | Avg ms | Max ms | Avg chars |
```

Parameters:

- `hours: int = 0` — look-back window; `0` means all-time.
- `top: int = 30` — max rows.
- `log_path: str = ""` — override the file path for one call.

Output sorts by call count descending. The header reports total
calls, unique tools, and total errors so operators see the
denominator at a glance.

Pure helper `_summarise_records(records, since_ts=None)` does the
aggregation: per-tool counts, error rate, avg / max latency, avg
response size. Handles both numeric epoch timestamps (server
internal) and ISO 8601 timestamps (the on-disk format) for the
`since_ts` filter. Robust to garbage `duration_ms` (treats
non-numeric as zero).

The tool lands in the `core` tier preset so it's available in every
session — self-introspection should not require switching tiers.

### #4 — Shared `_iter_registered_tools()` in `server.py`

The two FastMCP private-API touchpoints now share a single helper:

```python
def _iter_registered_tools(mcp) -> Iterable[Any]:
    manager = getattr(mcp, "_tool_manager", None)
    if manager is None:
        _logging.getLogger("zbbx_mcp").warning(...)
        return
    tools = getattr(manager, "_tools", None)
    if not isinstance(tools, dict):
        _logging.getLogger("zbbx_mcp").warning(...)
        return
    yield from tools.values()
```

`_compact_descriptions` and the tool-wrapping loop both iterate via
this helper. If FastMCP renames either private attribute, the
helper logs a warning and returns nothing — the server still starts,
just without compaction or per-tool wrapping. No `AttributeError`
explosion at import time, and the warning surfaces in the standard
log so the cause is discoverable.

## Test approach

7 new tests in `test_analytics.py` covering `_summarise_records`:

- Per-tool call counts and average latency.
- Error-rate percentage calculation.
- Output sorted by calls descending.
- Max-latency tracking across multiple calls.
- Garbage `duration_ms` treated as zero (defensive).
- `since_ts` filter drops records older than the cutoff.
- ISO 8601 timestamp parsing for the cutoff filter.

The `_iter_registered_tools()` helper is exercised by the existing
registration / smoke tests — server startup either succeeds (FastMCP
private API present) or surfaces the warning (private API absent),
both branches already covered by `test_server.py` start-up checks.

## Consequences

- 393 tests pass (386 pre-change + 7 new).
- Tool count goes from 155 to 156.
- `WRITE_TOOLS` unchanged — `get_telemetry_summary` is read-only.
- The `core` tier preset gains one tool; net handshake cost is
  negligible.
- The two FastMCP private-API touchpoints now degrade gracefully
  with a single shared warning path.
- No new env vars (`ZABBIX_ANALYTICS_FILE` already exists from
  the v1.4-era logging setup).
- Operators can now run `get_telemetry_summary(hours=168)` to see
  weekly tool usage — the foundation for evidence-based tier and
  deprecation decisions (item #8 from the architecture review).

## Not included

- A telemetry-driven recommendation engine ("these tools haven't
  been called in 30 days — consider removing"). Surface the data
  first; let operators interpret.
- Cross-process aggregation. The analytics log is per-installation;
  multi-deployment aggregation would require shipping logs to a
  central store. Deferred unless a real use case appears.
- Per-instance breakdown in the summary output. The log records
  the instance per call; the summary tool currently aggregates
  across instances. Add a `group_by="instance"` arg in a follow-up
  if needed.
- Server-side dashboard. The MCP tool returns markdown to the LLM
  client; rendering richer charts is a client concern.
