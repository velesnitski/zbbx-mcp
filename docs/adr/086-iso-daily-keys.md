# ADR 086: ISO-sortable daily trend keys (month-boundary sort bug)

**Status:** Accepted
**Date:** 2026-07-17

## Problem

`fetch_trends_batch` keyed each `TrendRow.daily` entry by
`dt.strftime("%b %d")` → `"Jul 03"`. Several consumers then called
`sorted(daily)` and treated the result as chronological order. But `"%b %d"`
sorts **lexically**, and `"Jul" < "Jun"` (`l` < `n`), so:

```
sorted(["Jun 21", "Jun 30", "Jul 01", "Jul 20"])
  → ["Jul 01", "Jul 20", "Jun 21", "Jun 30"]
```

Any window crossing a month boundary — i.e. essentially always for the 30d
default — was scrambled, silently flipping verdicts:

- **`get_traffic_drop_timeline`** took `vals[-1]` as "today" → the wrong day at
  a month edge, so its drop gate skipped genuinely-blocked countries. A
  blocking-detection tool returning a **false negative** is the worst case.
- **`detect_regional_anomalies`** (daily mode) split `recent`/`baseline` on the
  scrambled order, so a real drop read as `drop_pct = 0`. The helper it
  calls, `recent_baseline_from_daily`, even *documents* that it needs
  "lexically sortable" `2026-06-04` keys — the caller was violating its
  contract, which is the tell that ISO keys were always the intended design.
- **`get_executive_dashboard`** computed `first_week`/`last_week` from
  `days[:7]`/`days[-7:]` → growth **backwards**.
- `get_geo_traffic_trends` and `generate_ceo_report` derived trend *direction*
  from the scrambled order too (silently, since they show no keys).

## Decision

Fix it at the source: key `daily` by **`"%Y-%m-%d"`** (ISO, sortable,
year-carrying), so every `sorted(daily)` is chronological by construction and
all consumers above are correct without touching their logic. A pure
`day_label("2026-07-03") → "Jul 03"` renders the key compactly at the (few)
sites that show it — the drop-timeline "Drop Started" cell and the daily-
breakdown column headers — so user-facing output is unchanged.

`get_month_over_month` already *parsed* the keys with a guessed year; it now
parses `"%Y-%m-%d"` directly, which also removes its year-boundary guess.

## Test approach

`test_anomaly.py` (+4): `recent_baseline_from_daily` across a Jun→Jul boundary
now picks the actually-latest days (a real July drop is visible, not masked);
`day_label` renders ISO → `Mon DD` and falls back on non-ISO; the core
invariant `sorted(iso_keys)` is chronological across a month. The existing
`recent_baseline_from_daily` suite already used ISO keys, so it stays green —
further confirmation ISO was the intended contract. 766 → 770.

## Consequences

- Traffic-drop, regional-anomaly, executive-growth, geo-trend and CEO-report
  verdicts are correct across month boundaries — no more silent flips on the
  30d default.
- One representation change fixes five consumers; the fragile "sort a
  human-formatted date" pattern is gone. Tool count unchanged (163).

## Not included

- **Changing the user-facing label** — output still reads `Jul 03`, not
  `2026-07-03`; only the internal key changed.
