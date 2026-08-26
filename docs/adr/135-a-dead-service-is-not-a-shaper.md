# ADR 135 — A dead service is not a shaper

**Status**: Accepted (2026-08-26)
**Affected**: `tools/traffic_shaping.py` (`combine_directions`),
`tests/test_shaping_directions.py`.
**Extends**: ADR 104, ADR 107, ADR 124, ADR 125.
**Origin**: contributed as PR #2 by an outside contributor, with two
follow-up corrections from review.

## Context

`SHAPED` means two things at once: the ceiling fell, **and** the peaks piled
onto it. A dead relay satisfies both by accident.

When a service dies, clients keep knocking. The inbound side therefore carries
a few Mbps of retry traffic, and because that residual is machine-generated it
has no diurnal shape — so the peaks sit flat on one value, which is exactly the
policer signature this module looks for. Meanwhile the baseline collapsed along
with the service, so the drop threshold clears too. Both halves are satisfied,
and the tool recommends opening a provider ticket for a box that is serving
nothing at all.

The direction note already printed "out reads idle" beside it, but the headline
still said `shaped`, and the headline is what a reader acts on.

This is the same error the surrounding ADRs keep circling: a signal that
resembles evidence *for* something is taken as that evidence, when it is
actually evidence of something else entirely. Here the flat inbound line is not
a wall being hit — it is the sound of nobody answering.

## Decision

**An inbound pin opposite a dead egress is a dead service, not a policer.** It
becomes `dropped`, which routes the reader to the drop detector and the host,
rather than to the provider.

The scoping is what makes this safe, and it follows ADR 107 exactly:

- **`idle` egress demotes.** Idle is a *measured* empty egress — it is
  evidence, and evidence may overturn a finding.
- **`insufficient` egress demotes only when the pin is itself residual.**
  Insufficient is an absence of judgment, and absence must not overturn a
  finding. It is allowed to contribute only when the pinned side sits at
  machine-chatter magnitude, where `shaped` was never a credible reading of a
  serving relay.
- **Egress pinned with idle ingress is left alone.** Send-only hosts — log
  shippers, backup origins — legitimately look like that, and the demotion is
  scoped to the inbound-residual signature only.

Two corrections were made during review.

**Both pinned verdicts demote, not only `shaped`.** A service that died
recently reads `shaped` because the ceiling fell. Once the baseline window
rolls past the death, the baseline is residual too, no drop remains to measure,
and the very same dead box reads `capped` instead. Keying on `shaped` alone
would have given the fix a shelf life of one baseline window per incident, and
the box would resurface as a provider-ticket recommendation a week later, cured
of nothing. The collapse quietly becoming the new normal is a recurring shape
in this detector, and it has to be designed against rather than discovered
again.

**The thresholds are named constants.** They were inline literals inside a
boolean. A fleet whose idle chatter runs above the residual threshold would
have found the demotion silently ceasing to apply — and silent inapplicability
is precisely the failure mode this module exists to prevent.

## Consequences

A dead relay stops being reported as a rate-limited one, at any age.

The note is worded per verdict ("not a shaper" / "not a cap") so it reads
naturally either way, which also let the contributor's original assertions
stand unchanged.

Twelve tests cover it, and the ones that matter most are the negatives: a real
pre-existing cap with live egress is not demoted, a full-size pin opposite an
unjudgeable egress is not demoted, and a send-only host keeps its verdict. A
demotion rule that fired on everything would be the same error pointing the
other way.

`_DEAD_EGRESS_MBPS` and `_RESIDUAL_MBPS` are module constants rather than tool
parameters. That is a deliberate stopping point: they are calibration, not
policy, and adding two more arguments to a tool that already takes four
thresholds would trade one kind of obscurity for another. If a deployment ever
needs them tuned, that is the moment to promote them — not before.
