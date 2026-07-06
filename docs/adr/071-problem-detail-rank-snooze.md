# ADR 071: `get_problem_detail` surfaces symptom rank and snooze state

**Status:** Accepted
**Date:** 2026-07-03

## Problem

ADR 059 (native snooze) and ADR 060 (cause/symptom ranking) added write
paths whose *state* no read tool surfaced — both ADRs deferred it "once
snooze/rank see real use, else dead columns" (task 162). That condition
is now met: a live feed-vs-Zabbix validation hit a real ranked-symptom
scenario (a green "resolved" line over a host whose remaining problem
was a dependency symptom), and snooze is in operational use. An operator
inspecting a problem still could not see *why* it was suppressed, for
how much longer, or whether it is a symptom that should be triaged via
its cause.

## Decision

Extend `get_problem_detail` (read-only, no signature change):

- **Snooze detail** — `selectSuppressionData` now also requests
  `suppress_until`. A new pure helper `_format_snooze_status(entries,
  now)` renders each entry: a non-zero `maintenanceid` as "maintenance
  window (id N)"; a manual snooze (ADR 059, `maintenanceid` 0) as
  "snoozed until the problem resolves" (`suppress_until == 0`), "snoozed
  for Hh MMm more (until <ts>)", or "snooze lapsed". Rendered as one
  `**Suppression:**` line, only when something is suppressing.
- **Rank** — the tool already fetches `output: "extend"`, so
  `cause_eventid` arrives free (6.4+; absent on older servers → line
  simply not rendered, no compat risk). Non-zero renders
  `**Rank:** symptom of cause event N — triage the cause, not this
  event`; cause events (0) render nothing.

## Test approach

Six pure-helper cases (`TestFormatSnoozeStatus`: empty/None, maintenance
window, until-resolve, remaining-time arithmetic, lapsed, multi-entry
join) and four wire-contract cases (`TestProblemDetailWireContract`, per
the ADR 068/070 lesson): `problem.get` carries the extended
`selectSuppressionData`; symptom rank renders; cause events render no
rank line; snooze renders. 644 → 654 tests.

## Consequences

- The ADR 059/060 write paths are now inspectable where operators look
  (`get_problem_detail`), closing task 162.
- Tool count unchanged (163). No API surface change; one extra field on
  an existing select.

## Not included

- **Rank/snooze columns in problem lists** (`get_problems`,
  `get_active_problems`). List views stay compact; the detail view is
  the inspection point. Revisit only if list-level filtering by rank is
  ever needed.
- **Resolving the cause event's name** (a second `event.get` round-trip
  per detail call). The id + guidance line is sufficient to pivot; the
  operator can call `get_problem_detail` on the cause id directly.
