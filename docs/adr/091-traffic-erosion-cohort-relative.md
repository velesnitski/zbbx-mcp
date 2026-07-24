# ADR 091: `detect_traffic_erosion` — cohort-relative multi-week slow-decline detector

**Status:** Accepted
**Date:** 2026-07-24

## Problem

`detect_traffic_drops` (ADR 040) is an **acute** detector: it compares a recent
window against a 7-day seasonal baseline and fires on a large same-hour drop.
By construction it cannot see a **gradual** multi-week decline — each day sits
only slightly below the trailing week, so the drop ratio never crosses the
threshold, yet over a couple of months a host can bleed most of its traffic.
This is a distinct, real failure mode (gradual reachability loss, effectiveness
decay, slow demand rot) with no instrument in the suite. The existing
trend/geo tools don't fill the gap either: hourly-trend uptime smooths the shape
away, and `get_geo_traffic_trends` aggregates by country, so a subset of hosts
eroding inside a healthy country is invisible.

The naive fix — "flag any host whose traffic fell over N weeks" — produces a
systematic false positive: a scope-wide demand dip (seasonality, a holiday lull)
drags **every** host down together, which is not a host-specific problem.

## Decision

New read-only tool **`detect_traffic_erosion`** (`tools/traffic_erosion.py`,
tool count 164 → 165): fits a slope to each host's **weekly-mean** throughput
over a bounded window (`weeks`, default 6, cap 12) and judges each host
**cohort-relative** — a host is flagged as *eroding* only when it declines
materially faster than the median slope of its scope. A decline that merely
tracks the cohort is labelled *demand*, not erosion. That single design choice
is what separates a host-specific problem from a fleet-wide one.

Pure core, unit-tested in isolation:

- `weekly_means(points, now, weeks)` — bucket `(epoch, mbps)` into weekly means,
  oldest→newest; a retention gap **shrinks** the series rather than injecting a
  zero (which would fabricate a trough).
- `linreg_slope(xs, ys)` — least-squares slope, zero-variance guarded.
- `classify_erosion(weekly, …)` → verdict in strict priority:
  1. **insufficient** — fewer than `min_weeks` (4) weekly points to fit a trend.
  2. **idle** — peak weekly mean below `min_baseline_mbps` (default 1.0): a
     spare / out-of-rotation host whose "decline" is noise on a near-zero
     denominator (the same denominator rule as ADR 040/042).
  3. **recovering** — a material rise (so a rebound is never called a drop).
  4. **eroding** — declined ≥ `min_decline_pct` (first-third vs last-third,
     robust to one spiky week) with a negative slope AND, when
     `cohort_relative`, doing so at least `relative_margin_pct` %/week faster
     than the cohort median.
  5. **demand** — same decline, but only tracking the cohort.
  6. **stable** — everything else.

The wire runs two passes: pass 1 computes each host's cohort-blind slope to form
the cohort median (over non-idle, sufficient hosts); pass 2 classifies against
it. A cohort of fewer than `_MIN_COHORT` (3) non-idle peers is degenerate — a
one-host median is that host's own slope — so the tool falls back to an
**absolute** decline verdict (`cohort_slope_pct=None`) and says so in the header.

Interface selection reuses the acute detector's bound: shortlist the top-N
`net.if.in` items per host by current value before the (heavy) trend fetch, then
pick the interface with the highest whole-window mean (the real uplink, not a
spiking tunnel — `pick_traffic_interface`). Traffic is scaled through the shared
`TRAFFIC_DIVISOR` so a bytes/sec deployment (ADR 087) is handled. Test hosts are
dropped from the cohort by default (ADR 080/089). Output ranks the declining set
(eroding before demand, steepest-relative first), states the cohort median, and
counts the rest; `max_results` caps the table.

The operator follow-up — deciding whether a confirmed erosion is a monitoring-
side artefact or a real reachability loss — needs an active out-of-path probe,
which Zabbix cannot provide from the serving side; that is deliberately out of
scope here. The tool's job is to make the slow decline **visible and
attributable** (host-specific vs scope-wide), which passive bandwidth alone
could not do before.

## Test approach

`tests/test_traffic_erosion.py` (+19): weekly bucketing (oldest→newest, in-week
mean, gap-week omitted not zeroed, out-of-window dropped); slope sign/flat/single
-point; and each classification fact as an invariant — steady decline vs a flat
cohort is *eroding*, the same decline tracking the cohort is *demand*, faster
-than-cohort is *eroding*, a no-cohort scope flags on absolute decline,
below-floor is *idle*, too-few-weeks is *insufficient*, flat is *stable*, a rise
is *recovering*; wire contract (trend requested with `value_avg` over the window
on the shortlisted item; no-items and no-match messages). 797 → 816.

## Consequences

- The slow-erosion class is now enumerable and attributable, with a scope-wide
  demand dip separated from host-specific decline rather than blamed on hosts.
  Tool count 165. Added to the `ops` tier alongside `detect_traffic_drops`.
- Complementary, not overlapping: `detect_traffic_drops` owns the acute cliff;
  `detect_traffic_erosion` owns the multi-week slope. Neither sees the other's
  signature.

## Not included

- **An active out-of-path reachability probe.** Confirming whether an erosion is
  a real access loss vs a monitoring artefact needs a vantage Zabbix does not
  have from the serving side.
- **Auto-creating a trend-based trigger.** Threshold choice is an operator
  decision; the tool names the candidates.
