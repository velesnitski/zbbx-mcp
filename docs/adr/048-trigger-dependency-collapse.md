# ADR 048: Trigger dependency collapse — root-cause-only mode

**Status:** Accepted
**Date:** 2026-06-04

## Problem

Zabbix 6.x lets an admin declare that trigger A *depends on* trigger B:
when B is firing, A's alert is a known consequence, not an independent
incident. The classic case — a service check that depends on "agent
unreachable": when the agent goes down, the agent-down trigger and every
service-check trigger on that host fire together. The service-check
problems are symptomatic noise; the agent-down is the root cause ops
should act on.

`get_active_problems` returned all of them undistinguished, so a single
root failure could present as a wall of symptom rows.

## Decision

Collapse dependent problems by default, surfacing only root causes.

### Pure helper

`data.collapse_dependent_problems(problems, dep_map, collapse=True)`:

- `problems` each carry `objectid` (the firing trigger id);
- `dep_map` maps a trigger id to the set of trigger ids it depends on;
- a problem is dropped when any of its dependencies is itself in the
  active set (the trigger ids present in `problems`);
- returns `(kept, collapsed_count)`.

A chain (C depends on B depends on A, all firing) collapses C and B,
leaving A — each link is dropped because its immediate dependency is
active.

### Wiring

`get_active_problems` gains `collapse_dependent: bool = True`. When set,
it fetches `trigger.get` with `selectDependencies` for the firing
triggers, builds the `dep_map`, and runs the collapse before the
host-merge and rendering. The header notes how many symptoms were
collapsed.

### Default on, but a no-op today

The monitored instance has no trigger dependencies configured, so
`dep_map` is empty and nothing is collapsed — zero behaviour change
today. The value is realised the moment dependencies are wired: cascade
noise drops out of the active-problem view, and a power user can pass
`collapse_dependent=False` for the full unfiltered set.

## Test approach

6 unit tests in `test_analytics.py::TestCollapseDependentProblems`:

- symptom dropped when its root is also firing;
- symptom kept when its dependency is not in the active set;
- no dependencies → no-op;
- `collapse=False` → no-op;
- a three-link chain collapses to the single root;
- a problem with no `objectid` is kept (no dependency to evaluate).

The async wrapper is configuration-level over the tested helper.

## Consequences

- Tool count unchanged (161).
- Test count 542 → 548.
- `WRITE_TOOLS` unchanged. No new env vars.
- One extra `trigger.get` per `get_active_problems` call (scoped to the
  firing trigger ids).
- API-compat: a new optional arg with a no-op default while no
  dependencies exist.

## Not included

- **`get_host_floods`.** The other tool named in tasks.md #144. Its
  flood count is per-host and interacts with the `min_problems`
  threshold — collapsing dependents there changes whether a host
  qualifies as a flood, which deserves its own consideration. Deferred;
  the same helper applies when done (add `objectid` to its `problem.get`
  output and run the collapse before the per-host count).
- **Annotating instead of dropping.** The collapsed symptoms are removed
  rather than shown greyed-out under their root. A "show dependents
  nested" mode is a presentation feature for later; the count in the
  header tells the operator how many were hidden.
- **Cross-host dependency cascades** beyond what the trigger-dependency
  graph encodes. This collapses exactly the dependencies Zabbix admins
  declared; inferring undeclared cascades is the job of the
  outage-cluster / flood detectors.
