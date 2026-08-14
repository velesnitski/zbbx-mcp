# ADR 106 — The cost survives a revoked `usermacro.get`

**Status**: Accepted (2026-08-14)
**Affected**: `src/zbbx_mcp/tools/hosts.py` (`COST_FALLBACK_KEY`,
`_host_context`), `src/zbbx_mcp/formatters.py`, `tests/test_host_detail.py`.
**Extends**: ADR 103.

## Context

ADR 103 made a denied macro read *visible* — `get_host` now says the call
failed instead of rendering the host as costless. That is honest, but it still
leaves the reader without the number they asked for, and the trigger was a
colleague on a restricted role who simply wanted a host's cost.

The monthly figure turns out to be published **twice**: as the
`{$COST_MONTH}` user macro, and as a plain item, `Cost_macros_present`.
Reading an item needs only host-group read plus `item.get` — not
`usermacro.get`, which a Zabbix role can revoke independently. So the answer
was reachable all along by a path the restricted token already has.

Checked before trusting it, on two hosts including a decimal value:

| host | `{$COST_MONTH}` | `Cost_macros_present` |
|---|---|---|
| A | 16.00 | 16 |
| B | 32.49 | 32.49 |

## Decision

When the macro yields no cost — whether the call failed or the macro is absent
— `_host_context` reads `Cost_macros_present` and uses its `lastvalue`.

- **The source is always carried into the output.** The value renders as
  `**Cost/month:** 16 _(via item `Cost_macros_present`)_`. A substituted value
  must never look identical to the real one: the item is a polled snapshot and
  can lag a macro edit, so silently presenting it as the macro would make this
  fallback the next confident wrong answer — the exact failure ADR 103 exists
  to remove.
- **The ADR 103 disclosure stays.** A permission failure still appears under
  *Not shown*, even when the fallback succeeded, so the underlying
  role problem remains visible instead of being papered over by a working
  number.
- **Only on the miss path.** The common case still costs one call, and a
  readable macro always wins.
- An unparseable `lastvalue` is skipped, not guessed.

## Consequences

- A restricted token gets the number it asked for, with a caveat it can act on.
- Two sources for one figure can disagree. The label is what makes that
  survivable: a reader who sees `via item` knows which one they are looking at
  and can go check the macro.
- Not addressed: nothing verifies the item and the macro still agree fleet-wide.
  A drift check belongs with the cost tooling (`get_cost_gaps` and friends),
  not in a per-host lookup.

## Verification

925 tests pass (+5): denied macro falls back and labels the source while still
disclosing the failure; absent macro falls back; a readable macro wins and
issues no fallback call at all; no cost anywhere stays silent; an unparseable
value is not guessed.
