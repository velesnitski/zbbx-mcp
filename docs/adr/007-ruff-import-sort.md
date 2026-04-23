# ADR 007: Enforce ruff import sort (I001) on CI

**Status:** Accepted
**Date:** 2026-04-23

## Problem

CI lint stage (`ruff check src tests`) failed on three files in PR #1
with `I001 Import block is un-sorted or un-formatted`. The offending
modules had a stray blank line *inside* the import block, which ruff
treats as an un-organized import section:

- `src/zbbx_mcp/tools/items.py`
- `src/zbbx_mcp/tools/problems.py`
- `src/zbbx_mcp/utils.py`

Local development did not catch the issue because contributors were
not running `ruff check` before pushing; the blank lines were
introduced incrementally by other edits and only became visible once
ruff was upgraded on the runner (0.15.11).

## Decision

Apply `ruff check --fix`, which removes the stray blank lines and
re-groups the import block per isort/ruff defaults (stdlib, third-party,
first-party, separated by a single blank line). No rule changes in
`pyproject.toml`; the fix is purely whitespace.

## Consequences

- CI lint passes; PR #1 unblocks.
- Full test suite (214 tests) still passes.
- No behaviour change — imports resolve identically.
- Future drift is caught on CI as long as the `Lint` job stays wired
  up in `.github/workflows/tests.yml`.
