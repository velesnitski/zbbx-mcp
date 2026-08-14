# ADR 111 — Answer "why is there no history", don't assert it

**Status**: Accepted (2026-08-14)
**Affected**: `src/zbbx_mcp/tools/traffic_shaping.py` (`host_added_hours`,
`explain_unjudged`), `src/zbbx_mcp/data.py` (`AUDIT_ACTION_ADD`),
`tests/test_traffic_shaping.py`.
**Extends**: ADR 107.

## Context

ADR 107 added a disclosure naming the hosts the shaping detector could not
judge, and explained why it mattered:

> _Recreating a host's items destroys its trend history, so a host that just
> lost its past reads the same as one that never had a problem._

The first time that line was read against real data it was **wrong**. Twenty
hosts on one dashboard came back un-judged with ~50h of history each; none had
been rebuilt. They had been provisioned two days earlier — `node-x-de0998`
added by ansible at 2026-08-12 10:07.

Both readings fit the same observation exactly: a short history means the items
were rebuilt, *or* the host is new. The disclosure picked one and stated it as
fact — the same defect the ADR it belongs to exists to remove, one level up, in
the prose instead of the data.

## Decision

Resolve it instead of hedging. The audit log records host creation
(`resourcetype=4`, `action=0`), so one `auditlog.get` for the ≤5 named hosts
answers the question outright:

- `node-a: 50h of history — host added 46h ago, so it is simply new`
- `node-b: 3h of history but host added 720h ago — its items were rebuilt and
  the earlier history is gone`
- `node-c: 3h of history, age unknown (no Add record in audit retention)`

Three states, not two. Audit retention is finite, so an old host legitimately
has no Add record; **that is "cannot tell" and must not collapse into either
answer** — pinned by test, because collapsing it is exactly how the original
wording went wrong.

Supporting choices:

- A 6-hour slack absorbs the boundary: trend flush timing makes "history
  reaches back as far as the host is old" differ by an hour or two either way.
- `host_added_hours` is best-effort — a failure returns `{}` and every host
  degrades to *unknown*. It enriches a disclosure and must never break one.
- The call runs only when there is something to explain, and only for the
  handful of names actually printed.

## Consequences

- The line now tells an operator what to do: *new* means wait, *rebuilt* means
  the history loss is real and the host needs watching, *unknown* means go look.
- One extra API call on runs that have un-judged hosts, none otherwise.
- The general lesson, recorded because this is the second time it has bitten:
  a disclosure is output too. "I cannot tell you X" is honest; "X is because Y"
  needs the same evidence as any other claim the tool makes.

## Verification

951 tests pass (+6): new host called new, old host with short history called
rebuilt, missing audit record called unknown and neither of the other two, the
boundary slack, an audit failure degrading to unknown rather than erroring, and
no call issued when there is nothing to explain.
