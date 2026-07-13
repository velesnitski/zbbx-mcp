# ADR 020: Disruption-wave cohesion guard + peer-relative drop classification

**Status:** Accepted
**Date:** 2026-05-05

## Problem

A smoke test of `detect_disruption_wave` fired on what looked like a
real wave but was actually a diurnal artifact: a small cluster of hosts
spread across nearly as many distinct /24s, at a ~60% average drop, with
the hosts scattered over several unrelated regions. The fleet has hosts
in many time zones, so the 12-hour baseline straddles regional peaks for
some of them; mid-day UTC catches enough of those hosts ≥50% below their
own baselines that the cluster meets the absolute thresholds.

The `min_subnets` guard added in ADR 014 was meant to reject "all
in one rack" false positives but actually *requires* the kind of
geographic spread that diurnal artifacts naturally produce, so it
fired on the wrong direction.

The same fix already shipped in zabbix-reports vpn_brief.py
(`fetch_mass_disruption_section` traffic-drop branch); MCP needs
parity to keep the primitive accurate for any consumer.

## Decision

Two independent gates, both implemented as pure helpers, both wired
into `detect_disruption_wave` with new keyword arguments. They are
complementary and ship together because each leaves a real gap on
its own.

### #134 — Country-cohesion guard inside `_compute_waves`

The cluster-detection helper gains a `min_country_concentration`
keyword argument (default `0.4`). Each drop record may now carry a
`country` field; when present, the cluster is accepted only if the
top country accounts for at least `min_country_concentration` of the
records inside it. Records lacking a `country` field bypass the
cohesion check (backwards-compat for callers that haven't been
updated).

Output adds two new fields per wave: `top_country` (the country with
the largest share inside the cluster) and `top_country_share`
(0–1). The tool's rendered output prefixes a "Centered on: TR (60%)"
line so the operator sees the geo cluster on every wave.

The cohesion check happens *inside* the slide-along loop, so a
cluster that fails the threshold can still produce a downstream
sub-cluster that passes — for example, a 3 TR + 2 ID cluster fails
at threshold 0.9 because the only sub-bucket large enough to clear
`min_hosts=5` includes both countries. This matches the operator
intent: a single tight cluster gets one chance to clear the bar.

### #135 — Peer-relative drop pre-filter

A new pure helper `_compute_peer_relative_drops` gates each candidate
host on two thresholds:

1. **Absolute:** `drop_pct ≥ min_drop_pct` (existing behaviour).
2. **Relative:** `drop_pct − cohort_avg_drop ≥ min_peer_relative_drop`
   where the cohort is identified by `cohort_key` (the tool sets it
   to `"product:tier:country"`).

Hosts in cohorts smaller than `min_cohort_size` (default 3, including
self) skip the relative gate — there are not enough peers to form a
stable comparison. Such records carry `cohort_drop=None` and
`peer_relative_drop=None` so consumers can see they are unvalidated.

The point of the gate: when a region's diurnal cycle pushes every
host in a cohort 60% below its own baseline, every host's
peer-relative drop is ≈ 0. The gate rejects them. When one host
genuinely fails while peers stay flat, that host's peer-relative
drop is large (e.g., 80% vs 5%) and it passes.

The tool fetches trends for *all* hosts (not just candidates), so
the cohort comparison uses data we already paid for — no extra API
calls.

### How they compose

- The cohesion guard catches **globally-spread** false positives
  where peers don't share a cohort (single-host countries, mixed
  product/tier).
- The peer-relative gate catches **regionally-clustered** false
  positives where peers do share a cohort and all drop together.

Each alone leaves the other class of FP visible. Both ship.

## Test approach

12 new unit tests in `test_analytics.py`:

**Cohesion guard (5):**
- Globally spread (5 different countries) is filtered out.
- All-one-country cluster passes with `top_country_share=1.0`.
- Partial concentration (4 TR / 1 ID / 1 MX = 67%) passes at
  default 0.4.
- Threshold is configurable — same input fails when raised to 0.9.
- Records without a `country` field bypass the check (backwards
  compat).

**Peer-relative filter (7):**
- Below absolute threshold dropped.
- Diurnal-cohort uniformly-dropped hosts filtered out.
- Genuine outlier (host drops 80% while peers drop 10%) kept with
  correct `peer_relative_drop`.
- Small cohort (size < 3) passes absolute-only with `cohort_drop=None`.
- Solo cohort (size 1) passes absolute-only.
- Threshold is configurable — same input rejects at 20% and accepts
  at 5%.
- Separate cohorts evaluated independently (rejects cohort_a uniform
  drop, keeps cohort_b outlier).

The async tool wrapper does only fetch + render and is covered by
the existing registration / smoke tests.

## Consequences

- 365 tests pass (353 pre-change + 12 new helper tests).
- Tool count unchanged at 155.
- `WRITE_TOOLS` unchanged.
- `detect_disruption_wave` gains two optional keyword arguments
  with diurnal-safe defaults; existing callers see no breakage but
  do see far fewer false positives in normal-time-of-day windows.
- The wave output now includes a "Centered on: …" line per wave
  so the geo cluster is always visible.
- Future use: when a wave fires, `top_country` is a structured
  field consumers can use without re-parsing the rendered text.

## Not included

- Multi-country cohesion (e.g., "75% from EMEA"). The current
  cohesion check is a single-country argmax; an extension to
  region-of-cohesion would need a country→region mapping (we have
  one in `data.REGION_MAP`) but that's a separate enhancement.
- A `min_peer_relative_drop` knob exposed per-cohort. The current
  global default is fine for the present use cases; a per-cohort
  override is over-engineering until someone needs it.
- Maintenance-window awareness inside the cohort. A peer in
  scheduled maintenance contributes a 100% drop into the cohort
  average, which masks real outliers; same trade-off as ADR 014.
- Calculated-item collapse from ADR 019 #133 — independent feature.
