# ADR 077: Illegal `selectAcknowledges` field + select-field guard

**Status:** Accepted
**Date:** 2026-07-13

## Problem

`get_problem_detail` was **dead on every problem**. A live call returned:

```
-32602: Invalid parameter "/selectAcknowledges/2": value must be one of
"acknowledgeid", "userid", "clock", "message", "action", "old_severity",
"new_severity", "suppress_until", "taskid".
```

The call asked for `selectAcknowledges: ["userid", "alias", "message",
"clock", "action"]`. **`alias` is not a field of the acknowledge object** —
it was the pre-5.4 *user* field (renamed `username` in Zabbix 5.4) and never
belonged here. Zabbix rejects the whole request, so the tool failed on every
input, not just on acknowledged problems.

A second, quieter bug rode along: the renderer printed
`a.get('alias', '?')`, so even if the API had accepted the request, the
acknowledgement author would have rendered as `?` forever — the acknowledge
object carries only `userid`, never a name.

**Why the ADR 072 guard missed it.** That guard AST-scans `client.call()`
dict literals against a deny-map of *parameter names* (`selectHosts` on
`problem.get`, the twice-shipped -32602 of ADR 068/070). Here the parameter
name was perfectly valid — the illegal token was a *field inside its list*.
The guard had no notion of legal field values, so the class was invisible to
it. The ADR 071 wire test also passed: `RecordingClient` returns canned data
and never validates params against Zabbix's schema.

## Decision

1. **Fix the contract.** `selectAcknowledges` now requests only legal fields:
   `["userid", "clock", "message", "action"]`.
2. **Restore the author.** A new best-effort `_resolve_usernames()` maps
   `userid → username` via `user.get`. A token without `user.get` rights
   yields an empty map and the renderer falls back to `user <id>` — it
   degrades, never crashes, and never prints a bare `?`. No `user.get` call
   is made when a problem has no acknowledgements.
3. **Close the class.** A new **select-field guard** in `test_guards.py`
   AST-scans the string literals *inside* known `select*` lists against the
   field sets Zabbix accepts (`ALLOWED_SELECT_FIELDS`, sourced from the API's
   own error enumeration). A `not_vacuous` test asserts the scanner actually
   sees the guarded call sites, so it cannot pass by failing to look.

## Test approach

Guard fires: re-introducing `"alias"` makes `test_select_fields_are_legal`
fail with `problems.py:402 — problem.get selectAcknowledges carries ['alias']`.
Wire tests (`TestProblemDetailAckAuthor`): the sent `selectAcknowledges` is a
subset of the legal set and excludes `alias`; the author resolves to
`username`; a rights-less token falls back to `user 42`; no `user.get` fires
without acks. 704 → 710.

## Consequences

- `get_problem_detail` works again, and now names the acknowledging user
  instead of `?`.
- The guard covers param *names* (ADR 072) **and** field *values* (this ADR),
  so both -32602 shapes are CI failures rather than live incidents.
- Tool count unchanged (163).

## Not included

- **Live A/B verification** against the production API. The check needed the
  MCP's credentials, and the machine's security hook correctly blocked
  "read credential file + network call" — the very confused-deputy pattern
  ADR 076 hardens against. It was not worked around. Correctness rests on the
  API's own enumeration of legal fields, the firing guard, and the wire
  tests; a live confirmation is one `/mcp` reconnect away.
- **Decoding the `action` bitmask** into human-readable acknowledge actions
  (close / severity change / suppress). The field is requested and available;
  rendering it is a separate improvement.
