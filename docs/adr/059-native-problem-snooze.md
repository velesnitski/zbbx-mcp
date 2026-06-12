# ADR 059: Native problem snooze (suppress / unsuppress write path)

**Status:** Accepted
**Date:** 2026-06-12

## Problem

ADR 044→052 made every problem-consuming tool in this server *read*
Zabbix suppression correctly: suppressed problems drop out of digests,
diagnoses, floods, clusters, and Slack reports by default. But nothing
could ever *create* a suppression short of configuring a maintenance
window — and the instance has none. The read path was complete; the
write path didn't exist. Meanwhile the live problem list carries
known-chronic noise that drowns real signal in every view.

Zabbix (5.2+; confirmed on the connected 7.4) supports exactly this via
`event.acknowledge` action bits **32 (suppress, with `suppress_until`)**
and **64 (unsuppress)** — per-problem snooze with no maintenance window.

## Decision

Extend the existing acknowledge tools rather than add a new one — a
snooze *is* an acknowledgement workflow, and the tool count stays flat:

- `_build_ack_action` gains `suppress` / `unsuppress` flags → bits 32/64.
- New pure helper `_suppress_until_from_hours(hours, now)` translates the
  tool-level knob: `0` → no suppression, positive → `now + hours`,
  `-1` → `0`, which Zabbix defines as "suppressed until the problem
  resolves" (indefinite).
- `acknowledge_problem` and `bulk_acknowledge` gain
  `suppress_hours: float = 0` and `unsuppress: bool = False` (mutually
  exclusive, validated before any API call). The payload carries
  `suppress_until` only when snoozing.

Snoozing implies acknowledging (the base bit stays) — "I know about this,
hide it for N hours" is one operator intent. `bulk_acknowledge` makes the
chronic-noise case one call: list the event ids, `suppress_hours=24`.

Because the suppression is recorded in Zabbix itself, every consumer
honours it — the Zabbix UI badge, escalations configured to pause on
suppression, and all seven suppress-aware tools here (ADR 052) — not just
this server's rendering.

## Test approach

Three new bitmask cases in the existing `TestBuildAckAction` (34, 66, 38
combos) and four for `_suppress_until_from_hours` (none / epoch /
fractional hours / indefinite-zero). The payload assembly is the same
config-level pattern the existing ack paths use.

## Consequences

- Tool count unchanged (161). Tests +7 (578 → 585).
- The ADR 052 filter work becomes operationally *active* the first time a
  problem is snoozed: suppressed noise vanishes from all default views
  and returns automatically when the timer lapses.
- `include_suppressed=True` (ADR 044/052) remains the audit lens for
  seeing everything snoozed.

## Not included

- **Showing remaining snooze time** in problem listings
  (`suppression_data` carries `suppress_until`). Presentation nicety;
  add to `get_problem_detail` if the operator asks for it.
- **A dedicated `snooze_problem` tool.** The intent is fully expressible
  on the ack tools; a new tool would duplicate their entire surface.
