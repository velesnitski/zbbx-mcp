# ADR 011: Traffic-drops skip visibility and shutdown peer-headroom

**Status:** Accepted
**Date:** 2026-04-30

## Problem

Two long-standing low-priority items in `tasks.md` (#42 and #43)
turned out to be load-bearing once we started recommending shutdowns:

1. **`detect_traffic_drops` silently dropped the long tail.** Three skip
   conditions — no trend data, no records older than 24h, baseline
   below a hard-coded `1 Mbps` floor — removed servers from analysis
   without telling the operator. A fleet of 220 servers might quietly
   become "Analyzed 173 servers"; the missing 47 were invisible. We
   could not distinguish "looks healthy across the board" from "we
   never even checked half of these".

2. **`get_shutdown_candidates` did not tell you whether the shutdown
   was safe.** A server flagged DEAD/IDLE/ZOMBIE/BROKEN got the badge
   regardless of whether the rest of its country/product/tier could
   absorb its traffic. A perfectly idle server may still be the only
   server in its region — shutting it down strands users.

## Decision

### #42 — Skip-breakdown footer + configurable floor

`detect_traffic_drops` now:

- Counts skips by reason: `no_history`, `no_baseline_window`,
  `below_floor`.
- Accepts a new `min_baseline_mbps` argument (default `1.0` —
  preserves prior behaviour) so the floor is no longer a magic
  number.
- Reports `Analyzed X of Y` in the header when drops are found, and
  appends a one-line skip breakdown when any servers were skipped:
  `47 skipped: 12 no-history, 5 no-baseline-window, 30 below-1Mbps-floor.`
- Same line is appended to the "no drops detected" message so the
  operator never sees a green-light response without knowing the
  blind spot.

The skip itself is unchanged — without a baseline you cannot compute
a drop. The change is making the skip *visible*.

A small pure helper `_format_skip_breakdown(skips, min_baseline_mbps)`
holds the rendering and is unit-tested directly without HTTP.

### #43 — Peer-headroom safety check

For each shutdown candidate, build the peer cohort:

```
cohort_key   = (product, tier, country)
cohort_peers = filtered ∩ same_key − {self} − other_candidates
```

Other candidates are excluded so a wave of "shut down all of these"
does not get rubber-stamped by counting each other as available
headroom.

Per-peer spare capacity is `peak − avg`; cohort headroom is the sum
across peers. A peer with negative spare (data anomaly) contributes
zero, never negative.

Safety label:

| Label | Condition |
|-------|-----------|
| `SOLO` | No peers in cohort — cannot shut down regardless of load |
| `SAFE` | Cohort headroom ≥ candidate avg × 1.5 (50% safety margin) |
| `RISKY` | Cohort headroom is positive but below the safety margin |
| `N/A` | Candidate has no traffic figure (no trend data) |

The output renders the badge inline next to each candidate:

```
**IDLE (3):** host-x (1.2 Mbps) SAFE (200Mbps headroom),
              host-y (0.5 Mbps) RISKY (30Mbps headroom),
              host-z (0.1 Mbps) SOLO (no peers)
```

DEAD candidates only show a badge when `SOLO` or `RISKY` (the SAFE
case is the boring default for already-dead servers).

A footer line summarises non-SAFE counts:

```
Peer-headroom: 1 SOLO (no peers), 1 RISKY (insufficient cohort capacity).
```

Pure helper `_compute_shutdown_safety(candidate_avg, peers,
safety_margin=1.5)` returns `(label, headroom_mbps)` and is unit-
tested with eight cases covering the SOLO / SAFE / RISKY / N/A
matrix, the safety-margin parameter, missing peer metrics, and the
zero-load (DEAD) edge.

## Test approach

13 new tests in `test_analytics.py`:

- 5 for the skip-breakdown formatter (empty, single reason, all three,
  configurable floor, zero-categories-omitted).
- 8 for the headroom check (SOLO, SAFE, RISKY, configurable margin,
  negative-spare-clamped-to-zero, missing-peer-metrics dropped, N/A
  candidate, zero-load DEAD candidate).

## Consequences

- 259 tests pass (246 pre-change + 13 new for the helpers).
- `detect_traffic_drops` gains an optional argument with backwards-
  compatible default. No caller-visible breakage.
- `get_shutdown_candidates` output gains inline safety badges and a
  one-line summary footer. Existing fields are unchanged.
- Tool count stays at 145. Neither change adds a new MCP tool.
- `WRITE_TOOLS` is unchanged — both tools remain read-only.

## Not included

- A multi-period peer-headroom view. The cohort capacity is computed
  from the same `period` argument as the candidate analysis (default
  `7d`); we do not separately model a peak-hour vs. off-peak headroom.
  In practice peak-hour analysis is the right window for this question
  and `period="1d"` covers it. A future tool could fold weekly peaks
  in if the false-confidence case shows up.
- Maintenance-window awareness in the cohort. A peer currently in
  scheduled maintenance still counts as available headroom. Rare in
  practice; revisit if it bites.
