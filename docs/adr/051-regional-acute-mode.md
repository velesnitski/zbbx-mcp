# ADR 051: Acute mode for `detect_regional_anomalies`

**Status:** Accepted
**Date:** 2026-06-04

## Problem

ADR 047 rebuilt `detect_regional_anomalies` on the classifier at a
**daily** grain — comparing each host's recent-days average to its
baseline-days average. That removed the diurnal false positives, but
daily aggregates cannot detect an *acute* regional block: a drop that
started a few hours ago is diluted in today's daily average, so a
genuine immediate regional event is missed until it has run most of a
day.

ADR 047 named this as the deferred deeper version: fetch hourly trends,
aggregate per country, and build a per-country same-hour seasonal floor
for acute detection.

## Decision

Add an **opt-in** acute mode (`acute=False` by default), leaving the
diurnal-safe daily path as the default so existing behaviour and API
volume are unchanged.

When `acute=True`, `_detect_regional_acute`:

1. Picks one main traffic interface per host (highest current value —
   bounded, the same shortlist principle as `detect_traffic_drops`
   after the v1.10.1 volume fix).
2. Fetches hourly trends for those interfaces over `baseline_days`.
3. Sums them into a **per-country hourly series** via the new pure
   helper `anomaly.aggregate_hourly_by_country` — values at the same
   hour bucket add across the country's hosts.
4. For each country, computes a recent-window average (`recent_hours`)
   vs a baseline average, derives the **same-hour-of-day seasonal
   floor** (`seasonal_floor`) of the country aggregate, counts
   persistence, and runs `classify_drop`.
5. Flags countries that classify `blocked_acute` / `blocked_sustained`,
   rendering state + confidence + reason per country.

This is the country-grain analogue of the per-host hourly detection in
`detect_traffic_drops`: the seasonal band makes "the country's
aggregate traffic is below its normal level for this hour, right now"
an immediate signal, with persistence escalating acute → sustained.

## Why opt-in

The default daily path already removed the live false positives (the
goal of ADR 047). Acute mode adds a *capability* — immediate regional
detection — at the cost of an extra item.get + hourly trend.get. Making
it opt-in keeps the common call cheap and the new path available when
ops needs immediate regional awareness, with no risk to the established
default.

## Test approach

5 unit tests in `test_anomaly.py::TestAggregateHourlyByCountry`:

- sums same-hour values across a country's hosts;
- separates countries;
- drops a host with no country mapping;
- result sorted by clock;
- empty input → empty.

`classify_drop` and `seasonal_floor` are already covered by
`test_anomaly.py`. The acute path wires those tested helpers plus the
new aggregator; the interface-selection and trend-fetch are
configuration-level, and the async tool is covered by the registration
/ smoke tests.

## Consequences

- Tool count unchanged (161).
- Test count 552 → 557.
- `WRITE_TOOLS` unchanged. No new env vars.
- Default behaviour unchanged (`acute=False`).
- `acute=True` adds two API calls (one item.get, one hourly trend.get),
  bounded to one interface per host.

## Not included

- **Per-host attribution in acute output.** The acute table reports per
  *country*; it does not list which hosts drove the country aggregate
  down. The daily (default) mode still gives the per-host detail; a
  drill-down for acute is a later nicety.
- **CPU / connection corroboration at the country grain.** As in ADR
  047, the seasonal band carries the diurnal safety; block-vs-demand
  corroboration (ADR 042) is not wired into the regional path.
- **Multi-week (weekday/weekend) seasonality.** The 7-day baseline gives
  ~7 same-hour samples per country; a weekday/weekend split would need a
  longer window. Revisit if weekend troughs leak.
- **Backporting acute mode to `detect_disruption_wave`** — that detector
  has its own diurnal guards (ADR 020) and is tracked separately
  (tasks.md #156).
