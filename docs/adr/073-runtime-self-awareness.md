# ADR 073: Runtime self-awareness — stale-build warning + token accounting

**Status:** Accepted
**Date:** 2026-07-03

## Problem

A token-effectiveness review found the server efficient (compact
responses under budget, 0.2% error rate, broad tool usage justifying the
full tier) but surfaced two things the server *knows* and never *says*:

1. **It serves a stale build silently.** The process imports
   `__version__` once at startup; after every release bump it keeps
   serving the old code until the MCP client reconnects. This session
   alone hit the "why isn't the fix live?" confusion repeatedly — v1.13.0
   ran while v1.14.0 was on `main`; the triage `-32602` fix was committed
   yet the live call still crashed. Each time, a human had to infer
   staleness from behaviour.
2. **Token cost needs manual math.** `get_telemetry_summary` reports
   per-tool average chars, so answering "are we token-effective" meant
   eyeballing averages and multiplying by counts.

## Decision

Two small additions, one theme — the server reports its own operational
state:

1. **Stale-build warning in `check_connection`** (the tool an operator
   reaches for first, per the ADR 057 precedent). Two pure helpers:
   `source_tree_version(package_file)` walks from `zbbx_mcp.__file__` to
   the checkout root and reads `pyproject.toml`'s version (returns "" for
   wheel installs — no false positives); `stale_build_warning(running,
   source)` emits "⚠ Running build vX, but the source tree is vY —
   reconnect /mcp to load the new build" only when both sides are known
   and differ (the `0.0.0+unknown` fallback is suppressed).
2. **Token accounting in `get_telemetry_summary`.** `_summarise_records`
   now exposes `response_chars_total` per tool, and a pure
   `_token_footer` renders `Σ responses: N chars ≈ M tokens
   (~K tokens/call, est. 4 chars/token)`. The estimate is deliberately
   rough — its job is trend and order-of-magnitude, not billing.

## Test approach

Ten new tests: stale-build pure cases (mismatch / match / unknown-side
suppression), `source_tree_version` against a fake checkout and a
wheel-like layout (tmp_path), a wire-level `check_connection` case with
monkeypatched versions asserting the warning renders alongside the
normal output, footer math / empty-suppression / totals-exposure for
telemetry, and a wire-level `get_telemetry_summary` run over a real temp
log. All via the shared `tests/wiretest.py` (ADR 072). 659 → 669.

## Consequences

- The next stale-build episode announces itself in the first
  `check_connection` instead of masquerading as a broken fix.
- "Are we token-effective" is one tool call.
- Tool count unchanged (163); no signatures changed.

## Not included

- **Auto-reload on staleness.** A stdio MCP server swapping its own code
  mid-session is fragile and surprising; a loud warning plus the
  operator's reconnect is the right amount of magic.
- **Exact tokenizer counts.** Pulling a tokenizer dependency for a
  monitoring hint is cost without benefit; 4 chars/token is stated in
  the output.
