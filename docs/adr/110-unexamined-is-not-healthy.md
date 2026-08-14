# ADR 110 — "8 skipped" is a number nobody acts on

**Status**: Accepted (2026-08-14)
**Affected**: `src/zbbx_mcp/tools/traffic.py` (`detect_traffic_drops`),
`tests/test_traffic_disruption.py`.
**Extends**: ADR 103, ADR 107, ADR 109.

## Context

A host collapsed ~98% — hourly peaks of 70–177 Mbps until a cliff, then
1–20 Mbps sustained for over a day — and `detect_traffic_drops` returned
**"No blocks detected"**. Run live against its group after the fact: 25 servers
analyzed, 17 healthy/diurnal, 8 skipped. The collapsed host was in neither the
findings nor any named list.

Two separate reasons, both ending in silence:

1. **It was invisible to discovery.** Its items come from Zabbix's stock Linux
   template, and this tool searched by item *name* until ADR 109. Fixed there.
2. **The hosts it *does* skip are only counted.** `no_history` and
   `no_baseline_window` — a host whose items were recreated, so there is no
   baseline to compare against — collapse into *"8 skipped for insufficient/low
   baseline"*. Two of the three hosts in that incident landed here.

The second is the one that matters, because it survives the first being fixed.
A count reads as reassurance and names nobody, so a host that could not be
looked at is indistinguishable from a host that was checked and found fine.

## Decision

Hosts skipped for **absence of data** are named:

> _2 host(s) had no usable trend data and were NOT examined: node-a, node-b.
> Absent from this result means unmeasured, not healthy._

- Only the two "no data" reasons are collected. `below_floor` and `healthy` are
  real verdicts reached by looking, not blind spots, and sweeping them in would
  bury the signal in noise — the failure this fixes.
- The note is appended to **both** exits, the empty one and the populated one.
  A run that found two blocks and could not examine a third must say so, or the
  disclosure only appears when nothing else does.
- Bounded to five names plus a count, like every other list in this tool.

## Consequences

- `detect_traffic_drops` now answers two different questions distinctly: "is
  anything dropping" and "is there anything I could not check".
- Third tool to get this treatment (`get_host` in ADR 103, shaping in ADR 107).
  The recurring shape is that absence has to be rendered, never inferred from
  an empty result — and each time it has been found in production rather than
  by a test, because the broken version looks like the healthy one.

## Verification

945 tests pass (+2): a host with no trend history is named and the wording says
unmeasured-not-healthy; a fully measured fleet adds nothing. Confirmed absent
from the previous commit.
