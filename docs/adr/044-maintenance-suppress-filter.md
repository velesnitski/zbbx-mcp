# ADR 044: Maintenance-suppress filtering across problem-surfacing tools

**Status:** Accepted
**Date:** 2026-06-04

## Problem

Zabbix sets `suppressed: "1"` on a problem whose host is inside an
active maintenance window. That is planned downtime, not an incident.
The problem-surfacing tools (`get_active_problems`, `get_problems`,
`get_host_floods`, `get_outage_clusters`) fetched and counted those
problems like any other.

Today this is latent: the monitored instance has zero maintenance
windows configured, so nothing is suppressed and nothing changes. But
it is a landmine — the first time ops schedules maintenance, every one
of these tools (and every report built on them) flags the planned
downtime as a live outage, including it in cluster counts, flood
thresholds, and active-problem summaries.

## Decision

Exclude suppressed problems by default, with an opt-in to see them.

### Pure helper

`data.filter_suppressed(problems, include_suppressed=False)` drops any
problem with `suppressed == "1"` unless `include_suppressed` is True.
A missing field is treated as visible (not suppressed).

It filters **client-side** rather than via the `problem.get`
`suppressed` request parameter, because that parameter's semantics
shifted across Zabbix versions (return-only-suppressed vs
exclude-suppressed vs all). A client-side drop on the returned field is
version-agnostic and unit-testable. The only requirement is that each
caller add `suppressed` to its `problem.get` `output`.

### Wiring

Each of the four tools gains `include_suppressed: bool = False`,
requests the `suppressed` field, and pipes its `problem.get` result
through `filter_suppressed`:

| Tool | Module |
|------|--------|
| `get_active_problems` | `health.py` |
| `get_problems` | `problems.py` |
| `get_host_floods` | `floods.py` |
| `get_outage_clusters` | `correlation.py` |

`detect_disruption_wave` (named in the original ticket) does **not**
call `problem.get` — it is traffic-trend based, so there is no
suppressed concept to filter. It is excluded from scope.

### Default off — zero behaviour change today

Because nothing is suppressed on the current instance, the default
(`include_suppressed=False`) changes no output today. The value is
realised the day a maintenance window is created: planned downtime
silently drops out of incident views, and a power user can pass
`include_suppressed=True` for full visibility.

## Test approach

5 unit tests in `test_analytics.py::TestFilterSuppressed` pin the pure
helper: default excludes the suppressed row, `include_suppressed=True`
keeps all, a missing field counts as visible, empty input, and the
result is a fresh list (not an alias of the input). The four tool
wrappers are configuration-level over the tested helper.

## Consequences

- Tool count unchanged (161).
- Test count 524 → 529.
- `WRITE_TOOLS` unchanged. No new env vars.
- API-compat: a new optional arg with a no-op default on each tool;
  existing callers unaffected while no maintenance windows exist.

## Not included

- **`diagnose_host` / `bulk_diagnose`.** Their verdict reads
  `problem.get` on the host; a suppressed problem arguably shouldn't
  make a maintenance host read "degraded." Reasonable, but the verdict
  semantics deserve their own consideration (is a maintenance host
  "healthy" or "in maintenance"?) — deferred.
- **`get_recent_changes` / Slack report.** They surface problem activity
  too; lower priority and a different audience. Add the same one-liner
  if maintenance noise shows up there.
- **A fleet-level "what's in maintenance" view.** Surfacing the
  suppressed set on its own (rather than filtering it out) is a
  separate feature.
