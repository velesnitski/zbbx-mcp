# ADR 074: File-length budgets — split the sink, keep the structured big files

**Status:** Accepted
**Date:** 2026-07-07

## Problem

"Should very long files be prevented, or are they fine for AI?" The
honest answer is: **length is not the variable that matters — structure
is.** Working evidence from this repo:

- Large *structured* modules (`executive.py` ~1,050 lines — one
  independent `if "tool" not in skip:` block per tool) cost little:
  agents navigate by gate/grep and read ~80-line spans; humans review one
  tool at a time.
- `tests/test_analytics.py` was the opposite: an **accumulation sink** —
  4,104 lines, **67 classes across ~10 unrelated domains** (geo/country,
  classification, correlation, traffic, diagnose, problems/ack,
  telemetry, canonical folds…), with a name that stopped being true
  years of features ago. Its cost was measured, not theoretical: this
  quarter alone it caused repeated edit-anchor collisions ("found 2
  matches"), constant grep-for-line-number pre-reads, full re-read cycles
  after every linter touch — and, worst, every new feature's tests
  defaulted into it *because* it was the biggest file, compounding the
  problem (6 more classes appended this quarter).

## Decision

Two halves — cure and prevention:

1. **Split `test_analytics.py` by domain** into 9 files
   (`test_geo_country`, `test_classify_products`,
   `test_correlation_floods`, `test_traffic_disruption`,
   `test_risk_recovery`, `test_diagnose`, `test_problems_ack`,
   `test_canonical_folds`, `test_health_telemetry`), 277–742 lines each.
   Done mechanically with an AST script that moves classes whole in
   original order (shared fixtures like `_ProblemOnlyClient` travel with
   their users) and fails loudly on any unmapped statement. The
   verification invariant: **collected test count identical before and
   after (669 → 669), all green** — pure file motion, zero behaviour
   change. `ruff --fix` pruned the duplicated import headers.
2. **`TestFileLengthGuard`** in `tests/test_guards.py`: src ≤ 1,100
   lines, tests ≤ 1,000 lines, **no grandfathered exceptions** — after
   the split, every file in the repo fits, so the invariant starts
   clean. Budgets are deliberately generous: the point is to force
   "start a new domain module" over "append to the biggest file", not to
   trigger refactor churn on well-structured modules.

## Why not split the big tool modules too

Reviewed and declined (consistent with ADR 072): their size tracks tool
count, each tool is a self-contained gated block over tested helpers,
and they sit *under* the budget. Splitting them would churn blame and
imports for no navigational gain. The guard caps them from ever becoming
sinks anyway.

## Test approach

The invariant above (identical collect count, 669 green) plus the guard
itself (2 new tests). 669 → 671.

## Consequences

- Domain-named test files: "where are the diagnose tests" is now a
  filename, not a grep across 4k lines; edit anchors are file-scoped.
- The sink pattern is structurally dead — the next 1,001-line test file
  fails CI with instructions.
- `test_analytics.py` no longer exists; new tests go to the domain file
  (or a new one).

## Not included

- **Splitting source modules under budget.** No evidence of pain; the
  guard holds the line.
- **Per-class size limits or complexity metrics.** Line budget is crude
  but sufficient for the observed failure mode; heavier lint machinery
  is cost without a demonstrated class of bug.
