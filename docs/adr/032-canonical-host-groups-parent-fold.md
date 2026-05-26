# ADR 032: `canonical_host_groups` — parent / sub-host fold for per-host aggregators

**Status:** Accepted
**Date:** 2026-05-26

## Problem

The MCP's cost and shutdown tools iterate `host.get` results and
emit one row per Zabbix host. Multi-VIP physical machines are
modelled in Zabbix as a *parent* host (e.g. `edge01`) plus N
*sub-hosts* (e.g. `edge01 v1` … `edge01 v5`) sharing the same
underlying box. Per-host iteration treats every sub-host as a
separate billable / shut-downable unit, which double-counts.

The bug surfaced in a CEO-report Waste Detection section that
flagged the same physical machine as five "$280 idle servers".
User feedback: *"this is one server."*

`build_parent_map` already exists in `data.py` (ADR 022) and gets
used by outage-cluster cohesion, flood detection, and a few
inventory tools — but for **inheritance** (sub-host inherits parent
metrics so both appear in the output). Per-physical-machine
*aggregation* is a different semantic; no shared helper existed.

## Decision

Add one canonical-fold helper to `data.py`. Apply to the three cost
tools where the bug surfaced. Defer the rest of the per-host
aggregators to a follow-up.

### `canonical_host_groups(hosts, *, traffic_map=None, cost_map=None, cpu_map=None)`

Pure function. Returns one dict per canonical group (one per
physical machine):

```python
{
    "rep_host": parent host dict (or self if standalone),
    "sub_count": N,
    "sub_hosts": [child host dicts],
    "all_hostids": [parent + sub-hosts],
    "traffic": SUM across the group,
    "cost":    MAX across the group, or None,
    "cpu":     MAX across the group, or None,
}
```

Aggregation rules (from tasks.md #150 2026-05-26):

| Metric | Rule | Why |
|--------|------|-----|
| `traffic` | SUM | Each VIP has its own interface; per-VIP traffic adds. |
| `cost` | MAX | Sub-host `{$COST_MONTH}` macros typically duplicate the parent's bill; summing inflates spend. |
| `cpu` | MAX | Worst-case across VIPs is the box's actual load. |

Metric kwargs are independent — callers pass only the maps they
need. Malformed values (strings, `None`) are coerced/ignored
defensively so a single bad row doesn't break the fold.

### Tools refactored in this PR

| Tool | Symptom before fold | Behaviour after |
|------|---------------------|-----------------|
| `get_cost_efficiency` | Waste list multiplied per-VIP (e.g. five rows for one box). By-country / by-provider `$/Gbps` totals inflated. | Iterates groups; waste rows annotate sub-host count (`parent (+N sub)`); aggregates use group-level cost/traffic. |
| `get_cost_summary` | Server counts in by-product and by-provider tables were inflated. | Counts reflect physical machines; grand-total dollars unchanged (since `cost=MAX` matches the parent's actual bill). |
| `get_cost_gaps` | "M without cost" double-counted sub-hosts when no macro was set. | Counts physical machines without macros; one row per missing parent. |

The renderer `_render_cost_summary` (ADR 030) is unchanged — it
already takes pre-aggregated dicts, so the fold is transparent at
the render layer.

### Tools deferred to v1.9.0

Listed in CHANGELOG; each needs its own audit:

- `get_shutdown_candidates` — two pipelines (candidates + cohorts)
  and three metrics make this a multi-step refactor.
- `bulk_diagnose` / `diagnose_subnet` — fan-out doubles up on
  multi-VIP boxes; needs care so a child's "different IP"
  isn't lost.
- `detect_traffic_drops` / `detect_traffic_anomalies` /
  `get_traffic_report` — drop semantics need to be re-considered
  (the parent's drop is the box's drop; a sub-host alone dropping
  is different).
- `get_high_cpu_servers` / `get_underloaded_servers` / similar
  inventory_load tools — currently use the *inheritance* pattern
  (`_resolve` / parent metric falls through), so they don't crash,
  but per-VIP rows still dilute the output.

## Test approach

9 new pure-helper tests in `test_analytics.py` cover
`canonical_host_groups`:

- Standalone host → one group, no sub-count.
- Parent + 5 sub-hosts → one group, sub_count=5, all_hostids populated.
- **The bug case**: 5 sub-hosts each with a cost macro → group
  cost == the single per-host figure (not 5×).
- Traffic SUM across multi-VIP group.
- CPU MAX across multi-VIP group.
- cost=None when no host in group has a macro.
- Mixed standalone + parent/sub-host inputs.
- Orphan sub-host (parent missing from input) stands alone.
- Malformed metric values (strings, None) are ignored gracefully.

The three refactored cost tools rely on `_render_cost_summary`'s
existing tests (`TestCostSummaryRedactPartial`) for output-shape
correctness; the fold is upstream of that and is itself unit-tested.

## Consequences

- Tool count unchanged (161).
- Test count 456 → 465.
- `WRITE_TOOLS` unchanged.
- No new env vars.
- API-compat: tool signatures unchanged.
- Output-compat: `get_cost_summary` server counts will *drop*
  (each sub-host group folds into one); the dollar grand total is
  unchanged. `get_cost_efficiency` waste-row counts will *drop*;
  waste-list entries now look like `parent (+N sub)` when sub-hosts
  are present. Internal consumers of the markdown should be aware.

## Not included

- **Refactoring every per-host aggregator.** Listed above as
  deferred. Each needs its own audit and tests; bundling 8 tool
  refactors into one PR is too much surface for one review.
- **Promoting the fold into a base class / mixin.** Premature.
  The four callers (three in this PR, plus the existing
  `_resolve`-style inheritance users) have different semantics
  enough that a pure function is the right abstraction.
- **Configurable aggregation rules.** The MAX/SUM/MAX choices
  match how Zabbix data is wired today; a generic "pick your
  reducer" API would be over-engineering. If a future tool needs
  different rules, it can wrap the helper or fold inline.
- **`build_parent_map` deprecation.** It still serves the
  inheritance use case (each Zabbix host stays in the output;
  parent's metric flows down). The two helpers coexist by design.
