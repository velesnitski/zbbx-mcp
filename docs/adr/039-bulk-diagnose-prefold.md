# ADR 039: Pre-fold input host list in `bulk_diagnose` / `diagnose_subnet`

**Status:** Accepted
**Date:** 2026-05-28

## Problem

`bulk_diagnose` and `diagnose_subnet` resolve a target set of
Zabbix hosts and hand them to `_run_bulk_diagnosis`, which runs
`_collect_diagnosis_inner` once per record. Each call is itself a
small chain (`problem.get` + `trend.get` × 2 + `auditlog.get`).

When the resolved set contains a parent host plus its sub-hosts,
the fan-out runs the chain N times for one physical machine and
emits N near-identical rows. Output dilution **and** wasted
upstream calls. Same shape as ADRs 032 / 033 / 034 / 036 / 037
but on the *input* side (pre-fold) rather than the per-host
aggregator output side.

Diagnostic specifics:

- `bulk_diagnose(hosts="parent01, parent01 v1, parent01 v2")` —
  caller explicitly listed both parent and sub-hosts.
- `bulk_diagnose(country="XX")` — resolution pulls every Zabbix
  record in the country, sub-hosts included.
- `diagnose_subnet(subnet=…)` — every host with an IP in the
  subnet, sub-hosts included.

All three paths funnel through `_run_bulk_diagnosis`, so the
fold lands once.

## Decision

Add a pure helper `_dedupe_records_by_canonical(records)` that
returns `(deduped, sub_counts)`. `_run_bulk_diagnosis` calls
this at the top, before the `item.get` batch and the
`asyncio.gather` fan-out. After the fan-out resolves, each
result's `host` field is annotated `parent (+N sub)` when the
group covered more than one Zabbix record.

### Representative selection

For each canonical group prefer the parent (host name without a
space) as the representative; fall back to the first record when
no parent is present in the resolved set.

### Why "rep" instead of aggregating across the group

`_collect_diagnosis_inner` is a non-trivial async chain
(problem / trend / auditlog calls) keyed on a single hostid.
Rewriting it to aggregate across a multi-hostid group is a
larger refactor — and the diagnostic verdict semantics (DOWN /
DEGRADED / TRAFFIC_LOST / etc.) are themselves *physical-machine*
properties, so picking the parent's hostid as the diagnostic
target is the right answer for the question the operator is
asking: "what's wrong with this box?".

A future enhancement could merge per-sub-host problems into the
parent's diagnostic output (so a sub-host-specific alert isn't
silently dropped). Out of scope here; see "Not included" below.

### Table header

The rendered table header (e.g. `M of N host(s)`) keeps the
**original** (pre-dedup) count for `N`. That preserves the
"fan-out compressed from raw N to diagnosed M" signal for
operators who want to see when the fold actually fired.

## Test approach

5 new pure-helper tests in `test_analytics.py::TestBulkDiagnosePreFold`
cover every fold-relevant path:

- **`test_standalone_hosts_pass_through`** — no fold needed.
- **`test_parent_plus_subhosts_collapse_to_parent`** — parent
  preferred as representative, sub_count populated.
- **`test_subhost_only_set_picks_first_as_rep`** — when the
  parent isn't in the resolved set, the first sub-host wins.
- **`test_mixed_standalone_and_groups`** — partial fold with
  some single hosts and one group.
- **`test_empty_input`** — defensive (empty in → empty out).

The async wrapper (`_run_bulk_diagnosis`) is covered by the
existing registration / smoke tests; the helper above is the
new logic.

## Consequences

- Tool count unchanged (161).
- Test count 488 → 493.
- `WRITE_TOOLS` unchanged.
- No new env vars.
- **Output-compat**: the rendered table now shows fewer rows
  when the input set contained multi-record physical machines.
  Each kept row's `host` field can contain the `(+N sub)`
  annotation.
- **Performance**: per-host upstream call count drops by the
  same factor (a 5-VIP box that used to drive 5 sets of
  problem / trend / auditlog calls now drives one).

## Not included

- **Aggregating per-sub-host problems / traffic into the
  parent's diagnostic output.** Today the diagnosis runs on the
  representative hostid only; sub-host-specific Zabbix
  problems wouldn't appear in the result. The verdict
  (DOWN / DEGRADED / etc.) is computed from the rep's trends
  and items, which is usually correct because the
  per-VIP measurements on the rep already reflect the box's
  state. Worth a follow-up if a real case surfaces where the
  rep-only view misses a sub-host problem.
- **A `verbose=True` mode** that emits both the parent row and
  each sub-host row. The verbose row layer would make the
  output much wider; not justified on the cases we see.
- **`detect_regional_anomalies` (`geo_traffic.py`) per-country
  fold** — different shape (per-host counts feed a country
  aggregate). Queued as v1.9.7.
- **Internal counters in `generate_service_brief`** — still
  per-host (ADRs 034 §"Not included" and 036 §"Not included");
  revisit when a specific counter inflates in production.
