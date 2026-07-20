# ADR 085: `get_users` -32602 (removed output field) + output-field guard

**Status:** Accepted
**Date:** 2026-07-17

## Problem

`get_users` was **dead on every call**. Confirmed live:

```
-32602: Invalid parameter "/output/…": value must be one of "userid",
"username", "name", "surname", "url", "autologin", "autologout", "lang", …
```

`user.get` was asked for `output: [… "type" … "rows_per_page"]`. The `type`
field (the old User / Admin / Super-admin user *type*) was removed in Zabbix
5.2 when role-based permissions replaced user types — it is now `roleid`;
`rows_per_page` is likewise gone from the user object. Requesting a removed
field in `output` makes Zabbix reject the whole call.

**Why every existing guard missed it:**
1. The API-contract guard (ADR 072) checks parameter *names*; the select-field
   guard (ADR 077) checks fields inside `select*` lists. **Neither validates
   the top-level `output` list.**
2. Even if it had, `get_users` builds `params = {...}` in a *variable* and
   passes it to `client.call("user.get", params)`. The AST scanners only
   inspected inline dict literals, so a variable-built params dict was
   invisible to them.

Both blind spots had to line up for this to ship; both did.

## Decision

**Fix the tool.** `output` now requests only fields the live API enumerates as
valid, plus `roleid`. Role *names* (which replaced the user-type labels) are
resolved with a separate `role.get` keyed by the collected `roleid`s;
best-effort, falling back to the raw id if `role.get` is not permitted, never
crashing.

**Close the class.** A new **output-field guard** in `tests/test_guards.py`:
- `DENIED_OUTPUT_FIELDS` maps a method to `output` fields Zabbix rejects
  (`user.get` → `{type, rows_per_page}`; extended as more are found).
- `iter_call_output_fields()` resolves a params dict passed **by variable**
  (nearest-preceding `name = {...}` assignment), not just inline literals —
  directly covering the second blind spot.
- A not-vacuous test asserts the scanner actually resolves `user.get`'s
  variable-built params and sees `host.get`'s inline ones.

## Test approach

`tests/test_users.py` (+5, wire-contract): the sent `user.get` output excludes
`type`/`rows_per_page` and is a subset of the legal set; the role name is
resolved via `role.get` and rendered; a rights-less `role.get` degrades to
`role <id>`; no `role.get` fires without roleids. Guard tests confirm the
denied field is caught and the variable-built params are seen. 759 → 766.

## Consequences

- `get_users` works again and shows real role names.
- The guard now covers param names (ADR 072), select-list fields (ADR 077),
  **and** top-level output fields — and sees variable-built params, not only
  inline literals. Tool count unchanged (163).

## Not included

- **A full positive allow-list of every object's output fields.** Too large
  and brittle; the deny-list of *known-removed* fields catches the real class
  cheaply.
- **`selectRole` inline** instead of a separate `role.get`. Avoided to not
  gamble another possibly-unsupported parameter; a plain `role.get` uses only
  well-established calls.
