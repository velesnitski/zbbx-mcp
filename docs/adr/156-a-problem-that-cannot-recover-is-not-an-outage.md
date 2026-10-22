# 156. A problem that cannot recover is not an outage

## Status

Accepted (v1.16.76)

## Context

Zabbix does not close a problem when its trigger, or every item behind it,
is disabled, and `problem.get` cannot filter on trigger status. Such an event
stays in the problem table indefinitely. Every tool that treats open problems
as current state counted it as a live outage: problem listings, the
active-problems summary, the per-host diagnosis and its verdict, alert
triage, correlation and flood views, the Slack digest. On a real fleet these
frozen problems were the larger part of everything marked urgent, and
health read far worse than it was.

## Decision

`frozen.py` resolves the problems' trigger ids in one `trigger.get` with the
trigger and item status. A problem is frozen when its trigger is disabled,
when the trigger has items and every one is disabled, or when the trigger no
longer exists.

Two guards decide the direction of any error. If the lookup fails or returns
nothing for a non-empty set, every problem stays live: a fetch failure must
never shrink an incident count. A trigger with no items is live, because
calculated and aggregate triggers report none.

Frozen problems are excluded from counts and verdicts and are never dropped
silently: each surface prints one line naming how many were excluded as
monitoring debt. `get_problems(include_frozen=True)` lists them, marked.
History views built on events are untouched.

## Consequences

One extra call per problem-counting tool, over the distinct trigger ids.
Counts fall where they were inflated; the excluded number is still shown,
because disabling a trigger without resolving its problem is real cleanup
work.
