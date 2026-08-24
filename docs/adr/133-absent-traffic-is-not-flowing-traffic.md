# ADR 133 — Absent traffic data is not flowing traffic

**Status**: Accepted (2026-08-24)
**Affected**: `tools/diagnose.py` (`_classify_verdict`,
`_verdict_primary_signal`), `tests/test_diagnose_unmeasured_traffic.py`.
**Extends**: ADR 126, ADR 128, ADR 130, ADR 131.

## Context

`_classify_verdict` decided a host's state from two facts — is the agent
reachable, and has traffic collapsed. The second was computed as:

```python
traffic_collapsed = (
    traffic_baseline_mbps is not None
    and traffic_recent_mbps is not None
    and traffic_baseline_mbps >= 5.0
    and traffic_recent_mbps < traffic_baseline_mbps * 0.1
)
```

A host with **no traffic data at all** yields `False` — the same value as a
host measured and healthy. Execution then fell through to the last
agent-unreachable branch, which returned:

> "Agent unreachable but traffic still flowing — agent-side issue
> (restart agent, check connectivity to Zabbix server)."

That sentence asserts traffic is flowing on the strength of never having
measured any.

This is the same root error as ADR 126, 128, 130 and 131, but it is the most
consequential instance found so far, because the others mislabel while this one
**issues a wrong instruction**. Live: four hosts at one site, agent dead and no
traffic for twenty-six days, spread across two separate /24s — every one of
them recommending an agent restart. The evidence pointed at the site or the
provider; the tool pointed at the machines.

It also under-ranked them. `bulk_diagnose` summarises how many hosts are
`down`/`traffic_lost`/`https_down`, so an entire location offline for most of a
month printed as **"0 flagged as down"** directly above a table listing it.

## Decision

**Traffic has three states, not two**: collapsed, flowing, and **unmeasured**.
`traffic_measured` is computed first, and the branches consume it explicitly.

**Agent unreachable with no traffic data is `down`.** Silence on every
available channel is not a lesser condition than "agent down and traffic
collapsed" — it is the same absence with one fewer witness, and must not sort
below it. Its action names the situation and says plainly that this is *not* an
agent-only fault.

**The "traffic still flowing" branch now requires evidence that traffic
flows.** It is reachable only when traffic is measured and not collapsed —
which is the one case where restarting an agent is the right call.

**The bulk summary follows automatically**, because these hosts now carry a
verdict the summary already counts.

## Consequences

A silent host reports as down, reaches the flagged count, and sends the reader
to the provider rather than to the agent.

Eleven tests; four were confirmed to fail against the previous logic. Several
pin the branches that must *not* change — a genuinely agent-only fault still
reads `degraded` with the original wording, a quiet host below the 5 Mbps floor
is still not an outage, and unmeasured traffic on a *reachable* host is still
not down. A fix that turns every ambiguity into an alarm would be the same
error pointing the other way.

The pattern across ADR 126–133 is now explicit enough to state as a rule:
**a predicate that can only be computed from present data must never be read as
false when the data is absent.** Every one of these five defects was that
single mistake, wearing a different verdict each time — down, healthy, zero,
not-found, and now "still flowing".
