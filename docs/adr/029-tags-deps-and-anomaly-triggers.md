# ADR 029: Tag filtering, dependency surfacing, and anomaly-trigger discovery

**Status:** Accepted
**Date:** 2026-05-21

## Problem

Three Zabbix-side capabilities that we hadn't yet plumbed into the
MCP, all relevant for operators running Zabbix 6.x:

1. **Tags.** Zabbix 6.x supports rich tags on hosts, items, and
   triggers (e.g. `tier:edge`, `role:edge`, `env:prod`). The MCP
   only filtered by country / product / hostgroup — no tag
   plumbing. Operators wanting to scope "all `env:prod` hosts" or
   "all `severity:critical` problems" had no way to do it.
2. **Trigger dependencies.** Zabbix supports trigger-level
   dependencies — Trigger A depends on B means "A doesn't fire when
   B is already firing". Useful for cascading alert suppression.
   `get_triggers` ignored this metadata entirely.
3. **Native anomaly triggers (6.4+).** Zabbix 6.4 added
   `anomalystl()`, `baselinewma()`, `baselinedev()`, `trendstl()`,
   and `forecast()` trigger functions for built-in time-series
   anomaly detection. The MCP shipped its own client-side
   detectors (`detect_loss_drift`, `detect_disruption_wave`) but
   had no way to surface what server-side anomaly alerting was
   already configured.

## Decision

Three small additions shipped together as v1.8.5.

### 1. Shared `tag_filter` module + plumbing into four tools

`src/zbbx_mcp/tag_filter.py` exposes one function:
`parse_tag_filter(spec) -> list[dict]`. Operators pass
`tags="key:value,key2:value2"`; the parser returns the
`[{tag, value, operator}, ...]` array Zabbix expects.

Format:

| MCP input | Parsed output | Zabbix semantics |
|-----------|---------------|------------------|
| `"role:edge"` | `[{tag:"role", value:"edge", operator:0}]` | equals |
| `"role"` or `"role:"` | `[{tag:"role", value:"", operator:4}]` | exists |
| `"role:edge,env:prod"` | two entries | AND-combined (evaltype 0) |

Plumbed into the four highest-value call sites:

- `search_hosts` (host.get)
- `get_problems` (problem.get)
- `get_active_problems` (problem.get)
- `get_triggers` (trigger.get)

Extending to additional tools (e.g. `search_items`,
`get_host_items`) is a one-line import + payload merge — done
incrementally on demand.

Other Zabbix operators (`contains`, `not equals`, `not contains`,
`not exists`) are not exposed yet. Adding them would need either
extra MCP parameters or richer spec syntax — defer until there's a
concrete need.

### 2. `with_dependencies` flag on `get_triggers`

New `with_dependencies: bool = False` arg. When True,
`get_triggers` passes `selectDependencies` to Zabbix and renders a
"depends on" line under each trigger with up to 5 dep descriptions
(plus "+N more" tail).

Operators on Zabbix deployments that don't use trigger dependencies
see zero behaviour change. On deployments that do, dependent
triggers masked by a parent firing become visible.

### 3. New tool `get_anomaly_triggers(only_active=True)`

Calls `trigger.get` with `expandExpression=True`, then filters
client-side for expressions containing any of the five 6.4 anomaly
function names. Returns a compact markdown table with the trigger
description, host(s), which function(s) it uses, current state,
severity, and last-change timestamp.

`only_active=True` (default) further restricts to
problem-state triggers; flip to `False` to inspect dormant
anomaly rules too.

Lands in the `ops` tier (incident-response surface, alongside the
client-side detectors it complements).

## Test approach

8 new pure-helper tests in `test_analytics.py` cover
`parse_tag_filter`: empty input, single key:value, multiple pairs
AND-combined, whitespace tolerance, bare-key "exists",
empty-value-after-colon "exists", empty-key skip, trailing-comma
handling.

The async tool wrappers are covered by the registration / smoke
test (asserts 161 tools and presence of `get_anomaly_triggers`).
Tag-arg plumbing on the four existing tools is configuration
threading — exercised end-to-end by anyone who passes `tags` and
verified by the unchanged behaviour of all existing tests (no
regression).

## Consequences

- Tool count 160 → 161.
- Test count 439 → 447.
- `WRITE_TOOLS` unchanged.
- No new env vars.
- API-compat: every change is a new optional arg with a no-op
  default — existing callers see zero behaviour change.
- One new module (`tag_filter.py`) — 15 mypy-clean source files
  total.

## Not included

- **Wiring `tags` into every detection tool.** The four highest-
  value places are done. Extending to `search_items`,
  `get_host_items`, `detect_traffic_drops`, etc. is mechanical and
  can land per-tool on demand.
- **Richer tag operators (contains / not-equals / not-contains).**
  The current `equals` and `exists` cover ~95% of practical
  filters. Operators who need exotic forms can build the
  `tags` array manually via the Zabbix UI for now.
- **Trigger-dependency tree walk.** `with_dependencies` shows
  one level. A multi-level walk would need recursion and risks
  blowing up output size for deeply-nested dep trees — defer
  until someone actually has such a tree.
- **Filter for triggers using anomaly functions in `get_triggers`**
  itself. The new tool `get_anomaly_triggers` is the focused
  surface; layering function-substring filtering onto
  `get_triggers` would add complexity for marginal benefit.
- **A "compose anomaly + tags" composite tool.** Both surfaces
  exist independently; an LLM client can call them together when
  it needs to.
