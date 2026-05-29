# ADR 040: False-positive-resistant, acute-aware traffic-drop classifier

**Status:** Accepted
**Date:** 2026-05-29

## Problem

`detect_traffic_drops` compared an instantaneous `lastvalue` against
the N-day trend average and flagged anything below a threshold. On
diurnal / bursty traffic this is structurally wrong: a spot reading
caught in a normal trough reads as an 80–96% "drop."

A hand investigation of the top "highest-confidence blocked" hosts
found **every one was a false positive**:

- a host reported "96% drop" was oscillating normally and *rising*;
- another's CPU was *climbing* while the tool said it was blocked;
- one box's "drop" was measured on an idle tunnel interface while its
  primary uplink carried steady traffic;
- sub-1-Mbps baselines flipping to zero counted as "90% drops."

The naive ratio also can't tell three different situations apart, all
of which present as "traffic is low":

1. a real **block** (host up, still trying to serve, but bytes don't flow),
2. genuine **low demand** (fewer users — CPU/connections fell too),
3. a normal **diurnal trough** (it's just night).

A further requirement surfaced: the fix must still catch an
**immediate** block (one that started minutes ago) at the same time as
sustained ones — a naive "require K consecutive buckets" persistence
gate would *delay* acute detection.

## Decision

A new pure module `zbbx_mcp.anomaly` holds the classification brain;
`detect_traffic_drops` becomes a thin data-gathering wrapper over it.

### Layered classifier — `classify_drop(...)`

Returns `DropVerdict(state, confidence, drop_pct, reasons)`. States:
`healthy`, `low_demand`, `blocked_acute`, `blocked_sustained`,
`artifact`, `unknown`. The layers, in order:

1. **Denominator rule.** Baseline below `min_baseline` → `artifact`. A
   percentage off a tiny absolute number is meaningless.
2. **Like-window comparison.** Compare a recent-window *average* to the
   baseline *average* — never a spot reading vs a distribution. (This
   alone is why `diagnose_host`, which already averaged, disagreed with
   the old detector and was right.)
3. **Seasonality.** If the recent average is at or above the
   same-hour-of-day floor (`seasonal_floor`), it's a normal diurnal
   trough → `healthy`. Below it → genuinely anomalous *for this hour*.
   This is the mechanism that lets an acute block be flagged on the
   **current** bucket: "below the band now" is itself the anomaly, no
   waiting required.
4. **Host-down rule-out.** Agent unreachable → `unknown` (host-down is
   `diagnose_host`'s verdict, not a traffic block).
5. **Corroboration.** A block leaves the host serving: CPU / connections
   hold up while bytes collapse. If the demand signal fell roughly in
   step with traffic → `low_demand`. If it held up → block confirmed,
   higher confidence.
6. **Acute vs sustained.** Persistence does **not** gate — an anomaly is
   `blocked_acute` immediately and only *escalates* to
   `blocked_sustained` at/above the persistence threshold.

Confidence scales with depth below the seasonal floor, corroboration,
and persistence; capped lower when no seasonal band is available (can't
rule out diurnal, so flag but hedge).

### Supporting pure helpers

- `seasonal_floor(hourly_points, hour_of_day, pct=10)` — buckets a 7-day
  hourly series by hour-of-day and returns the low percentile of the
  matching bucket. None when too few same-hour samples.
- `pick_traffic_interface(interfaces)` — selects the highest-*baseline*
  interface, not the highest-current. An always-idle interface has ~0
  baseline and is never chosen, so its zero reading can't fabricate a
  drop. This is the fix for the "drop measured on a dead tunnel" case.
- `percentile(values, pct)` — nearest-rank, robust for the ~7-sample
  per-hour buckets a 7-day series yields.

### Tool wiring

`detect_traffic_drops` now fetches every traffic interface (not one per
host), computes per-interface baselines from the trend data it already
pulls, picks by baseline, derives recent-avg / seasonal-floor /
persistence from the same trends (no extra trend calls), and adds one
cheap `agent.ping` fetch for the host-down rule-out. New params
`recent_hours` (default 6) and `seasonal` (default True); the floor
default rose 1.0 → 5.0 Mbps.

## Test approach

24 unit tests in `test_anomaly.py` exercise the brain in isolation,
using the exact shapes seen in practice:

- false positives that must be suppressed: diurnal trough within the
  seasonal band → `healthy`; tiny baseline → `artifact`; demand drop
  (connections fell too) → `low_demand`; idle-interface selection
  avoided by `pick_traffic_interface`;
- true positives that must fire: acute block on the current bucket
  (sustained=0) → `blocked_acute`; persistence → `blocked_sustained`;
  no-seasonal-data still flags but caps confidence; agent-down →
  `unknown`; deeper-below-floor raises confidence; CPU corroboration
  with and without connection data.

The async wrapper is covered by the existing registration / smoke
tests; the brain — where the logic lives — is fully unit-tested.

## Consequences

- Tool count unchanged (161).
- Test count 493 → 517.
- One new module (`anomaly.py`); 16 mypy-clean source files.
- `WRITE_TOOLS` unchanged. No new env vars.
- **Output-compat change**: `detect_traffic_drops` columns are now
  `Server | Provider | State | Conf | Recent → Baseline | Drop | Why`,
  and the result set excludes diurnal troughs and low-demand cases that
  the old tool reported as drops. Consumers parsing the old columns must
  update.
- The classifier is reusable: `detect_regional_anomalies`,
  `detect_disruption_wave`, and the per-host `diagnose_host` traffic
  verdict can adopt the same brain in follow-ups.

## Not included

- **Wiring CPU / connection corroboration into the tool.** The
  classifier accepts `cpu_ratio` / `conn_ratio` and is tested with them,
  but `detect_traffic_drops` currently passes only `agent_reachable`
  (host-down rule-out). Computing baseline CPU / connection ratios needs
  extra trend fetches; deferred so this change stays one trend call.
  Until then, `low_demand` separation relies on the seasonal band alone,
  which already removes the diurnal false positives.
- **Backporting the classifier to the other detectors.** Listed above as
  a follow-up; each has a different data-gathering shape.
- **A configurable seasonal percentile / bucket granularity.** The
  10th-percentile, hour-of-day bucket is a reasonable default; exposing
  knobs is premature until a real case needs them.
- **Multi-week seasonality (weekday vs weekend).** The 7-day window
  gives ~7 samples per hour-of-day, enough for a rough floor. A
  weekday/weekend split would need a longer trend window; revisit if
  weekend troughs prove to leak.
