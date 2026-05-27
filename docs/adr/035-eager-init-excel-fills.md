# ADR 035: Eager-init Excel fill constants

**Status:** Accepted
**Date:** 2026-05-27

## Problem

`excel.py` was loading openpyxl lazily — the module-level
constants (`HEADER_FILL`, `RED_FILL`, `ORANGE_FILL`, `GREEN_FILL`,
`LIGHT_GREEN_FILL`, `DARK_RED_FILL`, `THIN_BORDER`, `HEADER_FONT`,
`BOLD_FONT`, `DARK_RED_FONT`) were declared as `None` at module
import time, and a helper `_init_openpyxl()` rebound them to real
openpyxl objects on first call. The docstring claimed this saved
~15 MB of RAM for servers that never generate Excel.

The pattern was broken by design. `full_report.py` imported the
constants at its own module level:

```python
from zbbx_mcp.excel import HEADER_FILL, RED_FILL, GREEN_FILL, ORANGE_FILL
```

`from … import …` captures the *current binding* — at that point,
`None`. When `_init_openpyxl()` later ran (triggered by another
helper inside `excel.py`), it rebound `excel.HEADER_FILL` but the
binding inside `full_report` stayed at `None`. Every call to
`generate_full_report` therefore assigned `None` to cell `.fill`
attributes; openpyxl deferred validation until serialisation, then
fired `TypeError: expected <class 'openpyxl.styles.fills.Fill'>`
during `wb.save()`.

Surfaced in production via Sentry issue `dc717f4d`.

The four other Excel-using tools (`dashboard_report.py`,
`infra_report.py`, `report.py`, `costs_audit.py`) import openpyxl
*inside* the function body, so they always saw freshly-constructed
fills and never hit the bug.

## Decision

Drop the lazy-init pattern. Import openpyxl at the top of
`excel.py`; construct the fill / font / border constants
eagerly at module load time.

The "saves 15 MB" claim was unverified and incidental: openpyxl
is a declared hard dependency in `pyproject.toml`, so a server
that loads any MCP tool already imports the package transitively
via other paths (the four other Excel tools, `report.py` and
friends). The optimisation was illusory.

Eager construction also makes the type contract clear: at import
time, `HEADER_FILL` is a `PatternFill` — no rebind dance, no
broken module-level imports anywhere else.

## Test approach

3 new regression tests in `test_analytics.py::TestExcelFills`:

1. **`test_fills_are_pattern_fill_instances`** — directly asserts
   every module-level fill in `excel.py` is a `PatternFill`,
   not `None`.
2. **`test_workbook_with_module_fills_saves`** — exercises the
   full failure path: builds a workbook, assigns each module-level
   fill to a cell, saves to a `BytesIO`, asserts the bytes are
   non-empty. This reproduces the Sentry crash and would have
   caught it pre-fix.
3. **`test_full_report_module_level_imports_resolve_to_fills`**
   — reads `full_report.RED_FILL` / `GREEN_FILL` / `ORANGE_FILL`
   through the `full_report` module's binding (the exact
   broken-by-design code path) and asserts each is a
   `PatternFill`. This is the specific regression sentinel —
   any future attempt to re-introduce lazy init in `excel.py`
   would trip this test.

The async tool wrapper (`generate_full_report`) is covered by
the existing registration / smoke tests; the regression tests
above cover the bug directly without needing a live Zabbix.

## Consequences

- Tool count unchanged (161).
- Test count 476 → 479.
- `WRITE_TOOLS` unchanged.
- No new env vars.
- `generate_full_report` works again. Other Excel tools were
  unaffected by both the bug and this fix.
- Memory impact: openpyxl's style classes are tiny (a handful of
  `PatternFill` / `Font` / `Border` instances). The "15 MB saving"
  claim was about deferring the openpyxl *package* import, which
  is moot — the package is loaded the moment any Excel tool runs,
  and most server lifetimes will generate at least one report.

## Not included

- **Refactoring the four function-local openpyxl imports** in
  `dashboard_report.py`, `infra_report.py`, `report.py`, and
  `costs_audit.py`. They work today. Moving them to module-level
  is a stylistic cleanup, not a bug fix; leave for a future
  consistency pass.
- **A pre-commit lint rule banning lazy-init globals.** The
  pattern is rare enough that the regression test above is
  sufficient. Codifying a generic rule would be over-engineering.
- **Dropping `_init_openpyxl()` from cached `.pyc` files.** The
  removed function is gone from source; stale `.pyc` files would
  be rebuilt on next import. No special cleanup needed.
