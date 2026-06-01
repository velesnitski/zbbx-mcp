# ADR 041: Render the HIGH severity tier in `get_predictive_alerts`

**Status:** Accepted
**Date:** 2026-06-01

## Problem

ADR 040's companion change added a sanity-floored, four-tier severity
classifier to `get_predictive_alerts` (CRITICAL / HIGH / WARNING / INFO).
The classifier writes the correct tier into each alert's `severity`
field, and the unit tests on the classifier pass.

But the markdown render layer was never updated to know about the new
`HIGH` tier. Two spots assumed the legacy three-tier set:

1. **Table cell.** The per-row severity was mapped with:

   ```python
   sev_cls = "CRITICAL" if s == "CRITICAL" else "WARNING" if s == "WARNING" else "INFO"
   ```

   Any value that is neither `CRITICAL` nor `WARNING` fell through to
   `INFO`. So every `HIGH` alert rendered as `INFO` — the *lowest*
   visible tier.

2. **Summary counts.** Only `CRITICAL` and `WARNING` were tallied, so
   `HIGH` alerts were absent from the trailing summary entirely.

Net effect: an item classified one step below the top — projected to
breach a resource threshold within roughly a week and already in the
concerning band — was displayed as the most benign category and omitted
from the call-to-action summary. A false-negative (under-alert), which
is why a "no false-positives" spot-check did not surface it: nothing was
over-flagged; real near-term risks were quietly down-flagged.

The classifier itself was correct end-to-end. Only the presentation
collapsed the tier. Found by comparing live tool output against the
committed classifier: an item the classifier must score `HIGH`
(within the day-and-headroom gate) came back labelled `INFO`, which is
unreachable from the classifier for that day count — isolating the gap
to the render path.

## Decision

1. Render the `severity` field directly in the table — it is already
   one of the four canonical uppercase strings, so no mapping is
   needed:

   ```python
   f"| {a['severity']} | {a['label']} | ..."
   ```

2. Add a `HIGH` line to the summary, between `CRITICAL` and `WARNING`,
   with horizon-appropriate guidance:

   - CRITICAL — act now (≤3 days)
   - HIGH — act this week (≤7 days)
   - WARNING — within 2 weeks

`INFO` remains unlisted in the summary (informational only; reachable
only when the forecast horizon exceeds 14 days).

## Consequences

- HIGH-tier forecasts now display as HIGH and are counted in the
  summary — visible to operators instead of buried as INFO.
- No classifier change; this is presentation-only.
- No other module consumes the predictive-alert severity, so the blast
  radius is this one render block.
- Tests remain green (517).

## Lesson

When a classifier grows a new tier, the render and summary paths are
separate surfaces that must learn the tier too. A passing classifier
test suite does not prove the tier reaches the user. Prefer rendering an
already-canonical field directly over re-deriving it with a branch that
enumerates "known" values and has an `else`.
