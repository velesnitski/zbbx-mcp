# ADR 118 — A host capped from birth, which no before/after can find

**Status**: Accepted (2026-08-17)
**Affected**: `src/zbbx_mcp/tools/traffic_shaping.py` (`swing_ratio`,
`classify_peer_cap`, cohort pass, `PEER_CAPPED`),
`tests/test_traffic_shaping.py`.
**Closes**: task 188, with one correction to its premise.
**Extends**: ADR 104, 108.

## Context

A node was reported as under-performing. Every tool said it was fine:

- `diagnose_host` — **healthy**, 99% of its own baseline, no problems
- `detect_traffic_drops` — no drop
- `detect_traffic_erosion` — no decline
- `detect_traffic_shaping` — **normal**

All four were correct, and all four were useless, for the same reason: they are
**before/after** comparisons, and this host had no "before". It ramped from idle
straight into a ceiling on the hour it entered service and never once exceeded
it. A host capped from birth never falls, so nothing that looks for a fall can
see it.

What it looked like against peers sharing its country, product, provider and
interface: hourly peaks bounded at ~37 Mbps for five days while three identical
siblings peaked at 363–408. During busy hours its hourly *minimum* sat at 34–36
— demand pressing against a boundary continuously, not demand failing to
arrive. Its average-to-peak ratio was 0.82 against 0.37–0.41 for the siblings:
the daily curve was flattened, not scaled down.

## Correction to the task's premise

Task 188 assumed such hosts *are* visible as `capped`, and proposed refining
that verdict's wording with cohort context. Measured against the real series,
`ceiling_hit_rate` returns **54%** against a 60% gate, so the verdict is
`normal` and no refinement of `capped` would ever have fired. The gate, not the
wording, was the problem.

The task's rejection of *widening the tolerance* stands and is respected: ADR
108's modal point-mass already absorbs cap wobble, and a wider band erodes the
negative controls — a healthy diurnal plateau would start reading as capped,
which is the ADR 093 class this codebase keeps removing. So the tolerance is
untouched.

## Decision

A cohort pass, after the per-host verdicts, costing no extra fetch.

`swing_ratio` = `(p95 − p10) / p95` over a host's active hours. A host serving
real demand swings with the daily curve; a host held at a limit cannot,
**whatever the morphology of the limit**. That is what the hit rate cannot be:
it asks "is there a point mass", which needs a hard clip.

`classify_peer_cap` requires **both** conditions against the country × product
cohort:

1. swing materially below the cohort median, and
2. ceiling materially **below** the cohort's median peak level.

The second is the saturation guard and it is what keeps this honest: a host flat
*at or above* peer level is running at capacity — healthy utilisation, not a
limit. Only flat **and** low has been held back. Both are pinned by test,
including the case that must not fire: a host with genuinely less demand, whose
curve still swings.

Declined, rather than guessed, when there are fewer than three peers or when
the whole cohort is flat — "flatter than everyone" means nothing if everyone is
flat, and a verdict built from no contrast is not a verdict.

The result renders on **both** exits. A peer-capped host is normally `normal` by
the ceiling test — that is the entire point — so gating the section on the
existing `flagged` list would have hidden precisely the case it was added for.

## Consequences

- The one failure class in this tool that no self-comparison could reach now
  has a detector, and it needs no tuned threshold: 13% swing against a 46%
  cohort is not a borderline call.
- `peer capped` states what was measured — flat and below peers — and stops
  there. Whether the limit is a provider policy, a `tc` rule or a
  misprovisioned plan is not visible in throughput, and the verdict does not
  pretend otherwise.

## Verification

1024 tests pass (+7), built on the real hourly series from the live host. The
load-bearing ones: the ceiling test alone does **not** catch it (54% < 60%,
pinning the gap); flat-at-peer-level reads as saturation; a flat cohort and too
few peers both decline; and a lower-demand host whose curve still swings is not
flagged.
