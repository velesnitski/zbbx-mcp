# ADR 112 — API tokens had no surface, so the audit went around the server

**Status**: Accepted (2026-08-14)
**Affected**: `src/zbbx_mcp/tools/tokens.py` (new), `tools/__init__.py`,
`tests/test_tokens.py`, README/CLAUDE.md counts.
**Extends**: ADR 103.

## Context

A Zabbix token audit had to bypass this server entirely and call `token.get`
by hand, because no tool covered API tokens. What it found is exactly what a
routine tool would have surfaced without anyone going looking: expired
temporary tokens still present, several tokens with **no expiry at all**
(including a service token unused for over a year), and a token named `test`
sitting on a personal account.

Every other object in this fleet is monitored because it might *break*. A token
is different: the finding is its **absence of use**. A key nobody has touched in
a year is not idle capacity — it is an unrevoked credential, and nothing was
watching for it.

## Decision

`get_api_tokens` — expiry, last use, and owner, ranked by risk rather than by
name.

**The trap the pure core exists to avoid**: Zabbix encodes "never" as **0** in
both `lastaccess` and `expires_at`, and 0 is also a valid timestamp meaning
1970. Treating it arithmetically makes a never-used token read as ~19,000 days
idle — or, once sorted, as the *most recently used* one — and makes a token that
never expires read as long expired. Both are separate states here, pinned by
test, because either confusion inverts the exact signal the tool is for.

Risk order, worst first: expired-but-present; permanent key never used;
permanent key gone stale; never used; permanent; stale. **A disabled token
ranks below every live one** regardless of its other flags — it cannot be used,
and ranking it on those flags would push real keys off the top of the list.

Failure handling follows ADR 103. `token.get` is Super-admin-only unless a role
grants it, and a denied call returns *"this is a permissions answer, NOT 'there
are no tokens'"*. An empty token list would otherwise be the most reassuring
possible output for the least trustworthy possible reason. Owner lookup is a
separate, softer failure: names are a nicety, so a denied `user.get` still lists
the tokens by id rather than blanking the audit.

## Consequences

- Token hygiene becomes a thing you can check on any run instead of a one-off
  manual sweep.
- Deliberately read-only. `delete_api_token` was offered by the task and left
  out: revoking a credential is not a step to take from a summary table, and
  the read tool is what was actually missing.
- 167 → 168 tools. Not added to a tier preset — this is periodic hygiene, not
  incident response, and `full` is where it belongs.

## Verification

964 tests pass (+13). The load-bearing ones are the two zero-handling cases
(never-used is not ancient, never-expires is not long-expired), the
disabled-ranks-last rule, and the denied-`token.get` wording.
