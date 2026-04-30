# ADR 014: At-risk skip rule and disruption-wave defaults

**Status:** Accepted
**Date:** 2026-04-30

## Problem

A smoke test of the disruption-detection tools shipped in ADR 013
surfaced two follow-on issues in their default behaviour:

- **#125** — `get_at_risk_hosts` ranked all 909 hosts at the same
  score `2.26`. Both peer_count and drift were inert in the test
  environment (no audit-log rotations, no ping items configured),
  leaving only the age component to fire. Since age caps at 90d for
  every never-rotated host, the rank degenerated to a constant.
- **#126** — `detect_disruption_wave` returned one "wave" of 567 hosts
  across 268 /24s at 100% avg drop. That was the fleet-wide overnight
  ramp-down being misread as a disruption. The defaults (`window_hours=6`,
  `recent_hours=1`, `drop_pct=30`, no baseline floor) were too tight
  for any fleet with a normal diurnal cycle.

Both fixes were pre-validated in the zabbix-reports port; this commit
keeps the MCP behaviour in sync.

## Decision

### #125 — substantive-signal gate in `get_at_risk_hosts`

After computing per-host signals, a host is dropped from the ranking
when `peer_rotations_7d == 0` AND `drift_label ∈ {"ok", "n/a"}`. Age
alone is not a disruption predictor without another firing signal —
it only matters as a tiebreaker between hosts that already have peer
churn or drift.

The pure helper `_compute_risk_score` is unchanged; the gate is in
the calling tool because it depends on the input combination, not on
the score arithmetic.

### #126 — diurnal-safe defaults in `detect_disruption_wave`

| Argument | Old | New | Reason |
|----------|----:|----:|--------|
| `window_hours` | 6 | 12 | Span at least half a day so the baseline averages across both peak and off-peak |
| `recent_hours` | 1 | 2 | Reduces sensitivity to a single anomalous hour |
| `drop_pct` | 30 | 50 | 30% off-peak vs midday baseline is normal traffic shape, not a disruption |
| `min_baseline_mbps` | — | 5.0 | New floor; mirrors `detect_traffic_drops`. Hosts below 5 Mbps are too noisy to count |

The signature gains one new keyword-only argument with a default; no
breaking change for existing callers.

## Test approach

No new unit tests in this commit — both fixes are configuration-level
changes (the substantive-signal gate is a one-line filter on existing
inputs; the wave defaults are constants). The existing 26 helper
tests still pass; the fixes do not touch any pure-helper logic.

## Consequences

- 307 tests pass (unchanged).
- Tool count is still 153.
- `get_at_risk_hosts` now returns an empty result on a fleet with no
  rotations or ping items configured, instead of returning the entire
  fleet at a constant score. This is the correct behaviour — the tool
  has nothing useful to say without at least one substantive signal.
- `detect_disruption_wave` is silent on a normal diurnal day. It only
  fires when ≥5 hosts on ≥3 distinct /24s drop ≥50% against a 12h
  baseline, with each host carrying at least 5 Mbps in that baseline.

## Not included

- A ping-items-not-configured warning in `get_at_risk_hosts`. The tool
  silently treats `n/a` as a non-firing signal rather than warning,
  on the principle that the operator already saw the env-var docs in
  `detect_loss_drift`. Revisit if this confuses users.
- A maintenance-window awareness layer in `detect_disruption_wave`.
  Same trade-off as in ADR 010.
