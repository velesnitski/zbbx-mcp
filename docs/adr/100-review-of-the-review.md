# ADR 100: reviewing the fixes — two regressions the batch introduced

**Status:** Accepted
**Date:** 2026-07-28

## Problem

ADRs 093–099 fixed 22 defects sharing one shape: code that returned a
confident wrong answer instead of an error. That change set touched ~30
files and was never independently reviewed. An audit of it found that two of
the fixes had introduced the very class they were meant to remove.

**1. The country allow-list erased ~50 real countries.** ADR 093 made
`ISO2_CODES` load-bearing: a code outside it is discarded as "not a
country". But the set was derived from the ISO-3/English *name* lookup
table, which only covers 200 codes — so the 50 alpha-2 codes with no name
entry (mostly dependencies and overseas territories) became invalid
overnight. A host in one of those territories resolved to a blank country
everywhere `extract_country` is used, dropping silently out of every
country roll-up, and `normalize_country` rejected a genuine ISO-2 code as
unknown input. Strictly worse than the bug ADR 093 fixed, because it was
silent across ~60 call sites.

**2. The disruption detector went blind to total outages.** ADR 098 made
`detect_disruption_wave` require an interface to report in *both* windows,
to stop a renamed item inflating only the baseline side. But a host that
goes completely dark — agent down, host offline, network cut — has **no**
recent rows at all, so it was skipped entirely and never entered the
analysis. That is precisely the mass-outage case the tool exists to find.
The accompanying test covered only the multi-NIC variant, which is how it
shipped.

The audit also found `get_trends` was only half-fixed (ADR 096 bounded the
default call, but an explicit `time_from` still returned the oldest rows,
now *sorted newest-first* and therefore more misleading than before), and
that widening the seasonal bucket to ±1 hour (ADR 097) silently weakened
`min_samples`: the threshold of 3 was set for a bucket a third of the size,
so a floor could now form from a single day's three consecutive hours.

## Decision

- **`ISO2_CODES` covers every assigned code.** A `_ISO2_TERRITORIES` set
  supplies the codes absent from the name table; the allow-list is their
  union — 250 entries (249 official plus `XK`, user-assigned but in
  practical use). Pinned by a test so it cannot silently shrink again. The
  ADR 093 property is unchanged: a non-ISO tag is still rejected.
- **One shared aggregation rule**, `anomaly.aggregate_host_windows`, pure and
  unit-tested, encoding three rules that each fix a distinct wrong answer:
  a bond is not additional to its slaves (so a bonded host is not
  double-counted); only interfaces present in both windows contribute (so a
  renamed item cannot manufacture a drop); and **total silence is an outage,
  not an absent host** (baseline preserved, recent 0.0). Independent NICs
  now *sum* rather than max — two active 3 Mbps cards genuinely carry 6, and
  a plain max would drop that host under a 5 Mbps floor.
- **`get_trends` anchors on the window's end.** `limit` is applied by the
  server to an ascending scan, so the window's *start* decides what returns;
  the range now walks back from `time_till` (or now) far enough to hold
  `limit` rows, clamped to any caller-supplied `time_from`.
- **`min_samples` scales with the bucket** (3 → 9, i.e. 3 hours × 3 days), so
  "enough history to know what normal looks like" means what it did before.

Smaller items from the same audit: the ADR 098 guard scanned only `tools/`
despite claiming tree-wide coverage — it now scans the whole package, with
named-constant *definitions* exempted (naming the value is the fix, not a
violation); the aggregation test now imports the shared rule instead of
restating it, so it can actually fail when the implementation changes; two
tautological assertions were replaced with a real ordering check; and a
stale module row in CLAUDE.md was corrected.

## Test approach

The two regressions are pinned by the cases that would have caught them:
a single-NIC host going fully dark reads a 100% drop rather than vanishing,
and the territory codes resolve through both `extract_country` and
`normalize_country`. `seasonal_floor` fixtures now use the ±1 bucket
realistically and assert the property that matters — a lone freak-low
reading can no longer *become* the floor. 870 → 872.

## Consequences

- The batch's own blast radius is now covered by tests rather than by
  assumption.
- Worth stating plainly: both regressions were introduced by fixes that
  were themselves correct in intent, tested, and passed a full green suite.
  What caught them was an independent read of the diff — not the test suite,
  which was written by the same pass that made the mistakes.

## Not included

- **Adding the territory codes to `REGION_MAP` / `CAPITAL_COORDS`.** Those
  tables are internally consistent at 200 and drive region filters and
  distance estimates; extending them is a separate, larger data change.
  A territory therefore resolves as a country but is not in any region.
