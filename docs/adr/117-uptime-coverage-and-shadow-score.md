# ADR 117 — Coverage disclosure and a shadow score that can see a dead protocol

**Status**: Accepted (2026-08-17)
**Affected**: `src/zbbx_mcp/uptime.py` (`low_coverage_hosts`,
`protocol_score`, `LOW_COVERAGE_HOURS`), `src/zbbx_mcp/tools/geo_health.py`
(`get_service_uptime_report`), `tests/test_uptime.py`.
**Closes**: task 178(a) and task 185. **Records a decision on**: task 178(b).

## Context

Two of the reporting side's SLA fixes were queued for this server. Reading the
tool closely changed what each of them should be here.

### 178(a) — coverage is disclosed from the wrong extremum

`coverage_note()` computes coverage from the **earliest sample anywhere in the
scope**. One long-lived host therefore makes the note report the most
optimistic possible coverage for everyone, and a host whose own window is a
day says nothing at all. This is the same fleet-extremum mistake ADR 113 found
in the health matrix, in different clothing: there it was `max(window)`, here
it is `min(clock)`, and both pick the single most flattering host.

### 178(b) — carry-forward does not port, and should not be forced

On the reporting side, carry-forward exists to stop a rebuilt host inflating a
**traffic-weighted fleet mean** by roughly half the error budget. This tool has
no such number: it reports per-host rows, and its only aggregate is a
per-country **median**, which is precisely the statistic a single short-window
outlier cannot move.

Implementing it anyway would mean substituting an unmeasured value into a
number that does not need protecting, and taking a cross-repo dependency on the
sibling pipeline's committed snapshot to do it. That is fabrication with a
maintenance cost and no benefit. **Not implemented, deliberately** — recorded
here so the task is closed by a decision rather than left to be re-raised.

### 185 — the shadow score, and a flaw in its specification

`up if any protocol answers` pins nearly every host at exactly 100%, so the
figure can neither rank hosts nor show a protocol dying behind healthy
siblings. The port is a mean across the protocols a host runs.

The specification said *deployed = answered at least once in the window*. That
rule is wrong for this purpose, and the first synthetic case tried exposed it:
a host with three protocols and one **dead for the entire window** scores
**100%**, because the dead protocol is dropped from the mean instead of counted
as zero — the exact case the score exists to surface.

## Decision

- **Deployed means the protocol produced measured hours** (`total > 0`): the
  host has that check and it recorded data. A protocol the host does not run
  has no item, no trends, and is excluded, so nobody is punished for a
  protocol they were never meant to run. A measured protocol that never
  answered scores **zero**, which is the honest reading of a check on a live
  host returning nothing.
- Hosts whose own window is under `LOW_COVERAGE_HOURS` (48) are **named**, with
  the reason: trends belong to the item, so recreating a host's checks restarts
  its history.
- The score is reported **alongside** the existing figures and changes no
  verdict, no sort order and no threshold — shadow discipline, until the series
  proves out.

## Consequences

- A dead protocol behind healthy siblings now moves a number, where before it
  moved nothing anywhere. `detect_dead_protocols` (ADR 114) names it; this
  scores it.
- The summary line states how many hosts sit at 100% across every protocol they
  run, which is the measurement of whether `up-if-any` still discriminates.
- 178(b) stays closed-by-decision. If this tool ever grows a weighted
  aggregate, the argument changes and carry-forward should be revisited.

## Verification

1017 tests pass (+10). The load-bearing one is the three-protocol host with one
dead: it must score ~67%, and it returns 100% under the specification as
written.
