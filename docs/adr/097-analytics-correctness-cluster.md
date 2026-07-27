# ADR 097: analytics correctness cluster — wrong numbers, not crashes

**Status:** Accepted
**Date:** 2026-07-27

## Problem

Eleven defects in the calculation layer, all of the same shape: they
produced a plausible number rather than an error, so nothing surfaced them.

1. **Disk prediction was sign-inverted.** `get_predictive_alerts` converts a
   `pused` item's *lastvalue* to free-% but left its **trend series** as
   used-%, so slope and current value ran in opposite directions. A filling
   disk showed a positive slope and was dropped by the "not declining"
   guard (no alert at all), while a disk being freed projected a fake
   exhaustion date. On a `pused` fleet the predictor was all false
   negatives.
2. **`r["service1"] or 100`** treated a genuine `0.0%` as "no data": a
   fully-dead host sorted as perfectly healthy, so it was cut by
   `max_results` — and the worst-wins canonical fold, which explicitly
   relies on that ascending order, kept the *healthy* sub-host instead. The
   same expression in the infra report sent a fully-idle box (the top
   decommission candidate) to the bottom of the underloaded sheet.
3. **Health matrix could exceed 100%.** `up` admitted traffic-validated
   groups but `checked` counted only groups with a check item, so a country
   whose one measured host was DOWN could render "OK (2/1)".
4. **CPU min/max sat on different grains.** `min_val` came from
   `max(hourly means)` instead of `max(hourly maxima)`, pulling the floor up
   toward the average — enough to fire "never below N%" chronic-overload
   verdicts on hosts that did idle deeply.
5. **`history.get` hardcoded `history: 0`.** That reads the float table; a
   connection counter is normally an unsigned integer in `history_uint`, so
   the blast-radius tool got zero rows and scored every peer "n/a".
6. **Skip accounting mislabelled unjudged hosts.** In `detect_traffic_drops`
   both `ARTIFACT` (baseline below the floor — never judged) and `UNKNOWN`
   (agent down) fell into the `else` branch and were counted as **healthy**,
   so the summary could report a fleet as fully healthy when much of it was
   never assessed.
7. **`get_peak_analysis` picked the first interface, not the busiest.** The
   comment said "highest value" but the code used `setdefault` and never
   even fetched `lastvalue` — on a multi-NIC host the analysis often ran on
   an idle interface.
8. **The seasonal floor was the minimum.** A nearest-rank 10th percentile of
   an exact-hour bucket (~7 points over 7 days) is `s[0]`. One freak-low
   hour became the floor for that hour-of-day permanently, and later real
   drops cleared it and read "diurnal trough".
9. **Country summary described the wrong population** — it was computed
   after the `only_problems` filter, so it reported "1 server" for a country
   with many healthy ones, and the column said "Avg" while computing a
   median.
10. **Trend direction was garbage below 4 points** (`len//4 == 0` made
    "recent" the whole list and "older" empty), labelling short CPU series
    "dropping".
11. **The in-progress hour always scored DOWN.** Zabbix has not flushed the
    current hour's trend row yet, so "no sample" there means unmeasured —
    yet every host lost that hour on every window.

## Decision

Fix each at its source: flip the converted item's trend series to match the
value it is regressed against; replace `or 100` with explicit `is None`
checks; let `checked` admit the same evidence that can raise `up`; derive
the CPU floor from hourly maxima; pass each item's `value_type` to
`history.get`; give `ARTIFACT` and agent-down their own skip buckets and
surface them in the summary; select the busiest interface by `lastvalue`;
widen the seasonal bucket to the target hour ±1 so the percentile has a real
sample; snapshot rows before the problem filter and relabel the column
"Median"; require 4+ points for a trend direction; and treat the current
hour as unmeasured rather than down.

`executive.py` crossed its size budget, so `get_predictive_alerts` moved to
a new `tools/predictive.py` — forecasting is a distinct domain from KPI
reporting, and the budget exists to force exactly this split.

## Test approach

Existing suites cover the shared helpers; the uptime expectations that
encoded the old "current hour is down" behaviour are updated with the
reason recorded inline. Tool count is unchanged (165) — the predictive tool
moved modules, it did not disappear.

## Consequences

- Disk exhaustion is predicted in the right direction; dead hosts rank as
  dead; uptime is not silently docked an hour; unjudged hosts are no longer
  reported as healthy.

## Not included

- **The bond/slave double-count in `detect_disruption_wave`** and the
  **`TRAFFIC_DIVISOR` sweep** (9 modules still hardcode `/1e6`, which only
  misreads under a bytes-configured deployment). Both are real and queued;
  they are mechanical changes better done as their own reviewed batch.
- **`get_host` enrichment** (inventory / cost / traffic) — an enhancement,
  not a defect.
