# ADR 053: Suppress false RTT drift against a degraded baseline

**Status:** Accepted
**Date:** 2026-06-05

## Problem

`compute_loss_drift` (the pure brain behind `detect_loss_drift`) compares
a host's recent packet-loss and RTT against a 14-day baseline and flags
`rtt-up` when the recent RTT climbs a configurable step above baseline.

The baseline is just an average over the window — it carries no notion of
whether the window was healthy. When a host spent its baseline window in
an outage (heavy packet loss), the few probes that did return measured an
unreliable, often inflated-or-deflated RTT. A host that has since
*recovered* then reads as `rtt-up`: e.g. baseline 47% loss with RTT
76 ms, recent 0.09% loss with RTT 142 ms. The RTT "doubled," so the
detector flags drift — but the move is the host coming back to normal,
not degrading. A recovery is reported as a regression. This is the same
false-positive class the surrounding false-positive audit has been
closing across the detectors, surfaced here by the report-side
`_classify_loss_drift` in zabbix-reports.

## Decision

Gate the RTT-drift branch on baseline quality: an RTT delta is only
trustworthy if the baseline it is measured against is itself trustworthy.

Add `_BASELINE_LOSS_MAX = 20.0`. When `loss_baseline >= 20%` the baseline
is treated as an outage baseline (`baseline_degraded`), and the RTT-drift
comparison is skipped entirely — `rtt_delta_pct` stays `None` and no
`rtt-up` flag is raised. The loss-delta branch is untouched: a real loss
change is still detected on its own terms, and recovery (loss falling)
never raised a flag to begin with.

The 20% threshold mirrors zabbix-reports' `_classify_loss_drift`, keeping
the MCP detector and the report in agreement so the two never disagree on
the same host.

This is a pure-function change with no API surface and no new tool. It
only ever *removes* a flag (the false `rtt-up`); it can never introduce
one, so it cannot mask a genuine regression measured against a healthy
baseline.

## Test approach

`TestLossDriftDetection::test_degraded_baseline_suppresses_false_rtt_drift`
pins the motivating case directly: `compute_loss_drift(47.24, 0.09, 76.4,
142.5)` — a degraded baseline with a recovered, "doubled" recent RTT —
must classify `ok`, not `rtt-up`. The existing loss-drift cases continue
to cover the healthy-baseline paths (loss-up, new-loss, rtt-up below /
above step) unchanged.

## Consequences

- Tool count unchanged (161).
- Test count: +1 (560 → 561).
- No new env vars, no `WRITE_TOOLS` change, no API change — pure-helper
  behaviour only.
- A host recovering from an outage no longer produces a spurious
  `rtt-up`; loss-based detection on that host is unaffected.

## Not included

- **Per-probe baseline reconstruction.** A more precise fix would
  recompute the baseline RTT from only the healthy sub-windows. That
  needs per-interval history the detector does not fetch; the 20% gate is
  the cheap, report-aligned guard and is sufficient for the observed
  false positives.
- **Tuning `_BASELINE_LOSS_MAX` per host class.** A single fleet-wide
  threshold matches the report; revisit only if a host class proves to
  have a legitimately high steady-state loss baseline.
