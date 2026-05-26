# ADR 034: Canonical-name fold for service-check tools

**Status:** Accepted
**Date:** 2026-05-26

## Problem

Four tools that surface "failing-server" totals from service-check
items each iterated raw Zabbix host lists:

- `generate_service_brief` — the per-check "Blocked Servers" table
  in the brief.
- `detect_regional_anomalies` — the anomaly list rendered after
  per-region statistical analysis.
- `get_service_uptime_report` — the per-host uptime rows.
- `get_service_health_matrix` — the per-country health counts.

Multi-record physical machines (the `"<parent> <suffix>"` naming
pattern) showed up as multiple distinct entries in each tool's
output. The "Servers Failing" / "anomalies detected" / "DOWN
servers" totals were therefore inflated by sub-host count — the
same bug shape ADR 032 fixed for the cost surface and ADR 033 fixed
for the outage-cluster surface.

## Decision

Promote the canonical-name primitive to `data.py` and add a
generic row-fold helper. Each affected tool calls the appropriate
helper at its main count site.

### Two new helpers in `data.py`

`canonical_host_name(name)` — moved from `correlation.py`. Returns
the first whitespace-delimited token of a Zabbix host name (the
parent); pass-through for standalone hosts. This is now the single
primitive used by every per-host fold across the codebase.

`fold_rows_by_canonical_host(rows, name_key, sort_key=None)` — new.
Dedupes a list of row dicts by canonical name extracted from each
row's `name_key` field. When `sort_key` is provided, sorts ascending
first and keeps the first occurrence per canonical name (use this
to make the worst-by-some-metric row win). Each kept row gets a
`sub_count` field set to the number of sub-hosts collapsed into it
(omitted when zero), and the `name_key` value is rewritten to the
canonical name so downstream rendering shows one row per physical
machine.

### Where each tool calls the helper

| Tool | Site | Aggregation |
|------|------|-------------|
| `generate_service_brief` | `blocked_by_check[key]` dedup inside the per-check loop | First occurrence per canonical wins (the failing row stands in for the whole machine) |
| `detect_regional_anomalies` | `all_anomalies` after severity-then-peer-median sort | Sort places CRITICAL first; fold keeps the worst row per canonical name |
| `get_service_uptime_report` | per-host `rows` after uptime-ascending sort | Lowest primary-check uptime sub-host wins |
| `get_service_health_matrix` | per-country count loop refactored to iterate canonical groups | A canonical group is "up" for a metric only when **all** sub-hosts are up (worst-wins). Traffic-validation fallback (any sub-host with real traffic) still applies. |

The "Health Matrix" case is the only one that needed direct count
logic rather than just `fold_rows_by_canonical_host`: it tallies
multiple metrics per country in one pass, and "worst-wins" maps
naturally to `all()` over sub-host statuses.

### Why this is consistent with ADRs 032 and 033

Three primitives, three call shapes, one rule:

| Primitive | Used by | When |
|-----------|---------|------|
| `canonical_host_groups()` (ADR 032) | Cost / shutdown / waste surfaces | Aggregating *metrics* (sum, max) across sub-hosts |
| `_cluster_problems` canonical fold (ADR 033) | `get_outage_clusters` | Counting *distinct physical machines* in an outage cluster |
| `fold_rows_by_canonical_host()` (this ADR) | Service-check surfaces | Folding a *row list* to one row per physical machine |

All three reduce to "one physical machine = one logical row" in
their respective contexts.

## Test approach

5 new pure-helper tests in `test_analytics.py`:

- No sub-hosts → pass-through, no `sub_count` field added.
- Sub-hosts collapse → first occurrence kept, `sub_count`
  populated, name rewritten to canonical.
- `sort_key` makes the worst row win (lowest uptime first).
- Mixed standalone + sub-hosts → only the parent-group row
  carries `sub_count`.
- Alternate `name_key` (e.g. `"server_name"` instead of
  `"host"`) works.

The async tool wrappers (`generate_service_brief`,
`detect_regional_anomalies`, `get_service_uptime_report`,
`get_service_health_matrix`) call these helpers at their main
count sites; the existing registration / smoke tests cover the
wrapper layer.

## Consequences

- Tool count unchanged (161).
- Test count 471 → 476.
- `WRITE_TOOLS` unchanged.
- No new env vars.
- **Output-compat**: per-check "Servers Failing" counts in the
  service brief, the anomaly-row count in
  `detect_regional_anomalies`, the row count in
  `get_service_uptime_report`, and the per-country totals in
  `get_service_health_matrix` will all *decrease* when the input
  fleet contains multi-record physical machines. This is the
  intended correction.

## Not included

- **Touching every internal counter** inside the four tools.
  `generate_service_brief` in particular has several country-level
  counters (`country_data["ok"] / "partial" / "down"`) that still
  iterate raw hosts. Those drive non-marketing aggregates and
  fold differently per metric (worst-wins for status, sum-wins
  for traffic). Out of scope here; revisit if a specific
  inflated counter surfaces.
- **A unified status-priority lookup**. The `worst wins` rule
  varies by tool (`all()` in the matrix, sort-ascending for
  uptime%, severity ordering for anomalies). Codifying a generic
  "status rank" table would couple unrelated semantics; keeping
  the fold local to each call site is clearer.
- **Annotating `sub_count` in rendered output**. The helper sets
  the field; the tool-side rendering can opt in to show
  `"parent (+N sub)"` style annotations later. For this PR the
  count correction is the primary fix.
- **Deferred per task hierarchy**: `get_high_cpu_servers`,
  `get_underloaded_servers`, `get_low_disk_servers`,
  `get_low_memory_servers`, `get_stale_servers`,
  `get_shutdown_candidates`, `bulk_diagnose`, `diagnose_subnet`,
  and the remaining `detect_traffic_*` siblings still need their
  own fold passes. Each gets a separate audit because the
  metric-aggregation rules vary.
