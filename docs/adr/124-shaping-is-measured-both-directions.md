# ADR 124 — Shaping is measured in both directions

**Status**: Accepted (2026-08-19)
**Affected**: `src/zbbx_mcp/tools/traffic_shaping.py` (`combine_directions`,
`detect_traffic_shaping`), `tests/test_shaping_directions.py`.
**Extends**: ADR 104, 108, 118.

## Context

`detect_traffic_shaping` read inbound NIC trends only. That is half the link,
and the wrong half to pick if you can only have one: on a relay fleet the
provider's incentive is to limit **egress**, because egress is what it pays
transit for.

So an egress-only cap was not under-reported. It was **invisible** — the host
read `normal` on every ingress measure while its uplink was pinned.

## Decision

Classify each direction independently with the same core, then combine.

Independently matters: the two directions have their own baselines, their own
ceilings, and their own busy hours. Concatenating the series would blur a cap
on one side into an average across both.

The combination answers a question neither direction can:

| Observation | Reading |
|---|---|
| Both pinned, ceilings within 10% | One limit on the **link** — port speed or plan tier |
| Both pinned, different ceilings | **Two** limits — an asymmetric plan |
| One pinned, other free | A **shaper** on that direction |
| One pinned, other **not measured** | Cannot tell the two apart — said plainly |

Those are different tickets. From one direction they are the same picture.

That last row is the one worth guarding. With no data for the opposite
direction there is nothing to be asymmetric *against*, so a one-sided shaper
and a link-wide limit are indistinguishable — and calling it a shaper would be
exactly the confident wrong answer this tool exists to avoid. It says it cannot
tell.

A host whose egress is pinned while its ingress reads clean is **promoted into
the table**, not left to a footnote. Leaving it out would reproduce the blind
spot the change exists to close.

## Consequences

- One extra `item.get` and one extra `trend.get` per run. Bounded the same way
  as the inbound pass: top-N interfaces per host.
- The verdict shown for a host is its **worse** direction, since the action a
  reader takes follows the constrained side.
- The cohort/swing pass (ADR 118) still runs on inbound only. It is tuned
  against inbound peaks and re-tuning it was out of scope here; an egress-only
  jittery shaper therefore still relies on the hit-rate test.
- Discovery uses the existing `direction` parameter and the existing physical
  filter, so tunnels, bridges and loopback are excluded on egress exactly as on
  ingress — verified, since the fleet's outbound items include `docker_gwbridge`
  interfaces that must not be mistaken for an uplink (ADR 078 / 109).

## Verification

Worse-direction headline in both orderings; a missing direction never improving
a verdict; the four pair-readings above; and that the symmetry tolerance
discriminates — 9% apart reads as one limit, 11% as two — because a threshold
that always fires, or never does, is not a threshold.
