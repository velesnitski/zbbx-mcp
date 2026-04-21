# ADR 005: `export_cost_audit` — XLSX dump of estimated costs for finance review

**Status:** Accepted
**Date:** 2026-04-21

## Problem

After a reconciliation session most hosts carry a `{$COST_MONTH}` value,
but a non-trivial subset were filled via bulk patterns or peer-median
estimates rather than exact billing matches. Accounting needs to see
exactly which rows are estimated so they can add the missing entries to
the source-of-truth spreadsheet on their next pass.

Without a dedicated export, operators either dump everything (too much
noise) or reconstruct the list by hand.

## Decision

Add `export_cost_audit`:

- Input: `output_xlsx` (path, defaults under `~/Downloads`), `mode`
  (`estimated` or `all`).
- Fetches all enabled hosts + their `{$COST_MONTH}` macros (including
  description).
- Classifies each row as **billing-backed** (description starts with
  `src:billing_` or `base ` — the cluster-extras audit tag) or
  **estimated** (anything else, including `src:bulk_pattern`,
  `src:product_median`, `src:provider_median`, ad-hoc manual notes, or
  empty description).
- `mode=estimated` (default) includes only the non-billing-backed rows —
  the subset accounting must review.
- `mode=all` emits every costed host with a `billing_backed` column.
- Writes a single-tab XLSX sorted by descending cost so the biggest
  estimates appear first.

## Consequences

- One command produces a paste-ready tab for the source-of-truth
  workbook, closing the audit loop introduced in ADR 004.
- The classifier intentionally treats missing or non-standard
  descriptions as "estimated" — erring toward more review rather than
  less. As the provenance tags backfill through normal operation, the
  `all` export will converge toward the same boundary.
- No write operations; safe under `read_only=True`.
- Tool count: 139 → 140.
