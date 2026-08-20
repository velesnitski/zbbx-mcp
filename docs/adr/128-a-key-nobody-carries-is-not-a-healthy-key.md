# ADR 128 — A key nobody carries is not a healthy key

**Status**: Accepted (2026-08-20)
**Affected**: `tools/service_brief.py` (`generate_service_brief`),
`tools/executive.py` (`get_sla_dashboard`), `tools/geo_health.py`
(`get_service_uptime_report`), `tests/test_service_brief_scoping.py`.
**Completes**: the audit ADR 126 began. **Extends**: ADR 113, ADR 114, ADR 117.

## Context

ADR 126 fixed one rollup that scored a missing check as a failing one. This is
the audit of the other four surfaces that judge health by the configured check
keys. Each was checked for the same question in both directions: **does a fleet
whose real checks live outside the configured keys read falsely down, or
falsely up?**

The results differ per surface, and the differences matter more than a single
verdict would.

**`generate_service_brief` — falsely UP.** Its per-protocol table is built from
`blocked_by_check`, populated only from checks that FAILED. A key carried by no
host therefore produces no rows, which is indistinguishable downstream from a
key carried by many hosts of which none failed. The empty-rows branch rendered
**"all healthy"**.

This is the more dangerous direction. A false DOWN gets investigated; a false
green does not. And the whole section was gated on `if blocked_by_check`, so
when nothing anywhere was failing the table vanished entirely — taking the
disclosure with it, at precisely the moment a reader concludes the fleet is
fine.

**`get_sla_dashboard` — correct verdict, undisclosed coverage.** It skips hosts
that do not carry the key, which is right: absent evidence must not become a
down vote. But it consults exactly **one** configured key and the skip is
silent, while the output reads as a whole-fleet SLA. A fleet serving under a
different key does not appear as unhealthy; it does not appear at all.

**`get_service_uptime_report` — narrower than its name.** It reads the primary
and secondary keys and never the tertiary, unlike the matrix which reads all
three. It has no numerator/denominator asymmetry: it builds rows only from
items that exist and folds afterwards, so a sub-host with no item never enters
and cannot drag its group down. The prediction that it would repeat ADR 126's
defect was wrong, and checking beat assuming.

**`get_fleet_risk_score` — not applicable.** It scores provider concentration,
CPU, redundancy and traffic distribution, and consults no service check key.

## Decision

**Count carriers separately from failures.** `generate_service_brief` now
counts how many hosts carry each configured key and distinguishes the two empty
cases: a key nobody carries is reported as such and explicitly labelled *not
measured, not healthy*; a key that is carried and passing still reads "all
healthy", with its carrier count. The section renders whenever there is
something to say, including when nothing is failing.

**Disclose coverage where the verdict is right but partial.**
`get_sla_dashboard` counts the hosts it skipped and says so. The uptime report
names the configured key it does not read.

**Say what the columns are.** Where a surface measures configured keys, the
output says that a protocol served under another key is absent rather than
down, and points at the tool that walks every check item.

## Consequences

Task 190 is closed across all five surfaces: one real defect, two disclosed
limits, one non-issue, one not applicable.

The pair ADR 126 / ADR 128 is worth reading together, because the same root
cause produced **opposite** symptoms. Where absence entered a numerator it
read as failure; where absence entered a filter it read as health. Both are
the same error — treating "no evidence" as a value — and neither is visible
from the output alone, which is why both needed the wider walk as ground truth.

What remains, and is deliberately not fixed here: while these surfaces measure
configured keys, they answer "are the configured keys answering", not "is the
fleet serving". Widening to discovered checks changes what every column means
and needs its own decision.

Six tests pin the new behaviour; four were confirmed to fail against the
previous code first.
