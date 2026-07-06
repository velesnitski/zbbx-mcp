# ADR 072: Architecture guards — turn recurring failure classes into tests

**Status:** Accepted
**Date:** 2026-07-03

## Problem

An architecture review of the repo (163 tools / 40+ modules) found the
core design sound — pure-helper + async-wrapper split, compat shims at
the client boundary (ADR 055), canonical registries with registration
tests — but two failure classes recurred this quarter and had **no
automated guard**, only ADR postmortems:

1. **Invalid API params reaching the wire.** `problem.get` +
   `selectHosts` shipped twice (ADR 068 in triage, ADR 070 in
   `get_recent_changes`), each 100% breaking a tool with -32602 on its
   first live call. Both ADRs' "Not included" deferred a guard.
2. **Hand-maintained doc counts drifting.** ADR 063 found the README
   claiming three different tool totals at once (badge 161 / tier table
   156 / prose 154, real 162) and deferred "a test that asserts the badge
   equals `len(ALL_TOOLS)`". The CLAUDE.md module table had also drifted:
   three rows attributed tools to the wrong files.

A third, smaller finding: the wire-contract test scaffolding
(recording client / capture MCP / stub resolver) had been copy-pasted
three times (`test_triage.py`, plus two classes in `test_analytics.py`)
— the rule of three met exactly.

## Decision

Three changes, one theme — make the invariants the ADRs already argued
for *executable*:

1. **`tests/test_guards.py::TestApiContractGuard`** — AST-scans every
   `client.call("<method>", {…literal…})` in `src/` against a deny-map of
   params Zabbix rejects (`problem.get` × `selectHosts` /
   `selectGroups` / `selectHostGroups`). Extensible per class discovered.
   Best-effort by design (dynamically-built params dicts are invisible),
   but every call site in this codebase — including both shipped bugs —
   uses inline dict literals. A vacuity check asserts the scanner
   actually sees ≥5 `problem.get` sites.
2. **`tests/test_guards.py::TestDocCountGuard`** — pins the README badge,
   headline, and all five tier-table rows, plus the CLAUDE.md header, to
   `len(ALL_TOOLS)` / `resolve_tier_disabled(...)`. A new tool that
   misses a doc site now fails the suite instead of silently aging the
   docs (ADR 063's root cause).
3. **`tests/wiretest.py`** — the shared scaffolding (`RecordingClient`
   with per-method canned responses, `CaptureMCP` keyed by function name,
   `StubResolver`, `run_tool`). The three private copies are refactored
   onto it; behaviour-identical, test count unchanged.

Also fixed the three factually wrong CLAUDE.md module rows
(`trends_health`, `trends_compare`, `availability` — verified against the
actual `not in skip` gates) and extended the "Adding a new tool"
checklist with the doc-count and wire-contract steps.

## What was reviewed and deliberately NOT changed

- **`data.py` as a 532-line shared hub** (imported by 38 files). It is a
  wide but shallow utility module with tested pure helpers; splitting it
  would churn 38 import sites for aesthetics. Declined.
- **`tools/__init__.py`'s explicit `ALL_TOOLS` list** alongside imports
  and the modules list. The redundancy is load-bearing: the registration
  test cross-checks the three, which is how drift gets caught. Deriving
  the list dynamically would remove the cross-check. Declined.
- **`test_analytics.py` at ~4k lines.** A split by domain would be pure
  file-motion churn with rename-noise in blame; pytest `-k` already
  scopes runs. Declined until it actually hurts.
- **Large tool modules** (`executive.py` ~1050 loc). Size tracks tool
  count, not complexity; each tool remains a thin wrapper over tested
  helpers. Declined.

## Test approach

The guards are themselves tests (5 new). The wiretest refactor is covered
by the existing 10 wire-contract tests continuing to pass unchanged.
654 → 659.

## Consequences

- The next `problem.get select*` mistake or forgotten doc count fails CI
  in milliseconds instead of crashing a tool live or shipping a lying
  README.
- New wire tests cost ~10 lines (import + canned responses) instead of
  ~40 of scaffolding.
- Tool count unchanged (163). No runtime code changed at all — this
  release is tests + docs only.

## Not included

- **Guarding dynamically-built params dicts** (e.g. `params` assembled
  across statements). Would need dataflow analysis; no current call site
  needs it.
- **Auto-generating the CLAUDE.md module table.** The count guard covers
  totals; regenerating per-module rows is tooling for a file only humans
  read. Revisit if the table keeps rotting.
