# ADR 033: Outage-cluster dedupe by canonical host name

**Status:** Accepted
**Date:** 2026-05-26

## Problem

`_cluster_problems` in `tools/correlation.py` walks problem records
and emits a cluster whenever a single grouping key (subnet,
hostgroup, etc.) accumulates ≥ `min_hosts` distinct *hostids* within
a time window. The threshold uses raw Zabbix hostids:

```python
uniq_hosts = {r["hostid"] for r in bucket}
if len(uniq_hosts) >= min_hosts:
    ...
```

A multi-VIP physical machine is modelled in Zabbix as a parent host
plus N sub-hosts (`"<parent> <suffix>"`). Each VIP has its own
hostid. When the box misbehaves, each sub-host can fire its own
problem record. The hostid-based count then mis-classifies one
machine acting up as ≥ 3 distinct hosts and surfaces a phantom
cluster.

Companion bug shape: the cost tools' parent/sub-host double-count
(ADR 032). `get_host_floods` already folded by parent via
`build_parent_map`; outage clusters were the remaining gap.

## Decision

Add a small pure helper `_canonical_host_name(name)` in
`correlation.py`:

```python
def _canonical_host_name(name: str) -> str:
    return name.split(" ", 1)[0] if " " in name else name
```

`_cluster_problems` now uses canonical names in both places that
previously used hostids/hostnames:

1. The `uniq_hosts` set built for the threshold check.
2. The `hosts` output field surfaced to the caller.

The threshold check `len(uniq_hosts) >= min_hosts` therefore
counts **physical machines**, and the rendered cluster size shows
the same physical count instead of inflating it with sub-host
records.

### Why hostname-split, not `build_parent_map`

`build_parent_map` returns child→parent **hostid** map. Using it
here would have required threading the host list through
`_cluster_problems` (it currently takes only problem records).
Sub-host names already encode the canonical signal — first
whitespace-delimited token — so a one-line string split is enough.
`build_parent_map` remains the right primitive for tools that fetch
host records themselves (cost, shutdown, diagnose paths); the
canonicalisation here doesn't need the round-trip.

### Aligned with the existing convention

`get_host_floods` (ADR 015) already deduplicates by parent hostid.
`get_outage_clusters` is the user-visible cousin and now uses the
same semantic. ADR 032 added the same fold for cost tools via a
different primitive (`canonical_host_groups`) — three primitives
serving three call shapes, all aligned on the "one physical
machine = one logical row" rule.

## Test approach

6 new pure-helper tests in `test_analytics.py`:

- 1 parent + 3 sub-hosts with `min_hosts=3` → **no cluster**
  (canonical count = 1).
- 3 distinct hosts with `min_hosts=3` → cluster (unchanged behaviour).
- 2 distinct hosts + parent-with-2-subs with `min_hosts=3` → cluster,
  with the parent counted once in the `hosts` list.
- 4 sub-hosts of one parent (parent record absent) → still folded
  to canonical, **no cluster**.
- `_canonical_host_name("host-a")` → `"host-a"` (standalone).
- `_canonical_host_name("parent01 v1")` → `"parent01"`.

The async tool wrappers (`get_outage_clusters`) call
`_cluster_problems`; the existing registration / smoke tests cover
the wrapper layer.

## Consequences

- Tool count unchanged (161).
- Test count 465 → 471.
- `WRITE_TOOLS` unchanged.
- No new env vars.
- **Output-compat**: callers that relied on the inflated
  `host_count` will see lower numbers when a cluster was driven by
  one multi-VIP box. The cluster either disappears (failed
  threshold) or shrinks to its true physical-machine size. That's
  the intended correction.

## Not included

- **Sub-host count annotation** (e.g. `"parent01 (+3 sub)"` in the
  cluster's `hosts` list). The threshold question is "how many
  physical machines?"; the display matches. Annotating each
  canonical entry with sub-counts is a UX touch worth considering
  alongside the report formatting, but not required for the bug
  fix itself.
- **Re-tuning `min_hosts` defaults.** The threshold's intent
  ("≥ N physical machines") was correct; only the counting was
  wrong. Defaults stay where they are; operators can lower
  `min_hosts` if the post-fold counts feel too restrictive.
- **Touching `_cluster_problems`'s sort order.** Still
  `(-host_count, -max_severity)`. The canonical fold may reduce
  some `host_count` values, but the relative ranking remains
  meaningful.
- **#152 VPN-check dedupe.** Same bug shape, different code path
  (4 tools across `generate_service_brief`, `detect_regional_anomalies`,
  `get_service_health_matrix`, `get_service_uptime_report`). Queued
  separately; needs its own audit because the dedupe-by-canonical
  step also needs a "worst status across sub-hosts" rule that
  differs per call site.
