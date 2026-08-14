# ADR 108 — The ceiling is a point mass, not a band around p95

**Status**: Accepted (2026-08-14)
**Affected**: `src/zbbx_mcp/tools/traffic_shaping.py` (`ceiling_hit_rate`,
module docstring), `tests/test_traffic_shaping.py`.
**Extends**: ADR 104.

## Context

An independent adversarial validation of ADR 104 ran controls the original
tests did not: a hard policer (→ `shaped`, correct), a falling diurnal curve
(→ `dropped`, correct), a flat steady load (→ not shaped, correct) — and a
**token bucket**, which failed.

A token bucket permits an occasional burst above its cap. With ~8% overshoot
every ~11 hours, that burst becomes p95. The real cap then sits *below* the ±2%
band around p95, so almost no hour counts as touching the ceiling: hit rate
collapses to ~7% and the verdict is `dropped` — *"peaks fell 62% … but still
spread … demand or reachability, not a cap"*.

Reproduced before fixing: a 48-hour sine clipped at 120 Mbps with every 11th
hour ×1.08, against a 350-peak baseline, read `dropped` at 7% hit rate.

That is the worst possible failure for this tool. Burst tolerance is *normal*
policer configuration, so the shape most likely to be a real rate limit was the
one the detector denied — and it denied it confidently, in the verdict wording,
which is the ADR 093 class this codebase keeps hunting.

## Decision

The ceiling is the **modal point mass**: the value where the most active hours
cluster, found by scanning every observed value as a candidate and counting how
many active hours fall within `tolerance` of it. The hit rate is that cluster's
share.

- p95 no longer *defines* the ceiling; it still sets the **active** threshold,
  where a single outlier is harmless.
- Scanning every observed value is exact and costs nothing at these sizes (a
  few hundred hours), so there is no bin width to tune and no binning artefact.
- Ties prefer the higher value, so a cap reads as the cap rather than as some
  trough beneath it.
- The modal rule is **more** outlier-robust than p95, not less: a lone freak
  minute forms a cluster of one and always loses. Pinned by test.

The negative control that constrains the design: a real sine spends more time
near its extremes, so a naive "most common value" rule could read a healthy
daily peak plateau as a cap. An uncapped diurnal series must still read
`normal`, and does.

## Consequences

- The three shapes a rate limit actually takes in the field — hard cap, burst
  tolerant, and pre-existing — all classify correctly.
- `ceiling_mbps` is now the cap rather than the burst, so `drop_pct` measured
  against it is right too. Both windows use the same estimator, so the
  comparison stays consistent.
- Known lag, documented in the tool rather than fixed: a **ratcheting** cap,
  stepped down more than once inside the recent window, reads `normal` until it
  settles, because no single value holds the point mass yet. Roughly one run of
  latency, self-resolving. Splitting the recent window in half and comparing
  halves would close it; worth doing only if it bites in practice.

## Verification

935 tests pass (+6). The load-bearing ones: the burst-tolerant repro now reads
`shaped`; the reported ceiling locks onto 120 and not the 129.6 burst; the
uncapped diurnal control still reads `normal`; a lone 9999 spike cannot become
the ceiling.
