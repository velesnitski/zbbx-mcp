# ADR 127 — Example host names use documentation country codes

**Status**: Accepted (2026-08-20)
**Affected**: `tests/`, `docs/adr/022`, `CONTRIBUTING.md`, `REVIEW.md`,
docstrings in `tools/hosts.py`, `tools/predictive.py`, `tools/trends_compare.py`.

## Context

Fixtures and documentation need host names. Any convention for inventing them
has to satisfy two things at once: the names must exercise the real parsing
paths — country extraction, canonical parent/child folding, cluster grouping —
and they must be recognisable as examples at a glance, by a reader with no
context, without anyone having to check them against anything.

A numbering rule alone cannot do the second job. Example names and operational
names occupy the same shape space, so "how many digits" or "which band" is a
convention a reader has to already know before the name means anything.

## Decision

**Country code carries the signal.** Examples use ISO codes for uninhabited or
near-uninhabited territories — `AQ`, `BV`, `HM`, `GS`, `TF`, `UM`, `PN`, `SJ`,
`CC`. These are real ISO 3166-1 codes, so `extract_country` resolves them and
every parsing path is exercised normally, but no infrastructure can be sited
in them. The name announces itself as an example without a convention to look
up, the way `example.com` does.

**Numbering sits in a reserved band**: 9000 and above, four digits.

**Real codes only where the test is about country parsing itself** — alias
normalisation, indexed-versus-segment precedence — where a documentation code
would make the test vacuous. Those keep the reserved numbering band.

## Consequences

`tests/test_canonical_folds.py`, `test_geo_country.py`, `test_utils.py`,
`test_classify_products.py`, `test_sentry_scrub.py`, `test_uptime.py`,
`test_check_flaps.py`, `test_traffic_erosion.py` and
`test_health_matrix_scoping.py` follow this, as do the prose examples in
`CONTRIBUTING.md`, `REVIEW.md` and ADR 022. `REVIEW.md` states the convention
so it is discoverable at review time rather than by inspection.

One consequence is worth stating because it is easy to misread as an
oversight: the uninhabited codes have no entry in `CAPITAL_COORDS`, so any
tooling keyed to that table does not treat these names as country-bearing at
all, while `extract_country` — which resolves against the wider ISO2 set —
does. Anything reasoning about example names needs to know which of the two
sets it is standing on.
