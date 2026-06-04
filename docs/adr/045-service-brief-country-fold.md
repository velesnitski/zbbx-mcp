# ADR 045: Canonical fold for `generate_service_brief` per-country counters

**Status:** Accepted
**Date:** 2026-06-04

## Problem

`generate_service_brief` builds a per-country service-quality table with
`total` / `ok` / `partial` / `down` / `validated` counts. The loop
iterated raw Zabbix hosts, so a multi-VIP physical machine (the
`"<parent> <suffix>"` naming pattern) counted once per VIP. The
marketing-facing numbers were inflated — a box with three VIPs added
three to `total` and up to three to `ok`/`partial`/`down`.

ADR 034 and 036 folded the main count sites in the service-check and
inventory tools, but explicitly left these internal `country_data`
counters as a "revisit when a counter visibly inflates" follow-up.
This is that revisit.

## Decision

Fold sub-hosts to canonical groups before tallying. One physical
machine contributes one count.

### Aggregation per group

- **traffic = SUM** across the box's VIPs (each VIP has its own
  interface; the box's real throughput is the sum). This also matters
  for the traffic-validation path — two VIPs at 3 Mbps each sum to 6,
  clearing a 5 Mbps floor that neither would clear alone.
- **service status = WORST-wins.** All check values across the box's
  VIPs are merged into one list; the box is `ok` only if every merged
  check is up, `down` if none are, `partial` otherwise. A single
  failing VIP check pulls the box below `ok`.

### Representative selection

The country is taken from the group's representative — the parent
(space-free name) when present, else the first sub-host — consistent
with the other canonical folds (ADR 037/039).

### Pure helper

`_classify_country_group(group_mbps, merged_checks)` returns
`validated` / `ok` / `partial` / `down` / `skip`. Extracted so the
worst-wins merge is unit-tested in isolation (the tool itself produces
an HTML report that is awkward to assert on).

## Test approach

6 unit tests in `test_analytics.py::TestClassifyCountryGroup`:

- real summed traffic ≥ floor → `validated` (regardless of checks);
- no traffic and no checks → `skip` (not counted);
- all merged checks up → `ok`;
- one failing VIP check → `partial` (worst-wins);
- all checks down → `down`;
- two sub-host traffics summing over the floor → `validated` (the fold's
  whole point — per-VIP each would be below it).

The async tool wraps the helper; the existing registration / smoke
tests cover the wrapper.

## Consequences

- Tool count unchanged (161).
- Test count 529 → 535.
- `WRITE_TOOLS` unchanged. No new env vars.
- **Output change**: per-country counts in the service brief drop when a
  country contains multi-VIP boxes; the totals now reflect physical
  machines. Marketing/product consumers see corrected (lower, accurate)
  figures.

## Not included

- **The blocking-risk / micro-market sections** lower in the same tool.
  They iterate `cc_hosts` separately; if a count there visibly inflates,
  apply the same fold. Out of scope for this pass — the headline
  per-country tally was the inflated one.
- **A shared per-country fold helper** across the brief and the other
  geo tools. The aggregation rules (sum traffic, worst service) match
  ADR 034's intent but the data shapes differ enough that a local
  helper is clearer than a forced abstraction.
