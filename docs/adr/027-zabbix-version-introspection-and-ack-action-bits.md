# ADR 027: Zabbix-version introspection + extended acknowledge actions

**Status:** Accepted
**Date:** 2026-05-21

## Problem

The MCP wraps the Zabbix API but assumes a "lowest-common-denominator"
feature set. Over the past three years Zabbix has shipped meaningful
additions whose availability depends on the server version:

- 5.2: suppress / unsuppress actions on `event.acknowledge`.
- 5.4: API token CRUD (`token.get`, `token.create`).
- 6.0: unacknowledge + change-severity action bits on
  `event.acknowledge`.
- 6.4: cause / symptom rank actions.
- 7.0: connector API (data streaming), proxy groups, HA cluster
  introspection via `core.ha.get`.

Two gaps surfaced:

1. **No version-aware feedback.** `check_connection` returns the
   version string but no caller knew what that meant in terms of
   feature availability. Operators had to memorise version gates.
2. **`acknowledge_problem` only wrote the `ack` bit.** Even when
   talking to Zabbix 6.4 (which supports six other action bits), we
   couldn't close-and-ack, change severity in the same call, or
   undo an accidental ack. Workflows that should be one MCP call
   required a Zabbix-UI roundtrip.

## Decision

Two related changes shipping together as v1.8.3.

### 1. New tool: `get_zabbix_version`

Wraps `apiinfo.version` and emits a markdown report listing version
plus an availability matrix for notable optional APIs.

Pure helpers:

- `_parse_zabbix_version(s)` → `(major, minor, patch)`. Returns
  `(0, 0, 0)` on malformed input. Stops at the first non-integer
  component (`"6.x.2"` → `(6, 0, 0)`).
- `_feature_matrix(major, minor)` → list of `(name, available)`
  tuples. Centralised version-gate table; one place to update when
  Zabbix 8.0 lands.

Lands in the `core` tier — version introspection is a foundational
operation every session may want.

### 2. Extend `event.acknowledge` wrappers

`acknowledge_problem` and `bulk_acknowledge` gain two optional
params:

| Param | Type | Default | Action bit |
|-------|------|---------|------------|
| `severity` | int | -1 (no change) | 8 — change severity |
| `unack` | bool | False | 16 — unacknowledge (replaces bit 2) |

The action bitmask logic moves into a pure helper
`_build_ack_action(close, message, severity, unack)`. Returns the
correct integer; callers append the resulting bitmask to the
`event.acknowledge` payload and, when severity is in range, also
pass `severity` as a top-level field per the Zabbix API contract.

Existing callers are not affected: the new params default to
no-ops, and the action bitmask matches the previous inline logic
for any combination of the old params.

## Test approach

16 new pure-helper tests in `test_analytics.py` covering both
shipped helpers:

- `_build_ack_action` (8 cases): default, close-only, message-only,
  severity-only, severity-out-of-range, all-compose, unack-replaces-
  ack, unack-with-other-bits.
- `_parse_zabbix_version` (5 cases): standard `6.4.2`, two-part
  `7.0`, empty, garbage, partial-garbage `6.x.2`.
- `_feature_matrix` (3 cases): Zabbix 6.4 baseline, Zabbix 6.0
  (no rank actions), Zabbix 7.0 (everything unlocked).

All run without a Zabbix server. The async tool wrappers are
covered by the existing registration / smoke test (asserts 158
tools and that `get_zabbix_version` is among them).

## Consequences

- Tool count 157 → 158.
- Test count 405 → 421.
- `WRITE_TOOLS` unchanged.
- No new env vars.
- API-compat: any existing caller of `acknowledge_problem` /
  `bulk_acknowledge` keeps working with no change.

## Not included

- A `get_api_tokens(token.get)` tool. The version helper announces
  whether the token API is available (5.4+), but exposing it as a
  separate tool is a follow-up — value depends on whether the
  operator actually has many tokens to audit.
- A `get_task_queue(task.get)` tool. Same reasoning: useful when
  the Zabbix server is overloaded, premature otherwise.
- Suppress / unsuppress action wiring on the ack wrappers. They
  exist on the API (5.2+) but conflate with the maintenance-window
  workflow; deserves its own design pass.
- Cause / symptom rank actions (6.4+). Recently added by Zabbix;
  the workflow around problem hierarchy is still settling — wait
  for usage signals before wiring it.
- Auto-version-gating of tools (e.g. hide `core.ha.get` calls on
  6.x). Currently we just return "Error" on unsupported APIs; the
  version matrix lets the operator/LLM avoid the call instead. A
  proper auto-gate would need server-startup version probing and
  the resulting `disabled_tools` set — over-engineering for now.
