# ADR 126 — A missing check is not a failing one

**Status**: Accepted (2026-08-20)
**Affected**: `src/zbbx_mcp/tools/geo_health.py`
(`get_service_health_matrix`), `tests/test_health_matrix_scoping.py`.
**Extends**: ADR 107, ADR 113, ADR 114, ADR 117.

## Context

The health matrix folds sub-hosts into canonical groups and scores each group
per protocol. It computed the two halves of that score over **different sets**:

```python
if all(vmap.get(hid) == 1 or hid in active_by_traffic for hid in group_ids):
    up += 1
if any(hid in vmap or hid in active_by_traffic for hid in group_ids):
    checked += 1
```

A sibling that carries no item of that key is absent from `vmap`, so
`vmap.get(hid)` is `None`, `None != 1`, and `all(...)` is False — the group
scores **down**. Meanwhile `any(...)` still counts it in the denominator,
because the *other* half does carry the item. Absence entered the numerator as
failure and the denominator as evidence.

On this fleet the two halves of a pair are routinely provisioned differently,
so this is the ordinary case, not a corner one. The live symptom, found while
re-measuring after the check-key rename: a country whose every protocol
answered `1` on **both** halves of **all** its pairs reported `DOWN (0/3)` —
while the wide walk in `detect_dead_protocols` found every check on every host
alive. Roughly twenty countries carried the same shape, all of them smaller
deployments where paired hosts dominate the sample.

Two things made it durable.

**There was no behavioural test on this tool at all** — its name appeared in
the registration list and nowhere else. The suite was green because nothing
ever asked this tool a question.

**The failure was quiet and plausible.** "Two protocols down in a small
country" reads like news, not like a bug. It had a denominator, it had a
ratio, and it moved when the fleet moved. This is the same defect class as
ADR 107 and ADR 114: not a crash, but an absent value rendered as a real one.

## Decision

**Judge a group only on the sub-hosts that carry evidence for that protocol.**
`_group_state` builds one `measured` set and uses it for both the verdict and
the denominator, returning `None` when nothing in the group carries the key —
which surfaces as `N/A`, the value ADR 113 already established for "not
measured". Worst-wins is preserved over everything that *is* measured: a
sibling that carries the check and fails still sinks its group.

**Disclose the scoping.** Three columns imply the fleet has three protocols.
It does not: the columns are the three *configured keys*. Live, one fleet
carries several differently-named variants of a single protocol, all
answering, while the configured key for that protocol exists on none of those
hosts — so that column describes a key, not a protocol. The table now says so,
and points at the tool that walks every check item. ADR 114 permits widening
or disclosing; widening changes what the columns mean and is deferred.

## Consequences

Countries whose pairs are asymmetrically provisioned stop reading as outages.
Cells with no matching item read `N/A` and are counted in a footnote, so
"nothing to measure" cannot be mistaken for "nothing wrong" in either
direction.

The deferred half is the real fix: while the columns track configured keys,
this tool answers "are the configured keys answering" and not "is the fleet
serving". The disclosure makes that limit visible instead of implied. Widening
to judged, discovered checks needs its own decision, because it changes the
column set from fixed to fleet-derived.

Five tests now pin the tool, and the first was confirmed to fail against the
old scoring — reproducing the exact live cell, `DOWN (0/1)` where the truth is
`OK (1/1)` — before being accepted. One of them pins the defective pairing
directly, so restoring `all`-over-all with `any`-over-any fails.

The general rule, now stated three times in three weeks in this repo: **when a
numerator and a denominator are computed over different sets, the difference
between those sets is where the lie lives.**
