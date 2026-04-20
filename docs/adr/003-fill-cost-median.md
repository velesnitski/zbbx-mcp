# ADR 003: `fill_cost_median` for empty-cost hosts

**Status:** Accepted
**Date:** 2026-04-20

## Problem

After exhausting every direct match against the billing source (IP, name,
subnet, compound, translated), a residual set of hosts still had empty
`{$COST_MONTH}`. Accounting would not be providing further per-server
breakdowns. Operators wanted a rough but consistent estimate so the cost
summary is representative across the fleet.

## Decision

Add a new write tool `fill_cost_median`:

- Inputs: `group_by` (`"product"` | `"provider"`) and `dry_run`.
- Reads all enabled hosts + their `{$COST_MONTH}` macros.
- Buckets costed hosts by the chosen key (product/tier from host groups,
  or provider from CIDR detection of the primary interface).
- Takes the median per bucket.
- For each empty-cost host **with** an IP (monitoring hosts without IP
  are skipped), if its bucket has a median, that value is assigned.
- Dry-run lists the top candidates, the per-bucket median, and the
  total projected delta. Apply writes a descriptive macro note
  (`estimated from <group_by> median`).

Registered in `WRITE_TOOLS` so read-only mode hides it.

## Consequences

- Empty-cost hosts get a representative estimate derived from their
  peers, rather than nothing. The cost summary becomes more complete
  for reporting without importing fabricated precision.
- Hosts carry a macro description flagging the value as an estimate,
  so a human review can refine it later.
- Monitoring hosts without IPs are intentionally excluded — they do not
  represent server cost.
- Median (not mean) is used to resist outliers: a handful of expensive
  premium boxes in a group will not inflate the estimate for its
  lower-tier peers.
