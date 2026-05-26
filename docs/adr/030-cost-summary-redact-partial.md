# ADR 030: `redact_partial` flag on `get_cost_summary`

**Status:** Accepted
**Date:** 2026-05-26

## Problem

`get_cost_summary` reports `Servers with cost: N | Without: M`
plus a full per-product and per-provider breakdown. Internally that's
the right shape: it answers "what's the monthly spend?" and "where
are the gaps?" in one view.

The same response becomes a small political problem when it leaves
the building. Externally-shared artifacts (board decks, partner
readouts, vendor documentation) treat "M servers missing cost
attribution (Z%)" as a finding about process maturity — not the
metric the audience wants to discuss. In practice a downstream
consumer has to manually strip the partial-coverage lines before
forwarding the summary. Manual scrubbing is easy to forget; the
"Easy to miss" failure mode is what tasks.md #150 calls out.

## Decision

Add a single boolean opt-in to the existing tool.

### `get_cost_summary(redact_partial: bool = False, instance: str = "")`

When `redact_partial=False` (default), behaviour is unchanged.

When `redact_partial=True`:

1. Drop per-product rows where `priced_count < total_count` for
   that `(product, tier)` key.
2. Drop per-provider rows where `priced_count < total_count` for
   that provider.
3. Recompute the grand total from the kept product rows only
   (per-provider filtering is independent — it's a separate view of
   the same fleet).
4. Suppress the `Servers with cost: N | Without: M` line.
5. Append a footer: `*Filtered to fully-attributed lines.*` so any
   reader can tell the output was redacted (and re-run without the
   flag to see the full picture).

Defensive default: if a key appears in `prod_costs` but not in
`prod_totals` (host-classification drift between the priced-pass and
the totals-pass), the row is kept rather than silently dropped. The
totals map is best-effort context, not a strict gate.

### Why a flag, not a separate tool

Considered `get_cost_summary_external()` as a parallel tool. Rejected
because it doubles the surface for one toggle; the renderer is the
same shape with one filter. A flag keeps `WRITE_TOOLS`, the tier
catalog, and downstream tests untouched.

### Why this is the renderer's responsibility

Considered redacting at the caller (have the consumer post-process the
string output). Rejected because:

- Substring scrubbing is fragile — easy to miss when the summary
  format changes.
- Recomputing the grand total from a markdown string is awkward; the
  tool already has structured `prod_costs` / `prov_costs` in hand.
- Centralising the rule means there's one place to audit "what does
  the public version of this report omit?"

## Test approach

8 new pure-helper tests in `test_analytics.py` covering
`_render_cost_summary`:

- `redact_partial=False` preserves the full output (counts and
  totals).
- `redact_partial=True` drops the partial product row.
- `redact_partial=True` drops the partial provider row.
- The grand total is recomputed from kept product rows.
- The "Servers with cost / Without" line is suppressed.
- The footer is appended.
- All-partial input yields zero total + footer (sanity check that
  redaction doesn't crash on empty kept set).
- Key absent from `prod_totals` → defensive keep, not drop.

The async tool wrapper is configuration-level (assembles dicts and
calls the renderer) — exercised end-to-end by anyone who passes the
flag.

## Consequences

- Tool count unchanged at 161.
- Test count 448 → 456.
- `WRITE_TOOLS` unchanged.
- No new env vars.
- API-compat: existing callers see no behaviour change; the new
  param defaults to a no-op.
- Renderer factored into a pure helper — future cost-summary tweaks
  (currency formatting, column order, etc.) land in one place
  without async-mocking overhead.

## Not included

- **Per-row redaction granularity** (e.g. "keep the row but mask the
  count"). The all-or-nothing rule is easier to reason about and
  matches the "the partial number is itself the political problem"
  framing.
- **`redact_partial` on `get_cost_gaps` / `get_cost_efficiency`.**
  `get_cost_gaps` exists *precisely* to surface the partials — it
  wouldn't make sense to redact them. `get_cost_efficiency` reports
  $/Gbps per priced host; partial-coverage caveats are less load-
  bearing in that view. Revisit per concrete need.
- **An auto-toggle based on output destination** (e.g. detect that
  Slack is the consumer). Out of scope: the tool can't see who's
  rendering it. Opt-in by the caller keeps the contract explicit.
- **A confirmation prompt when the redacted total deviates >X% from
  the full total.** Tempting (the gap between redacted and full
  totals is itself information), but layering UX rules onto a pure
  data tool muddles its semantics. Operators inspecting a redacted
  output can re-run without the flag to see the delta.
