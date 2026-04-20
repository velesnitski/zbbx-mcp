# ADR 001: `only_if_empty` flag for cost import tools

**Status:** Accepted
**Date:** 2026-04-20

## Problem

`import_costs_by_ip` and `import_server_costs` always write `{$COST_MONTH}`
for every matched host. When a finance reconciliation sources costs from
billing but some hosts already carry finalized values (e.g. augmented via
`import_cluster_ip_fees`), a full re-import overwrites those values and
erases manual adjustments.

Operators wanted a safe way to fill in only the hosts that currently have
no cost, without touching anything already set.

## Decision

Both tools now accept an `only_if_empty: bool = False` parameter.

When `only_if_empty=True`:

- `import_costs_by_ip` fetches existing `{$COST_MONTH}` macros for the set
  of matched host IDs and removes hosts whose current value is non-empty
  and not zero (`"0"`, `"0.0"`, `"0.00"`). Skipped hosts are reported in
  the dry-run summary as `Skipped (already costed): N`.
- `import_server_costs` performs the same check inline during the apply
  loop: if a host already has a non-empty, non-zero value, it is counted
  as `skipped` instead of `updated`.

Default behavior is unchanged.

## Consequences

- Imports can be rerun without clobbering augmented or manually-set costs.
- Dry-runs surface how many hosts would be left alone, making it easier
  to estimate the remaining gap.
- No schema or storage change; new flag is additive.
