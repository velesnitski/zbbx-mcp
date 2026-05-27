# ADR 036: Parent / sub-host fold for inventory + traffic tools

**Status:** Accepted
**Date:** 2026-05-27

## Problem

Seven more per-host aggregators iterated raw Zabbix host lists and
emitted one row per record. Multi-record physical machines (the
`"<parent> <suffix>"` naming pattern) inflated counts and diluted
top-N tables — the same shape ADRs 032, 033, and 034 fixed for
cost, outage-cluster, and service-check tools respectively.

Affected:

| Tool | Module | Output | Worst-wins sort |
|------|--------|--------|-----------------|
| `get_high_cpu_servers` | `inventory_load.py` | Servers above a CPU threshold | Highest CPU% |
| `get_underloaded_servers` | `inventory_load.py` | Servers below a CPU threshold | Lowest CPU% |
| `get_low_disk_servers` | `inventory_load.py` | Servers above a disk-usage threshold | Highest disk-usage % |
| `get_low_memory_servers` | `inventory_load.py` | Servers below a free-memory threshold | Lowest free-memory GB |
| `get_stale_servers` | `health.py` | Servers without recent agent data | Oldest last-update |
| `detect_traffic_drops` | `traffic.py` | Servers with biggest traffic drop vs baseline | Biggest drop % |
| `get_traffic_report` | `traffic.py` | Traffic ranking with connections + BW/client | SUM (different semantic — see below) |

## Decision

Apply the canonical fold each tool needs, reusing the primitives
already in `data.py` (ADRs 032 / 034) or — for the simpler tuple-
based row lists — an inline `seen_canonical` loop.

### Worst-wins pattern (six tools)

After the existing sort step places the worst row first, dedup by
`canonical_host_name(...)` — the first occurrence per canonical
name is the worst sub-host:

```python
result.sort(key=lambda r: -r[worst_key])
seen_canonical: set[str] = set()
folded = []
for entry in result:
    cn = canonical_host_name(entry_hostname(entry))
    if cn in seen_canonical:
        continue
    seen_canonical.add(cn)
    folded.append(entry)
result = folded
```

`detect_traffic_drops` uses `fold_rows_by_canonical_host(drops,
name_key="host")` directly because its rows are already dicts.

### Disk and memory tools needed an extra fetch

`get_low_disk_servers` and `get_low_memory_servers` previously
fetched hostnames only for the *top* `max_results` records — but
the fold has to dedupe **before** truncating, otherwise sub-hosts
that don't make the top-N would be lost. Fix: fetch hostnames for
all flagged hosts (typically tens, occasionally a couple of
hundred) upfront, run the fold, then truncate.

### SUM semantic for `get_traffic_report`

Traffic-report rows answer "how much traffic / how many connections
does this server carry?". For multi-record physical machines:

- Traffic across the sub-host interfaces sums to the box's total
  throughput. Each Zabbix sub-host records its own NIC's traffic;
  summing recovers per-box throughput.
- Connection counts likewise sum: each VIP has its own session
  counter.
- `bw_per_client` is recomputed from the summed totals.

The other tools use worst-wins; this one uses SUM. Both are
captured by the "one physical machine = one logical row" rule
ADR 032 framed.

### Why not `canonical_host_groups` for traffic_report

`canonical_host_groups` aggregates traffic, cost, and CPU. The
traffic-report fold also needs connections SUMmed (not in the
helper) and `bw_per_client` recomputed. A custom inline fold is
clearer here than threading two extra metric kwargs through the
generic primitive.

## Test approach

3 new pattern-sanity tests in `test_analytics.py::TestInlineCanonicalFolds`
exercise the three inline-fold shapes used across the seven tools:

1. **Tuple worst-wins dedup** — the pattern used by
   `get_high_cpu_servers` / `get_underloaded_servers` /
   `get_stale_servers`. After sort, the first occurrence per
   canonical is kept.
2. **Hostid → host_map dedup** — the pattern used by
   `get_low_disk_servers` / `get_low_memory_servers`, where the
   hostname has to be looked up via `host_map` before
   canonicalising.
3. **Traffic-report SUM fold** — verifies that traffic +
   connections both sum across sub-hosts and that
   `bw_per_client` is recomputed from the summed totals (not
   averaged from the originals).

The existing `fold_rows_by_canonical_host` regression tests cover
`detect_traffic_drops`. The async wrappers are covered by the
existing registration / smoke tests.

## Consequences

- Tool count unchanged (161).
- Test count 479 → 482.
- `WRITE_TOOLS` unchanged.
- No new env vars.
- **Output-compat**: the seven tools' counts and per-row lists
  *decrease* when the input fleet contains multi-record physical
  machines. This is the intended correction.
- `get_traffic_report` now shows different absolute numbers per
  row (traffic and connections summed across sub-hosts), and the
  total row count drops. Internal consumers that compared
  numbers across these reports over time will see a step
  change.

## Not included

- **`get_shutdown_candidates`** — two pipelines (candidates +
  peer cohorts) and three metrics (CPU / traffic / service)
  make this a separate refactor. Queued for v1.9.4 with its own
  ADR.
- **`bulk_diagnose` / `diagnose_subnet`** — these need
  *pre-fold* of the input host list, not output-fold. Different
  shape; separate PR.
- **`detect_regional_anomalies` (`geo_traffic.py`)** — referenced
  in ADR 034 but the v1.9.1 edit landed in
  `detect_traffic_anomalies` (`traffic.py`) instead. The regional
  variant aggregates *per country*, not per host, so the
  canonical fold takes a different shape (per-host counts feed
  the country aggregate). Queued separately.
- **Internal counters in `generate_service_brief`** — same
  rationale as ADR 034 §"Not included": the per-country
  `country_data["ok"] / "partial" / "down"` counters still
  iterate raw hosts. Out of scope; revisit when a specific
  inflated counter surfaces.
- **A unified "worst-wins" helper that takes a sort key.**
  `fold_rows_by_canonical_host` already provides this for dict
  rows; the six tuple-shaped sites here are clearer with inline
  dedup loops than with a forced conversion to dicts.
