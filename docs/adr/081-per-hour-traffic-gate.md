# ADR 081: Per-hour traffic gate + test-pattern gaps

**Status:** Accepted
**Date:** 2026-07-14

## Problem

Two residues from recent fixes, both found by cross-validating against the
sibling reporting pipeline (which computes the same availability figures from
the same trends):

1. **The uptime traffic gate was window-wide (task 172).** ADR 075's
   `compute_host_uptime` took `host_has_traffic` as one boolean — "did the
   host move traffic at any point in the window" (`traffic_map[hid] >= 1.0`
   in the caller). Every *missing* check-hour was then rescued for any host
   that had ever had traffic: a box that served for a week and then
   **hard-died mid-window read ~100% instead of ~50%** — the task-168
   inflation, back through the side door. The false-down guard (task 169)
   needs traffic *in the silent hour*, not traffic *somewhere in the window*.

2. **The test-host pattern had two gaps (ADR 080 follow-up).** The default
   `(?:^|[-_\s])test(?:[-_\s]|$)` missed dot separators (`a.test.b`) and
   numbered test boxes (`x-test2-y`). The sibling pipeline's segment matcher
   catches both, so the two systems disagreed about which hosts are
   production — a determinism split.

## Decision

1. **`traffic_hours_from_trends(rows, divisor)`** (pure, `uptime.py`) — the
   set of hour buckets in which ANY physical NIC cleared 1 Mbps (an idle NIC
   can never mask a busy carrier; junk rows skipped). `divisor` comes from
   the new public **`fetch.TRAFFIC_DIVISOR`** so the bytes-vs-bits convention
   (`ZABBIX_TRAFFIC_UNIT`) stays defined once.
2. **`compute_host_uptime(host_has_traffic=...)`** now accepts a **set of
   hour buckets** (preferred): a missing check-hour counts UP only if THAT
   hour had traffic. A bool is still accepted for callers without per-hour
   data — documented as the inflating legacy form.
3. **`get_service_uptime_report`** fetches physical-NIC trends
   (`is_physical_traffic_in_key`, ADR 078) for the same window in the same
   `asyncio.gather`, and passes per-host hour sets; the window-wide boolean
   remains only as fallback for hosts with no NIC trend rows at all.
4. **Test pattern** default becomes `(?:^|[-_.\s])test\d*(?:[-_.\s]|$)` —
   dots as separators, optional trailing digits. Lookalikes (`latest`,
   `contest`, `attestation`, `testing`) still cannot match;
   `ZABBIX_TEST_NAME_RE` override unchanged.

## Test approach

The task-172 pin: checks+traffic week 1, nothing week 2 → **~50%**, with the
legacy-bool contrast asserting the same host reads 100% (so the regression
is visible, not just the fix). Per-hour rescue counts only traffic hours;
empty set rescues nothing; a traffic hour never overrides an explicit down.
`traffic_hours_from_trends`: threshold at exactly 1 Mbps, any-NIC-clears,
junk skipped. Pattern: `x-test2-y`/`test3-a`/`a.test.b` match, five
lookalikes do not. +9 tests, 750 → 759.

## Consequences

- A premium/free box that served and then hard-died now shows its real
  uptime in `get_service_uptime_report` — parity with the sibling pipeline.
- One extra `item.get` + one extra `trend.get` per uptime-report call,
  bounded by the same `24 * 31` row cap as the service trends.
- Tool count unchanged (163).
