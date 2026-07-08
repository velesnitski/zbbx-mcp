# ADR 075: Time-honest uptime + trend-retention honesty

**Status:** Accepted
**Date:** 2026-07-08

## Problem

Building the reports-side premium SLA (reports ADR 0036/0037) and
cross-validating against live data surfaced three related correctness
bugs in the MCP's uptime/trend tools (tasks 168–170):

1. **`get_service_uptime_report` inflates uptime** (task 168, high).
   `entry["total"] += 1` per *observed* trend row made the denominator the
   set of hours the item happened to report. A host that wrote one sample
   then died reads **100%** (1 up ÷ 1 observed). Worse, the tool first
   dropped all-stale items, so a chronically dead host **disappeared from
   the report entirely** — the worst offenders were the invisible ones.
   Live proof: the reports SLA found 3 premium hosts at 0.00% (enabled but
   dead ~14d) that this tool showed as absent/healthy.
2. **No traffic validation** (task 169, med). Once the denominator counts
   every hour, a host with a *deprecated* check item (reads 0/stale) but
   real traffic would read a false 0% — the exact false-down class the
   reports killed in their ADR 0031/0036.
3. **Trend-window tools silently overstate coverage** (task 170, med).
   Zabbix trend housekeeping keeps only ~14d on this fleet, so a "30d"
   window returns 14d of data labelled 30d. Worst case:
   `get_month_over_month(days=30)` compares the live 30d against a prior
   30d that is largely empty — a delta against a void.

## Decision

A shared, pure `uptime.py` (unit-tested, no I/O):

- **`compute_host_uptime(service_rows, now, window_start, host_has_traffic)`**
  — the denominator spans every hour from the host's **first observed
  sample** (clamped to the window) through now. Each hour: an explicit
  sample is trusted (up iff avg ≥ 0.5); a **missing** hour is UP if the
  host moved real traffic (alive, check silent → kills the false-down
  class, task 169) else DOWN (a hard-down host writes no trends, task
  168). No samples at all → `(0, 0)` = no-data, never a false 100%/0%.
- **`coverage_note(min_clock, now, requested)`** — appends "covered ~Nd
  of the requested Md" when observed history is < 95% of the request.
- **`retention_too_short(min_clock, now, requested)`** — true when history
  can't fill both comparison periods.

Wired in:

- `get_service_uptime_report` — stops dropping stale items from host
  eligibility (keeps them; the new math + traffic gate handle them),
  fetches per-host traffic for the gate, buckets trends by hour, and
  appends the coverage note.
- `get_month_over_month` — computes the earliest daily sample; when
  `retention_too_short`, deltas render `n/a` with a loud warning instead
  of a fabricated growth number.
- `get_sla_dashboard` — its header no longer implies a period average; it
  is relabelled "current snapshot" (it computes point-in-time lastvalue +
  traffic, never a period integral).

## Test approach

`tests/test_uptime.py` (14): the dead-host-reads-~0% regression, fully-up,
explicit-down hours, traffic rescuing missing hours, traffic *not*
overriding explicit downs, no-samples → (0,0), pre-window drop, bad-value
skip; coverage-note thresholds; retention-too-short boundaries. Wiring is
config-level over the tested helpers. 671 → 685.

## Consequences

- A days-dead host now reads ~0% and appears in `only_problems` output
  instead of vanishing; a live host with a broken check reads ~100%
  instead of a false 0%.
- `get_month_over_month` refuses to compare against a retention void.
- Tool count unchanged (163).

## Not included

- **`get_sla_dashboard` as a trend integral.** It stays a point-in-time
  snapshot (now labelled as such) — converting it to the hourly integral
  is a larger change with its own weighting semantics; deferred.
- **Per-hour traffic validation** (the reports' finer grain). Per-host
  aggregate traffic is the gate here: it fixes the gross false-down/dead
  cases correctly and cheaply; sub-hour precision is deferred.
- **Maintenance-window exclusion** (task 171) and raising Zabbix trend
  retention (infra, not MCP) — both gated / out of scope, unchanged.
