# ADR 068 — triage_slack_alert: problem.get rejects selectHosts (live fix)

**Status:** accepted
**Date:** 2026-06-25
**Tag at acceptance:** v1.16.1

## Context

ADR 067 shipped `triage_slack_alert` (v1.16.0) with 25 tests — all
against the **pure core** (`alert_triage.py`: parsing, host-candidate
extraction, match/triage classification). The **orchestration layer**
(`tools/triage.py`, the actual `client.call` sequence) had **no test**.

First live invocation against Zabbix 7.4.9 failed immediately:

```
Zabbix API error (-32602): Invalid parameter "/": unexpected parameter "selectHosts".
```

Root cause: the ground-truth step called `problem.get` with
`selectHosts: ["hostid"]`. Unlike `event.get` and `trigger.get`,
**`problem.get` does not support `selectHosts`** — a problem only carries
`objectid` (its trigger). The subsequent `p.get("hosts", [])` loop that
attributed problems to hosts was therefore dead even if the call had
succeeded. The tool was 100% broken on every real call; generic-fixture
unit tests couldn't see it because they never hit the wire.

## Decision

Map problem → host through the trigger, which the code already fetches:

1. **Drop `selectHosts` from `problem.get`.** Keep `hostids` (a valid
   filter) + `objectid` in the output.
2. **Extend the existing `trigger.get`** (already called for dependency
   collapse) with `selectHosts: ["hostid"]`, building a
   `triggerid → [hostid]` map.
3. **Attribute each problem** to the resolved host(s) of its `objectid`'s
   trigger, intersected with the set we actually resolved (a multi-host
   trigger can name hosts the alert didn't).

No new API round-trips — the trigger fetch already existed for dependency
collapse; it now does double duty.

### Test gap closed

Added `TestTriageWireContract` — a recording fake client + capture-MCP +
stub resolver that runs the real tool function and asserts the wire
contract:

- `problem.get` is called **without** `selectHosts` (the bug),
- `trigger.get` is called **with** `selectHosts` (the fix),
- a live problem on the resolved host's trigger yields **`real_now`**
  even when the feed line claimed `RESOLVED` (the tool's whole point).

Both new assertions fail on the v1.16.0 code. 633 → 636 tests.

## Consequences

- `triage_slack_alert` actually works against Zabbix 7.x.
- The orchestration layer now has wire-level coverage; future `client.call`
  contract drift (wrong `select*`, renamed params) is caught in CI, not on
  first live use.
- Lesson logged: a tool that only unit-tests its pure core is unverified
  until one wire-contract test exercises the real `client.call` path.
  Worth applying to other orchestration tools.
- ACTION: reconnect `/mcp` to load v1.16.1, then re-run the live alert line.
