# ADR 008: Tighten Pass-5 prefix name match in `import_costs_by_ip`

**Status:** Accepted
**Date:** 2026-04-23

## Problem

`import_costs_by_ip` has a seven-pass name matcher. Pass 5 (prefix match)
used bidirectional `startswith` with first-match-wins:

```python
for zname in name_list:
    if zname.startswith(name_lower) or name_lower.startswith(zname):
        matched_host = name_to_host[zname]
        break
```

Intent: catch sheet/billing names that are a truncation of the full Zabbix
hostname (e.g. sheet `app-eu1`, Zabbix `app-eu1-retired`).

Actual behaviour in a dense numeric-suffix namespace: every shorter name is a
proper prefix of many longer names. When a reconciliation sweep was run
against the authoritative spreadsheet, Pass 5 silently bound:

- Zabbix `srv10` → sheet `srv100` (different hosts, different IPs, different
  prices). The `startswith` check is satisfied because `srv100` begins with
  `srv10`.
- Zabbix `web1` → sheet `web14`. Same failure mode.
- 16 hosts in one host family — all Zabbix `<prefix>10X` names incorrectly
  bound to sheet `<prefix>10X0` names. All priced $27.96 apart because the
  two series are distinct SKUs at the same provider.

A diff simulation before the write showed 24 host changes, of which:
- 16 were this digit-extension false positive.
- 7 would have overwritten hosts already correctly bound by IP in a prior
  run (including three hosts fixed in the same session).
- Only 1 was a legitimate reconciliation.

## Decision

Two complementary guards on Pass 5, plus an opt-in strict mode.

### 1. Digit-extension guard

After the `startswith` check, inspect the leftover characters. If the first
leftover character is a digit, reject the match:

```python
if zname.startswith(name_lower):
    rest = zname[len(name_lower):]
elif name_lower.startswith(zname):
    rest = name_lower[len(zname):]
if rest and rest[0].isdigit():
    continue  # same family, different host
```

Separator characters (`-`, `_`, `.`) and letters are still accepted, so
`app-eu1 ↔ app-eu1-retired` continues to match.

### 2. Ambiguity skip

Collect *all* candidates, not the first. If more than one Zabbix name
satisfies the prefix relation after the digit-extension guard, return
`None`. First-match-wins in arbitrary dict order is not a defensible tie
break.

### 3. `name_match_strict=False` flag on `import_costs_by_ip`

When `True`, Pass 5 is skipped entirely. Use for reconciliation runs
where a wrong bind is costlier than a missed match — the caller can
re-run with the flag off on the remaining unmatched set.

## Consequences

- The Pass-5 logic is extracted into `_prefix_name_match`, a pure
  function with 8 regression tests covering digit-extension
  (short→long and long→short), non-digit extension, ambiguity,
  exact-name short-circuit, under-length input, and mixed candidate
  sets where one valid candidate coexists with a digit-extended one.
- Defaults are unchanged for callers that did not opt in to strict
  matching; the behaviour change is strictly a narrowing — some
  previously-matched pairs will now return unmatched. This is the
  correct outcome: those were the false positives.
- 222 tests pass (214 pre-change + 8 new).
- Full suite runs in 8s; no network or fixtures touched.

## Not included in this change

- The seven-pass name match still exists. We could collapse it, but
  each pass has a legitimate job when the input is known-clean.
- `max(host_costs.get(hid, 0), cost)` pass-to-pass stacking is still
  in place. It is only dangerous when two passes match the same host
  with different prices; Pass 5 was the main offender, so that risk
  falls with this change.
