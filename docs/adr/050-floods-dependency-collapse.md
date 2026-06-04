# ADR 050: Dependency collapse in `get_host_floods`

**Status:** Accepted
**Date:** 2026-06-04

## Problem

ADR 048 added trigger dependency collapse to `get_active_problems` but
deferred `get_host_floods` — the other tool the ticket (tasks.md #144)
named — because its per-host flood count interacts with the
`min_problems` threshold and that interaction wanted its own
consideration.

The interaction, examined: a host inside a dependency cascade fires the
root trigger plus its declared symptom triggers. `get_host_floods`
counts active problems per host, so the symptoms inflate that host's
count and can push it over `min_problems`, flagging a "flood" that is
really one root failure plus its known consequences.

## Decision

Collapse dependent problems **before** the per-host count, reusing the
`collapse_dependent_problems` helper from ADR 048.

The threshold interaction is exactly what we want: after collapse, a
host with 5 problems that are 1 root + 4 declared symptoms counts as 1
real problem and no longer trips the flood. A genuine flood — many
*independent* problems — is unaffected, because independent problems
have no dependency edges to collapse.

`get_host_floods` gains `collapse_dependent: bool = True`, adds
`objectid` to its `problem.get` output, fetches `trigger.get` with
`selectDependencies` for the firing triggers, and runs the collapse
right after the suppress filter (ADR 044) and before records are built
for counting.

As with ADR 048, this is a no-op where no trigger dependencies are
configured (the monitored instance currently has none) — zero behaviour
change today.

## Test approach

No new tests: `collapse_dependent_problems` is already covered by
`test_analytics.py::TestCollapseDependentProblems` (6 cases, including a
multi-link chain). The wiring here is configuration-level over that
tested helper, and the suppress-filter / parent-map paths it sits among
are themselves covered. The async wrapper is covered by the
registration / smoke tests.

## Consequences

- Tool count unchanged (161).
- Test count unchanged (552).
- `WRITE_TOOLS` unchanged. No new env vars.
- One extra `trigger.get` per `get_host_floods` call (scoped to the
  firing trigger ids), only when `collapse_dependent` is on.
- API-compat: a new optional arg with a no-op default while no
  dependencies exist.

## Not included

- **Annotating the collapsed count in the flood output.**
  `get_active_problems` notes how many symptoms it collapsed in its
  header; `get_host_floods` does not surface the per-host collapsed
  count. The flood list is correct (collapsed before counting); adding
  a per-host "(N symptoms collapsed)" annotation is a presentation
  nicety for later.
- **Collapse in the other problem-consuming tools** (`get_problems`,
  `get_outage_clusters`). `get_problems` is a raw query — collapsing
  there would hide rows a caller asked for. `get_outage_clusters`
  clusters across hosts, where the dependency graph is per-host; the
  value is lower. Both deferred unless a concrete need appears.
