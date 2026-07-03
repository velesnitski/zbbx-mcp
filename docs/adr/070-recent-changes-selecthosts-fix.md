# ADR 070: `get_recent_changes` — same `problem.get selectHosts` bug as ADR 068

**Status:** Accepted
**Date:** 2026-07-03

## Problem

During a live feed-vs-Zabbix cross-validation, `get_recent_changes`
errored on its first call:

```
Zabbix API error (-32602): Invalid parameter "/": unexpected parameter "selectHosts".
```

The same defect ADR 068 fixed in `triage_slack_alert`: the tool's
`problem.get` carried `selectHosts: ["host"]`, which `problem.get` does
not support (unlike `event.get` / `trigger.get`). The render loop's
`p.get("hosts", …)` fallback was dead code even before Zabbix 7.x started
rejecting the parameter — `problem.get` never returns hosts.

ADR 068 explicitly flagged this sweep ("worth applying to other
orchestration tools"); this instance proves it. A full-repo sweep of
every `selectHosts` call site (30+) was performed this time: all others
ride `event.get`, `trigger.get`, `graph.get`, `item.get`,
`hostgroup.get`, `maintenance.get`, `template.get`, `proxy.get`,
`httptest.get`, or `usermacro.get` — all of which support it.
**`get_recent_changes` was the only remaining `problem.get` carrier.**

## Decision

Same shape as ADR 068:

1. Drop `selectHosts` from the `problem.get`; add `objectid` (the
   trigger id) to its output.
2. After the suppress filter, fetch `trigger.get(triggerids,
   selectHosts=["host"])` once and build a `triggerid → host name` map.
3. Render the New Problems table's Host column through that map.

The resolved-events branch is untouched — `event.get` supports
`selectHosts` natively. One extra `trigger.get` round-trip, scoped to the
firing triggers only.

## Test approach

`TestRecentChangesWireContract` (per ADR 068's lesson, wire-level, not
pure-core): a recording fake client + capture-MCP drives the real tool
function and asserts `problem.get` goes out **without** `selectHosts`
(and **with** `objectid`), `event.get` keeps its `selectHosts`, and the
trigger-mapped host name reaches the rendered table. 641 → 644 tests.

## Consequences

- `get_recent_changes` works against Zabbix 7.x again (it was 100%
  broken — every call errored).
- Sweep documented: no other `problem.get` + `selectHosts` sites remain.
- Tool count unchanged (163).

## Not included

- **A lint/CI guard** rejecting `selectHosts` next to `problem.get`
  (e.g. a tiny AST or grep test). With the sweep clean and two ADRs
  documenting the class, a guard test is cheap insurance — deferred as a
  follow-up candidate rather than folded in here.
