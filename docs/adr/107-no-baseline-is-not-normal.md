# ADR 107 — "No baseline" is not "normal"

**Status**: Accepted (2026-08-14)
**Affected**: `src/zbbx_mcp/tools/traffic_shaping.py` (`NO_BASELINE`,
`classify_shaping`, un-judged disclosure), `tests/test_traffic_shaping.py`.
**Extends**: ADR 103, ADR 105. Mirrors zabbix-reports ADR 0069/0073.

## Context

Chasing the ADR 105 field report turned up two more hosts in the same group,
each running at a small fraction of a sibling's throughput — and each with
freshly re-created items, so **no trend reached back past the recent window**.

`classify_shaping` handled the missing baseline by setting `base_ceiling =
None`, which makes the drop test unevaluable. The ceiling branches then fell
through to the final `return NORMAL` — *"peaks spread normally"*. Verified
against the previous commit: `classify_shaping(varying_series, [])` returns
`normal`.

That is health asserted from no evidence. `normal` is a **comparative** claim —
"against what came before, nothing changed" — and there was no before. A host
whose history had just been destroyed rendered identically to one that had been
fine all along.

The tool also only *counted* the states it could not judge (`… 2 insufficient`)
in a header line. A count is exactly what a reader skips, and it never names the
host, so there is nothing to act on.

This is the same failure the reporting side hit twice: an item rebuild resets
the measurement window and the metric reports the reset as a result
(zabbix-reports ADR 0069, then ADR 0073). Trends belong to the *item*, so
recreating items destroys history — and every comparative detector inherits
that blind spot.

## Decision

- New verdict **`no_baseline`**. Reached only when the recent window is
  measurable but nothing precedes it. It replaces the `normal` that used to be
  returned there.
- **`capped` still fires without a baseline**, deliberately. A ceiling is an
  observation about the recent window alone; it does not need a past. Only the
  comparative half is withheld. The distinction is the whole point: withhold
  the claims that need history, keep the ones that don't.
- Hosts the tool looked at and could not judge (`insufficient` or
  `no_baseline`) are **named in the output with how far back their history
  actually reaches** — `node-x (3h of history)` — followed by the reason it
  matters: *"Recreating a host's items destroys its trend history, so a host
  that just lost its past reads the same as one that never had a problem —
  this line is the difference."*

## Consequences

- A rebuilt host now surfaces as a host that cannot be assessed, which is a
  finding, instead of disappearing into a "normal" count.
- The history length is the actionable part: 3h of history on a long-lived host
  means the items were rebuilt; on a host provisioned yesterday it means
  nothing is wrong.
- `detect_traffic_drops` already disclosed this class via its skip breakdown
  (`no_history`, `no_baseline_window`) — that is the pattern followed here.
  `detect_traffic_erosion` returns `insufficient` below `min_weeks`, so it is
  guarded, but it does not name the hosts either; worth aligning when it is
  next touched.

## Verification

929 tests pass (+4). The behaviour change is pinned against the previous
commit: `NO_BASELINE` did not exist there and the same input returned `normal`.
