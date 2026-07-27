# ADR 095: audit-log resource/action constants were offset from Zabbix

**Status:** Accepted
**Date:** 2026-07-27

## Problem

The audit tables did not match the Zabbix 6.0+ audit constants. Verified
live on this instance: rows the code labelled "Trigger" named a **host**,
and rows labelled "Host group" named **items**. So `4` was being read as
Trigger when it is Host, and `15` as Host group when it is Item — the whole
table was shifted, and every rendered row borrowed a neighbouring object
class's label while the `resource=` filter selected a different class than
the caller asked for.

Separately, four modules inlined the host resource type as a bare `2`.
`2` is not an assigned resource type at all, so those filters matched zero
rows: `get_external_ip_history` reported no rotations ever (its entire
feature, ADR 012), and the host-scoped audit paths in `diagnose_host` and
the risk tools silently saw nothing. Nothing errored — the queries were
valid, just empty.

The action table was wrong in the same way: `4` was labelled "Login" (it is
Logout), `5` was invented for "Failed login" (unassigned, so that filter
could never match), and `6`–`9` were fabricated timeperiod operations.

## Decision

- Replace both tables with the documented 6.0+ constants (Host 4, Item 15,
  Host group 14, Trigger 13; Logout 4, Execute 7, Login 8, Failed login 9,
  History clear 10). The two load-bearing codes are confirmed live; the
  rest are the published constants.
- An unmapped code now renders as `Type N` rather than borrowing a wrong
  label — an honest unknown beats a confident mislabel.
- Name the host constant once in `data.py` (`AUDIT_RESOURCE_HOST`,
  `AUDIT_ACTION_UPDATE`) and use it at every call site, so the value cannot
  drift per-module again.

## Test approach

`tests/test_audit_constants.py`: the verified codes are pinned; `2` is
asserted **absent** from the table; login/logout are distinguished; wire
tests confirm `resource="host"` filters on 4, `resource="item"` on 15 and
`action="login"` on 8; an unknown type renders as `Type N`. An AST guard
fails the suite if any module re-inlines a numeric `resourcetype`.

## Consequences

- Audit output names the right object class, and the host-scoped audit
  features return data instead of an empty set.

## Not included

- **Backfilling every resource type Zabbix defines.** The table covers the
  documented set; anything beyond renders as `Type N` by design.
