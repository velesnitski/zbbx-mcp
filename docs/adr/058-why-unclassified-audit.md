# ADR 058: Why-unclassified breakdown in `get_product_audit`

**Status:** Accepted
**Date:** 2026-06-12

## Problem

A live `get_product_summary` shows ~21% of the fleet classified
**Unknown / Unknown** — the single largest bucket after the flagship
product. Every group-keyed view (product summaries, audits, cost
efficiency, executive dashboards) silently misattributes that fifth of
the fleet.

The cause is always the same: the host's groups contain names that
`ZABBIX_PRODUCT_MAP` does not map. But nothing reported *which* names —
the operator had to diff group lists against the map by hand, so the
Unknown bucket only ever grew.

## Decision

When `get_product_audit` is asked about the Unknown bucket
(`product="Unknown"` — substring-matched, as the tool already does), it
now appends a **Why unclassified** section: every unmapped group name
with the number of Unknown hosts carrying it, sorted by count. That list
is literally the set of `ZABBIX_PRODUCT_MAP` entries to add — fixing the
top few names typically reclassifies most of the bucket. Hosts with no
groups at all are counted under `(no groups)`.

Names already present in the map are excluded even when mapped to
*skip* — an explicit skip is an operator decision, not a gap.

The counting lives in a pure helper `classify.unmapped_group_counts(
group_sets, pmap)` (classify.py has no tools/ imports, so the helper
takes plain data). The audit body change is additive — a section at the
end — so existing output is unchanged for every other product query.

## Test approach

Four pure-helper tests (`TestUnmappedGroupCounts`): count-desc/name-asc
ordering; mapped and skip-mapped names excluded; group-less hosts counted
under `(no groups)`; empty input. The tool wiring is config-level over
the tested helper.

## Consequences

- Tool count unchanged (161). Tests +4 (574 → 578).
- Auditing "Unknown" becomes self-explanatory: the output names the exact
  mapping entries to add, with impact counts to prioritise by.
- No extra API calls — the breakdown reuses groups already fetched.

## Not included

- **Auto-writing map suggestions to the env/JSON file.** The product map
  encodes business decisions (which tier, what to hide); the tool's job
  ends at making the gap list precise.
- **A standalone tool.** This belongs inside the audit the operator
  already runs; a separate tool would just fragment the workflow.
