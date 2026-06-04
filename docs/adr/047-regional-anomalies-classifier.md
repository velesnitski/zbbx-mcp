# ADR 047: `detect_regional_anomalies` on the traffic-drop classifier

**Status:** Accepted
**Date:** 2026-06-04

## Problem

`detect_regional_anomalies` flagged a host as "affected" when
`(traffic.avg − traffic.current) / traffic.avg` exceeded a threshold —
the same instantaneous-spot-reading-vs-average comparison that produced
the diurnal false positives the whole v1.10.x line was built to
eliminate in `detect_traffic_drops` (ADR 040). A normal nightly trough
in a country's hosts made the detector report "N countries affected"
when nothing was wrong. A live run earlier this cycle showed exactly
that — a handful of countries flagged on what hand-checking confirmed
was ordinary diurnal variation.

It was the last detector still using the pre-classifier ratio logic.

## Decision

Judge each host through `anomaly.classify_drop`, the same brain
`detect_traffic_drops` uses, fed inputs at the grain this detector's
data supports.

### Daily grain, not hourly

`detect_regional_anomalies` works from `fetch_trends_batch` `TrendRow`s,
which carry a `daily` (date → average) series but no hourly series. A
same-hour seasonal floor (the mechanism that lets the hourly detector
flag an acute block immediately) is therefore not available here.

Instead, the new helper `recent_baseline_from_daily(daily, recent_days)`
splits the daily series into a recent-days average vs a baseline-days
average. Daily aggregates are inherently diurnal-safe — a full day's
mean cannot show a nightly trough — so comparing recent days to baseline
days removes the false positive without needing a seasonal floor.
`classify_drop` is called with `seasonal_floor_value=None`; its floor,
drop threshold, and host-down rule-out (mapped from service status)
still apply.

When there aren't enough daily points to form both windows, the code
falls back to the old `avg` / `current` inputs so a sparse host still
gets a (lower-confidence) judgment.

### What is unchanged

- Per-country roll-up: a country is flagged when ≥ `country_threshold` %
  of its hosts come back blocked.
- The `min_avg_mbps` micro-market gate.
- The output table and per-country detail.

### Why not country-aggregate hourly

A fuller treatment would fetch hourly trends, sum them per country per
hour, and build a per-country same-hour seasonal floor — giving acute
detection at the regional grain. That is a larger rewrite (new fetch
path, country-level aggregation) and is deferred; the daily-grain
classifier already removes the live false positives, which was the
goal.

## Test approach

6 unit tests in `test_anomaly.py::TestRecentBaselineFromDaily`:

- splits recent vs baseline correctly;
- sorts by date key, not dict insertion order;
- too few days → `(None, None)`;
- empty → `(None, None)`;
- malformed values → `(None, None)`;
- stable daily averages → recent ≈ baseline → no false drop.

`classify_drop` itself is already covered by `test_anomaly.py`. The
per-host wiring in the tool is configuration-level over both tested
helpers; the async wrapper is covered by registration / smoke tests.

## Consequences

- Tool count unchanged (161).
- Test count 536 → 542.
- `WRITE_TOOLS` unchanged. No new env vars.
- **Output change**: `detect_regional_anomalies` no longer flags
  countries whose hosts are merely in a diurnal trough; the affected
  set shrinks to genuine multi-day declines.

## Not included

- **`detect_disruption_wave`** (the other detector named in tasks.md
  #153). It already carries its own diurnal guards — peer-relative drop
  pre-filter and country-cohesion (ADR 020) — so it is not producing the
  spot-reading false positive the same way. A classifier backport there
  is optional and deferred; its hourly-cluster shape differs enough to
  warrant its own pass.
- **Country-aggregate hourly seasonal floor** for regional anomalies
  (described above) — the deeper version; deferred.
- **CPU / connection corroboration** at the regional grain. The
  per-host service status feeds the host-down rule-out, but the
  block-vs-demand corroboration (ADR 042) is not wired here; the daily
  grain already removes the diurnal class.
