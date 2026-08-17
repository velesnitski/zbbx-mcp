# ADR 116 — The ratio gets a verdict instead of a caveat

**Status**: Accepted (2026-08-17)
**Affected**: `src/zbbx_mcp/tools/diagnose.py` (`_collect_diagnosis_inner`,
`_render_full_report`, `_SEASONAL_DAYS`), `tests/test_diagnose.py`.
**Closes**: task 186. **Completes**: ADR 113.

## Context

ADR 113 disclosed a bias without removing it. `diagnose_host` measures the
recent window against the 24 hours immediately preceding it, which sits in a
different part of the daily cycle — so a healthy host reads 50–70% "of
baseline" outside peak hours, and that reads like a fault.

The caveat helped, but the underlying problem stood: `diagnose_host` and
`detect_traffic_drops` gave **different answers about the same host**, and the
one an operator reaches for first in an incident was the one without a seasonal
comparison. A caveat asking the reader to go run another tool is not a fix.

## Decision

`diagnose_host` now computes the same-hour-of-day floor that
`detect_traffic_drops` judges against, using the existing `seasonal_floor`
helper over a 7-day trend window, and states a verdict:

- recent **≥** floor → *"WITHIN the normal band for this time of day, so the
  ratio above is diurnal, not a fault"*
- recent **<** floor → *"N% BELOW the normal band for this time of day. This is
  anomalous, not diurnal."*

### The cost, which the task asked to measure first

It is one extra `trend.get` over 7 days. For a single host that is nothing. For
`bulk_diagnose` it multiplies by the fan-out, which is capped at 50 hosts with
a concurrency of 10 — so it would add up to 50 seven-day trend reads to a
command that currently issues far fewer.

So it is a parameter, defaulted **off**, and only the single-host path opts in.
Bulk keeps its cheap ratio and keeps the ADR 113 caveat that goes with it. Both
halves are pinned by test — a default flipped by accident would quietly
multiply the cost of every fan-out.

A failed or empty seasonal fetch leaves the floor `None` and falls back to the
caveat, naming which case applies. It enriches a report; it must never break
one.

## Consequences

- The two tools now judge a host against the same shape, so they can agree.
- The caveat survives exactly where it is still true: no seasonal band, or a
  bulk row.
- `seasonal_floor` already widens its bucket to the target hour ±1 with a
  minimum sample count, so a host with under a week of history simply gets no
  floor rather than one built from two points.

### A test method that was wrong

The ADR 113 assertions grepped the module source for its own wording. A lint
pass rewrapped one of those strings and the test failed while the behaviour was
correct — and worse, Python's adjacent-literal concatenation meant no amount of
whitespace normalisation could fix it. They are now written against **rendered
output** through the real renderer, which is what they were always trying to
check.

## Verification

1007 tests pass (+5, and 4 rewritten). Both verdict directions, the caveat
appearing only when there is no band, the caveat *not* appearing on a healthy
ratio, and the bulk default staying off.
