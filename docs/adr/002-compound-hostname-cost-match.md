# ADR 002: Compound hostname cost matching

**Status:** Accepted
**Date:** 2026-04-20

## Problem

Some Zabbix hosts monitor several physical servers as one entity. Their
hostname encodes the cluster via a space-separated compound form, e.g.
`base-cc1 cc3 cc5` — a prefix followed by sibling short codes.

The billing source lists each physical server individually under its full
name (`base-cc1`, `base-cc3`, `base-cc5`). The existing name matcher in
`import_costs_by_ip` only does exact / dash-split / prefix matches and
cannot reconstruct the siblings from the compound form. Result: these
hosts were left with empty `{$COST_MONTH}` and ~$N/mo was stranded.

## Decision

Add a pre-pass to `import_costs_by_ip`:

1. While building the hostname lookup, detect compound names (space in
   the lowercased host). Parse the first token with the regex
   `^(.+?)([a-z]{2}\d+)$` to extract a `base` prefix. Each remaining
   token that matches `^[a-z]{2}\d+$` is expanded to `base + token`.
2. Record the expanded list in `compound_components[hostid]`.
3. Before the regular name-match loop, iterate `compound_components` and
   **sum** (not max) every billing entry that matches a component. Tag
   the match source as `compound(N)` where N is the number of billing
   hits, and add the consumed billing names to `compound_consumed` so
   they are skipped in later fuzzy passes.
4. Dry-run output gains a `Compound: N` counter next to the other
   match-method tallies.

## Consequences

- Cluster-twin hosts now receive the sum of their members' billing
  entries instead of a single member's price or nothing at all.
- The `compound` source is distinct from `name` / `ip` / `translated`,
  so operators can tell at a glance which cost came from a sum.
- Names consumed by the compound pass are excluded from later passes to
  prevent double-counting through prefix-contains fallbacks.
