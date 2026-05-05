# ADR 019: Problem-age histogram, stale-item cascade collapse, disruption-rollup won't-fix

**Status:** Accepted
**Date:** 2026-05-05

## Problem

A reports-side review surfaced three follow-on items. Two are pure
MCP primitives that map cleanly to existing tool patterns; one is
report-domain logic that doesn't belong in MCP.

1. **Active-problem age distribution is invisible in current tools.**
   The ops report buckets problems into "< 24h" and "≥ 7d" only.
   Anything aged 1–7 days disappears in both the KPI cards and the
   stale-counter — even though that band is the most actionable
   class (real, persistent, with time to investigate).
2. **`get_stale_items` reports symptoms, not root causes.** When a
   master item goes unsupported, every dependent item that derives
   from it also reports stale, so the output reads as N independent
   failures when it's really one root cause with N children.
3. **`get_disruption_rollup`** would compose four existing detectors
   into one event list with severity tiers and `recommended_action`
   strings. The composition is useful in the report layer; the
   marketing-action field couples MCP to a business workflow that
   doesn't fit other consumers (Grafana, status pages, Slack
   alerters) and the four detectors are already individually
   callable.

## Decision

### #132 — `get_problem_age_buckets`

New tool in `tools/health.py`. Pulls ``problem.get`` for active
problems at or above ``min_severity`` and emits a per-severity
histogram over the buckets ``<1d``, ``1-3d``, ``3-7d``, ``7d+``.
Output is a markdown table with severity rows, four bucket columns,
plus a Total row and a one-line "1-7d band" callout when that band
is non-empty.

Pure helper ``_bucket_problems_by_age(problems, now)`` does the
classification: returns ``{severity: {bucket: count}}`` with every
bucket key always present (zero when empty) so consumers can render
fixed columns. Bucket boundaries use strict less-than (`< 1d` is
`age < 86400` exactly), which is what the report layer already
assumes.

### #133 — `get_stale_items` cascade-aware mode

Existing `get_stale_items` gains one new arg
``collapse_dependencies: bool = False`` (default preserves current
behaviour). When set:

1. The ``item.get`` call adds ``master_itemid`` to its output list.
2. After the stale list is built, ``_collapse_dependent_chain``
   walks each stale record's ``master_itemid`` chain. If the master
   is also stale, the child is suppressed and the master's
   ``affected_count`` increments. Two-hop chains (grandchild →
   parent → root) collapse correctly to the root.
3. The visible-output table gains an "Affected" column showing
   `+N` for collapsed roots or `—` for items with no dependents.
4. The header reports `(N downstream collapsed)` when the collapse
   actually fired.

**Limitation noted in the docstring:** this handles dependent items
only (Zabbix item type 18, the ``master_itemid`` mechanism).
*Calculated* items that reference other items via formula text are
not yet collapsed because parsing arbitrary formula expressions is
fragile (template-inherited refs, multi-item formulas, circular
formula chains). Calculated-item collapse is a separate follow-up
if the demand materialises.

The pure helper is robust to circular references — a
``seen`` set in the walk terminates after one revisit.

### #131 — `get_disruption_rollup` won't-fix

Marked WONTFIX in `tasks.md` with the reasoning recorded inline:

- The four underlying detectors (`detect_disruption_wave`,
  `get_outage_clusters`, `detect_regional_traffic_loss`,
  `detect_loss_drift`) are already individually callable as MCP
  tools. Composition is cheap on the consumer side.
- The proposed `recommended_action` field is a marketing-action
  mapping that belongs in the report layer where business policy
  lives. Embedding it in MCP couples the protocol to one consumer's
  workflow.
- Reports converging on one shared module is a reports-side
  refactor (a single `compose_disruption_events` helper inside
  `zabbix-reports`), not an MCP-side feature.

The server correlate stays where it already lives, in
`vpn_brief.py` (zabbix-reports). MCP stays primitive.

## Test approach

13 new tests in ``test_analytics.py``:

- 6 for ``_bucket_problems_by_age`` covering empty input, bucket
  boundaries (the strict `< 1d` boundary at exactly 86400s), three
  buckets occupied, 7d+ overflow, severity partitioning, and bad
  ``clock`` input handling.
- 7 for ``_collapse_dependent_chain`` covering pass-through (no
  master), single-hop child collapse, master-not-in-stale (child
  becomes its own root), two-hop chain to root, multiple children
  sharing a root, circular ref termination, and input-not-mutated
  invariant.

The async tool wrappers do only Zabbix I/O and rendering and are
covered by the existing registration / smoke tests
(``test_server.py`` is bumped 154 → 155 tools).

## Consequences

- 353 tests pass (340 pre-change + 13 new helper tests).
- Tool count goes from 154 to 155 (`get_problem_age_buckets` added).
- `get_stale_items` gains one optional argument with a default that
  preserves current behaviour — no caller-visible breakage.
- `get_problem_age_buckets` lands in the `core` tier preset because
  it's a basic-querying primitive that any consumer benefits from.
- `WRITE_TOOLS` unchanged.
- Compact-mode handshake at full tier moves from ~25k to ~25k tokens
  (one new tool, but offset by the older trim discipline so the
  proportional cost is negligible).

## Not included

- Calculated-item dependency collapse (formulas referencing other
  items by key). The Zabbix formula model has enough edge cases
  (template inheritance, multi-item formulas, circular refs) that
  a robust implementation needs its own dedicated effort. Will
  revisit if `collapse_dependencies` users report calculated-item
  noise that the master-itemid walk doesn't catch.
- A `get_disruption_rollup` MCP tool. See WONTFIX rationale above.
- Per-tool age-bucket boundaries. The default `<1d / 1-3d / 3-7d /
  7d+` matches the report-side convention; an env var would be the
  cheapest knob if a deployment needs different.
