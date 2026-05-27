# ADR 037: Parent / sub-host fold in `get_shutdown_candidates`

**Status:** Accepted
**Date:** 2026-05-27

## Problem

`get_shutdown_candidates` is the most consequential tool in the
deferred-fold queue: it tells operators which servers to shut down.
Per-record iteration on the parent / sub-host naming pattern (ADRs
032 / 033 / 034 / 036) had a worse effect here than elsewhere
because the tool's output drives real-money decisions:

1. **Candidate inflation.** Each sub-host of an idle multi-record
   physical machine was classified independently and surfaced as
   its own DEAD / IDLE candidate. One physical box appeared as N
   rows.
2. **False-positive DEAD candidates.** A parent host's own CPU /
   traffic items might show zero (the per-VIP measurements live on
   the sub-hosts). The parent alone looked DEAD even when a
   sub-host was carrying 100+ Mbps. Pre-fold the box was flagged;
   post-fold the aggregate metrics show the real load.
3. **Cohort-capacity inflation.** Peer-headroom math counted each
   sub-host as a separate peer (5x peer count), inflating apparent
   absorption capacity. An "SAFE to shut down" verdict could be
   based on phantom capacity.

ADRs 032 / 033 / 034 / 036 had folded the other per-host
aggregators; this tool was the largest remaining gap because of
its two-pipeline structure (candidate detection + cohort headroom
both iterate raw hosts).

## Decision

Pre-fold `filtered` into canonical groups via
`canonical_host_name`, then operate both pipelines on groups.

### Metric aggregation per group

Mirrors `canonical_host_groups` (ADR 032) but spelt out inline
because the tool has its own dataclass-y `host_metrics` shape:

| Metric | Rule | Why |
|--------|------|-----|
| `cpu_avg` | MAX | Worst-case CPU is the box's actual load. |
| `traffic_avg` | SUM | Each sub-host has its own NIC; per-VIP traffic adds. |
| `service` | WORST (DOWN > PARTIAL > OK) | A single failing sub-host marks the whole box as down for service health. |

### Representative host selection

For display + IP / provider / dashboard lookups the group needs a
representative. Prefer the **parent** host (the one whose name has
no space), fall back to the first sub-host when the parent isn't
in `filtered`:

```python
def _pick_rep(hs):
    for h in hs:
        if " " not in h.get("host", ""):
            return h
    return hs[0]
```

### Cohort pipeline

The second pipeline (peer-capacity headroom) is also rewritten to
iterate canonical groups. Cohort exclusion now operates on the
group level: if any sub-host of a group is a candidate, the whole
group is excluded from peer cohorts. Cohort traffic peak + avg are
also SUMmed across each group's sub-hosts so the resulting headroom
number is at the physical-machine grain.

### Display

Candidate rows annotate `parent (+N sub)` when N > 0. Standalone
hosts pass through unchanged.

## Test approach

6 new tests in `test_analytics.py::TestShutdownCandidateMetricFold`
exercise the per-canonical aggregation logic in isolation. They
mirror the inline `_aggregate_group` shape used in the tool:

- **`test_cpu_max_traffic_sum_service_worst`** — mixed sub-host
  metrics → MAX / SUM / WORST applied correctly.
- **`test_all_idle_group_qualifies_as_dead`** — bug-fix case:
  parent + 4 sub-hosts all idle → one DEAD candidate (not five),
  and the SUM still falls below the DEAD threshold.
- **`test_busy_subhost_rescues_parent_from_dead`** — bug-fix
  case: parent's own metrics zero but a sub-host very busy → the
  aggregate disqualifies the group from DEAD / ZOMBIE.
- **`test_empty_metrics_returns_none`** — no data → no false
  classification.
- **`test_partial_service_loses_to_down`** /
  **`test_partial_service_wins_over_ok`** — worst-wins service
  precedence (DOWN > PARTIAL > OK).

The async tool wrapper is covered by the existing registration /
smoke tests.

## Consequences

- Tool count unchanged (161).
- Test count 482 → 488.
- `WRITE_TOOLS` unchanged.
- No new env vars.
- **Output-compat**:
  - Candidate counts drop when input contains multi-record
    physical machines.
  - Some candidates change category (e.g. a parent that was DEAD
    pre-fold may now be IDLE post-fold because aggregate traffic
    is higher than the parent's own).
  - Peer counts in the safety column drop; headroom numbers stay
    physically meaningful.
  - `host` field in candidate rows can now contain the
    `(+N sub)` annotation.

## Not included

- **`bulk_diagnose` / `diagnose_subnet`** — pre-fold of the input
  host list, not output-fold. Different shape; queued separately
  (v1.9.5).
- **`detect_regional_anomalies`** — per-country aggregation;
  the canonical fold takes a different shape (per-host counts
  feed the country aggregate). Queued separately (v1.9.6).
- **Promoting the inline aggregation into a shared helper.**
  Considered but rejected: `canonical_host_groups` only knows
  about (traffic, cost, cpu). Adding "service worst-wins" would
  thread an extra map through the generic primitive; the inline
  shape is clearer at this call site. If a fourth per-host
  aggregator with the same "worst service" semantic appears,
  revisit.
- **Annotating sub-counts in non-DEAD output sections.** The
  DEAD section uses badges only for SOLO / RISKY; the
  `(+N sub)` annotation now appears in the candidate's `host`
  field, which is rendered uniformly across DEAD / ZOMBIE /
  BROKEN / IDLE sections.
