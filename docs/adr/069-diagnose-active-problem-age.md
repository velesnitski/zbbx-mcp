# ADR 069: diagnose_host must not age out still-active problems

**Status:** Accepted
**Date:** 2026-06-25

## Problem

Dogfooding `triage_slack_alert` against a real REAL-PROBLEM line, and
cross-checking it with `diagnose_host` on the same host at the same
instant, exposed a verdict bug: **`diagnose_host` reported `healthy` / 0
problems for a host that had eight active Disasters**, the oldest started
~3 days ago. `triage_slack_alert` and `get_active_problems` both showed
them.

Root cause in `_collect_diagnosis_inner`:

```python
problem_cutoff = now - problem_hours * 3600          # 24h default
problems = [p for p in problems if int(p["clock"]) >= problem_cutoff]
```

`problem.get` returns each problem's **start** `clock`. The cutoff drops
any problem that *began* more than `problem_hours` ago — including ones
that are still **unresolved**. So a long-running outage silently
disappears from the diagnosis and the host reads healthy. A days-long
unresolved Disaster is more severe than a fresh one, not less; aging it
out inverts the signal. `diagnose_host`, `bulk_diagnose`, and
`diagnose_subnet` all share `_collect_diagnosis_inner`, so all three were
affected. The bug survived because v1.16.x diagnose tests fed only
within-window clocks.

## Decision

Never age out an **unresolved** problem; window only the recently-resolved
ones. `problem.get` is already called with `recent=True` (active +
recently-resolved), and a resolved entry carries a non-zero `r_eventid`.
A new pure helper replaces the blanket cutoff:

```python
def _keep_active_or_recent(problems, now, problem_hours):
    cutoff = now - problem_hours * 3600
    return [p for p in problems
            if (p.get("r_eventid") or "0") in ("0", "")      # unresolved → keep
            or int(p.get("clock", 0) or 0) >= cutoff]        # resolved → window
```

`r_eventid` is added to the `problem.get` output. `problem_hours` keeps a
real meaning — it now bounds the recently-resolved set — and its three
tool docstrings say so. The verbose render header drops the misleading
"in last Nh" (active problems are no longer windowed).

This is a deliberate **verdict change**: a host with old, unresolved
problems now correctly reads non-healthy.

## Test approach

`TestKeepActiveOrRecent`: the pure helper keeps a 72h-old unresolved
problem, keeps one with `r_eventid == "0"`, drops a 72h-old *resolved*
one, and keeps a recently-resolved one. Plus a wire-level case driving
`_collect_diagnosis_inner` through the problem-only fake client — a
72h-old active Disaster yields a non-`healthy` verdict (the exact bug).
633 → 641 tests. (Per ADR 068's lesson, the verdict path is exercised
end-to-end, not just the pure helper.)

## Consequences

- `diagnose_host` / `bulk_diagnose` / `diagnose_subnet` no longer emit a
  false `healthy` for long-running outages — the most dangerous direction
  for a triage tool to be wrong.
- No new API round-trips (`r_eventid` rides the existing `problem.get`).
- Tool count unchanged (163).

## Not included

- **Excluding recently-resolved entries from the verdict count.** They
  still count toward `open_problems` (pre-existing behaviour; `recent=True`
  bounds them to Zabbix's OK-display window). A just-resolved problem
  reading `degraded` is the opposite, far smaller error; a verdict that
  counts only unresolved problems is a separate follow-up.
