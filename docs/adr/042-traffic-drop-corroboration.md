# ADR 042: CPU / connection corroboration in `detect_traffic_drops`

**Status:** Accepted
**Date:** 2026-06-01

## Problem

ADR 040 built a classifier that separates real blocking from diurnal
troughs and (in principle) from genuine low demand. But the demand-vs-
block discriminator needs a corroborating signal — `cpu_ratio` /
`conn_ratio` — and ADR 040 left the tool passing only
`agent_reachable`, deferring the corroboration fetch.

A fleet-wide run exposed the gap: a coordinated early-morning regional
demand trough (a whole provider/region quieting down together) came
back as ~50 `blocked_sustained` rows. Hand-checking the top hosts
showed traffic falling *with* CPU — the low-demand signature, not a
block — but with only `agent_reachable` (host is up), the classifier
had no way to see it and flagged everything below the seasonal band.
`0` rows were classified `low_demand` precisely because the signal that
produces that verdict was unwired.

## Decision

Add a bounded second corroboration pass to `detect_traffic_drops`.

### Two passes

1. **Classify with agent reachability only** (the existing logic).
   Hosts that come back `blocked_acute` / `blocked_sustained` become
   *candidates*; everything else (`healthy`, `low_demand`, `artifact`,
   skips) is settled here.
2. **Corroborate candidates.** For the candidate hostids *only* — a
   handful that passed the seasonal gate, not the 500+ fleet — fetch
   CPU and connection trends, compute recent/baseline ratios, and
   re-classify. Candidates whose connections/CPU fell with traffic flip
   to `low_demand` and leave the block list.

Fetching corroboration only for candidates is what keeps the cost
bounded — it never scales with fleet size, only with the (small) number
of hosts that already look anomalous.

### Connections strong, CPU weak

The classifier already prefers `conn_ratio` over `cpu_ratio`, and that
ordering matters here. Connection count tracks users directly: if it
fell in step with traffic, demand dropped. CPU is a *weak* fallback —
these hosts carry a fixed OS / monitoring / idle-service overhead that
doesn't scale linearly with traffic, so CPU can stay relatively flat
whether the box is blocked or merely quiet. CPU is used only when no
connection metric is configured (`KEY_CONNECTIONS` unset / absent), and
the lower confidence reflects that.

### New pure helper

`anomaly.metric_recent_baseline_ratio(records, recent_start,
invert_pct=False)` splits a trend series at `recent_start`, averages
each window, and returns recent/baseline. `invert_pct=True` converts an
idle-percentage metric (`system.cpu.util[,idle]`) to its used
complement (`100 - x`) on both windows *before* the ratio, so the result
reflects load — a busy host has a high used-ratio. Returns None when a
window is empty or the (post-inversion) baseline is non-positive.

## Test approach

6 new unit tests in `test_anomaly.py::TestMetricRecentBaselineRatio`,
focused on the one place a silent bug would hide — the idle→used
inversion (getting it backwards would read a busy host as idle and
invert all CPU corroboration):

- plain ratio (no inversion);
- idle inversion, busy host → high used-ratio (correct direction);
- idle inversion, quieting host → low used-ratio;
- empty recent window → None;
- zero baseline → None;
- fully-idle baseline (100% idle → 0% used) → None.

The classifier's consumption of `cpu_ratio` / `conn_ratio` is already
covered by `test_anomaly.py` (low-demand vs block cases from ADR 040).
The two-pass wiring is configuration-level over those tested pure
functions.

## Consequences

- Tool count unchanged (161).
- Test count 517 → 523.
- `WRITE_TOOLS` unchanged. No new env vars.
- `detect_traffic_drops` now separates coordinated low-demand troughs
  from real blocks; the headline count reports how many candidates were
  reclassified as low-demand.
- Extra API cost is two `item.get` + two `trend.get` calls scoped to
  candidate hosts only — negligible and fleet-size-independent.

## Not included

- **Corroborating the *acute* (current-bucket) path with sub-hourly
  data.** Trends are hourly, so an acute block detected mid-bucket is
  corroborated against the partial current hour. Good enough; sub-hourly
  history would need `history.get` (heavier, shorter retention).
- **Backporting corroboration to `detect_regional_anomalies` /
  `detect_disruption_wave`.** Same brain applies; separate follow-ups
  with their own data-gathering shapes.
- **A standalone connection-health tool.** The connection ratio is used
  here only as a block/demand discriminator; surfacing it on its own is
  a different feature.
