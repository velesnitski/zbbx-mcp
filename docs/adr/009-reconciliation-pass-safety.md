# ADR 009: Reconciliation-pass safety (prefer-earliest, zero-extras, dup-names)

**Status:** Accepted
**Date:** 2026-04-24

## Problem

A cost-reconciliation session against the authoritative spreadsheet surfaced
three separate failure modes in the import tools:

1. **`import_costs_by_ip` overwrote IP-backed prices with name-match prices.**
   A host correctly bound by IP in Pass 1 at $163.80 was observed re-bound to
   $84.95 by a Pass-2 exact-name match, because the terminal block used
   `max(ip_cost, name_cost)`. Pass semantics were "later passes fill gaps";
   the `max()` promoted them to "later passes can upgrade". IP is strictly
   more authoritative than name, so a name-match must not displace it.

2. **`import_cluster_ip_fees` could not re-tag a legacy macro without
   changing its value.** The dispatcher skipped every entry with
   `extra_cost_month <= 0`, so the escape-hatch combination
   `overwrite_base=X, extra_cost_month=0` silently did nothing. Re-tagging a
   historical `base X + N extra IPs (Y)` description into the provenance-
   prefixed `src:cluster_extras ...` form required going through
   `import_costs_by_ip` instead, which writes a different source tag.

3. **`import_costs_by_ip` silently dropped conflicting sheet rows.** When
   the input had three ip-entries for the same `name` field at three
   distinct prices, the map build kept whichever iterated first. Downstream
   the tool reported a clean "1 host matched" — no indication that two
   other prices had been discarded, and no way for the operator to notice
   the upstream inconsistency.

## Decision

### 1. Prefer earliest pass in the name block

The name-match terminal block changes from:

```python
host_costs[hid] = max(host_costs.get(hid, 0), cost)
if hid not in host_source:
    host_source[hid] = "name"
```

to:

```python
if hid in host_costs:
    continue  # IP/compound pass already bound — do not overwrite
host_costs[hid] = cost
if hid not in host_source:
    host_source[hid] = "name"
```

IP-ish passes (1, 1b, 1c) are unchanged. Only the name → IP-match collision
case is tightened.

### 2. Allow `extra_cost_month == 0`

Dispatcher skip becomes `extras < 0` instead of `extras <= 0`. A zero-extras
entry rewrites the description (with the existing or `overwrite_base`-
supplied base) without changing the summed value. The description format
still matches the `_CLUSTER_EXTRAS_RE` parser (`... + 0 extra IPs (0.00)`),
so idempotency on re-runs is preserved.

### 3. Duplicate-name detection

A new pure helper `_dedup_name_from_ip_entries` walks the ip-entry dict,
groups prices by name, and returns `(unique, duplicates)`. Only single-
consistent-price names feed the name-match map. Duplicates are surfaced in
the dry-run output so the operator can clean the upstream source:

```
⚠ Duplicate-name entries (dropped from name-match): 3
- `host-foo`: $72.00, $84.95, $163.80
- ...
```

If a duplicated name also has an explicit `by_name` entry with a fourth
price, it is also dropped — we cannot trust any of the values in isolation.

## Consequences

- 230 tests pass (222 pre-change + 8 for the dedup helper).
- The three bugs found in a real reconciliation session are regression-
  guarded by unit tests.
- A reconciliation operator will now see duplicate-name conflicts instead
  of silent first-wins behaviour.
- No breaking change to callers: the three fixes each tighten or widen
  behaviour only in cases that were producing wrong results.
- Legacy macros in the `base X + N extra IPs (Y)` format (no `src:` prefix)
  can now be re-tagged by `import_cluster_ip_fees` with
  `overwrite_base=current_base, extra_cost_month=0`. Previously the only
  option was to route through `import_costs_by_ip`, which writes
  `src:billing_ip` rather than the semantically-accurate
  `src:cluster_extras`.

## Not included

- Pass 1b (/24 prefix) still has its own upgrade logic (`if hid not in
  host_costs or cost > host_costs[hid]`). That is independently defensible
  (a full-IP match is strictly better than a /24-prefix match) and is
  orthogonal to the name-overwrite bug.
- Pass 1c (compound sum) can still grow past a pass-1 value for compound
  hosts; by construction it is summing multiple billing entries into one
  Zabbix monitoring entity, so beating a single-IP value is the correct
  outcome.
