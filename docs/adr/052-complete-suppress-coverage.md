# ADR 052: Complete maintenance-suppress coverage

**Status:** Accepted
**Date:** 2026-06-04

## Problem

ADR 044 added the maintenance-suppress filter (`data.filter_suppressed`):
a host inside a maintenance window has its problems flagged
`suppressed: "1"` — planned downtime, not incidents — and a problem-
surfacing tool should drop them by default. That filter was wired into
four tools (`get_active_problems`, `get_problems`, `get_host_floods`,
`get_outage_clusters`) but **three other tools that also call
`problem.get` were left out**, so they still treat planned downtime as
live problems.

The most consequential is the diagnosis path. `diagnose_host` /
`bulk_diagnose` / `diagnose_subnet` read problems through
`_collect_diagnosis_inner`, which had no suppress filter — so a box in a
maintenance window reads `degraded` purely from its planned downtime.
That is exactly the false-positive class the surrounding work has been
eliminating everywhere else. The other two, `get_recent_changes` and
`send_slack_report`, surface maintenance noise in a recent-activity feed
and a Slack summary respectively — the two places it is least wanted.

This carry-over was tracked across ADR 044 → 046 → 049 as "not included."

## Decision

Wire the existing, already-tested `filter_suppressed` helper into all
three remaining problem-consuming tools, mirroring the three-step pattern
the other four use:

1. add `"suppressed"` to the `problem.get` `output` list;
2. apply `filter_suppressed(problems, include_suppressed)` right after
   the fetch, before any merge / count / clock-cutoff;
3. add `include_suppressed: bool = False` to the tool signature so full
   visibility is one flag away.

Per tool:

- **`_collect_diagnosis_inner`** (diagnose.py) gains an
  `include_suppressed` keyword and applies the filter before the existing
  clock-cutoff. The flag is threaded out through `_run_bulk_diagnosis`
  and exposed on all three public tools (`diagnose_host`,
  `bulk_diagnose`, `diagnose_subnet`), matching how `group_hostids` was
  threaded in ADR 046.
- **`get_recent_changes`** (availability.py) filters `current_problems`
  after the parallel fetch. The resolved-events `event.get` branch is
  untouched — suppression is a problem-state concept.
- **`send_slack_report`** (slack.py) filters the `problems` result before
  it is summarised into the report.

As with the earlier wirings this is a **no-op today**: the monitored
instance has no maintenance windows, so nothing is currently suppressed.
The value is structural — once a window is configured, suppressed
problems drop out of all seven problem-consuming tools uniformly instead
of four.

## Test approach

`filter_suppressed` itself is covered by
`test_analytics.py::TestFilterSuppressed` (5 cases). The diagnosis
threading is the only non-trivial wiring, so it gets a focused unit:
`TestDiagnoseSuppressThreading` drives `_collect_diagnosis_inner` with a
minimal problem-only async client and asserts a maintenance-suppressed-
only host reads `healthy` by default, reads `degraded` under
`include_suppressed=True`, and that a mixed set keeps only the live
problem. The availability / slack wirings are configuration-level over
the tested helper, consistent with how ADR 050's floods wiring shipped.

## Consequences

- Tool count unchanged (161).
- Test count: +3 (557 → 560).
- `WRITE_TOOLS` unchanged. No new env vars.
- API-compat: a new optional arg on five public tools, no-op default
  while no maintenance windows exist.

## Not included

- **Suppress filtering in non-problem tools.** Only the three tools that
  call `problem.get` to assess current state are in scope. Raw/event
  history paths are intentionally left whole.
- **Surfacing a suppressed count.** None of the seven tools annotate
  "(N suppressed)" in their output. A presentation nicety for later, not
  a correctness gap.
