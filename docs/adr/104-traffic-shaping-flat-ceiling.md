# ADR 104 — Shaping is a shape, not a level: the flat-ceiling detector

**Status**: Accepted (2026-08-14)
**Affected**: `src/zbbx_mcp/tools/traffic_shaping.py` (new),
`tools/__init__.py`, `tools/tiers.py` (`ops`), `tests/test_traffic_shaping.py`.
**Context**: a request for automated graph analysis that would flag "a sharp
throughput drop signalling that traffic shaping has kicked in".

## Context

Two detectors already answer *is it lower*: `detect_traffic_drops` (ADR 040,
acute step vs a 7-day seasonal baseline) and `detect_traffic_erosion` (ADR 091,
multi-week cohort-relative slide). Neither answers *why*, and that is the
question with a cost attached — **shaped** means open a provider ticket,
**demand** means do nothing. In an average the two are indistinguishable: both
are simply less traffic than before.

Shaping does have a fingerprint, but it is not in the level. A policer
truncates the top of the distribution: every hour that wants more than the cap
reports exactly the cap, so the busy hours pile up on one value. Ordinary
demand reaches its maximum once or twice and spreads out below it, and keeps
swinging with the diurnal curve even while falling.

## Decision

A new tool, `detect_traffic_shaping`, reading hourly **`value_max`** — never
`value_avg`. A steady load has a stable average and still-moving peaks; only a
cap flattens the peaks themselves, so averaging erases the single signal that
separates the two cases. A wire test asserts `value_max` is requested and
`value_avg` is not, because that regression would be silent.

The metric is the **ceiling hit rate**: of the hours where the host was
actually pushing (peak ≥ 50% of the p95 ceiling), what share sit within 2% of
that ceiling. Clipping produces a point mass there; demand does not.

Verdicts: **shaped** (ceiling fell ≥25% AND hit rate ≥60%), **capped** (hit
rate high, no drop), **dropped** (fell, peaks still spread — handed back to
`detect_traffic_drops`), **normal**, **idle**, **insufficient**.

Both halves are required for `shaped`, and with no usable baseline the drop
test cannot run at all, so such a host reads `capped` — the tool never claims
a change it had no way to observe.

### The metric that looked right and was not

The first implementation measured the **spread (CV) of the top quartile** of
hours. It is the obvious reading of "the peaks went flat", and it is wrong:
selecting the largest values compresses *any* distribution, so a perfectly
healthy series scores as flat as a shaped one. It fired `capped` on the first
synthetic control tried. The test has to be how MANY hours reach the ceiling,
not how tightly the highest few agree. Both the healthy-varying and the
diurnal controls are kept as tests precisely because they broke it.

## On the graph-reading premise

The request describes analysing graph *images* against an annotated reference set.
The image is a lossy rendering of `trend.get`, which we already have: reading
the numbers is strictly more accurate than reading pixels of the numbers, needs
no reference set, and lets a verdict cite exact Mbps and timestamps. The
valuable half of the ticket is the **classifier** — shaped vs demand vs block —
not the modality. The annotated set is still worth assembling, as few-shot
examples of the numeric shape for a skill built on this tool.

## Consequences

- The ambiguity that made a traffic drop unactionable now resolves to a next
  step in the verdict itself.
- `capped` cannot distinguish a hard rate limit from genuinely constant demand
  — throughput alone does not carry that information. The verdict is worded as
  an observation rather than a diagnosis, and it is listed separately from
  `shaped` so the two are never read as the same finding.
- Added to the `ops` tier (59 → 60): this is an incident-response question.

## Verification

914 tests pass (+18). The load-bearing ones are the negative controls — a
healthy varying series and a normal diurnal curve must never read as shaped —
plus the wire test pinning `value_max`.
