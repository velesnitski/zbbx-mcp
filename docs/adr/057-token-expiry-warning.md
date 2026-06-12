# ADR 057: Token-expiry early warning in `check_connection`

**Status:** Accepted
**Date:** 2026-06-12

## Problem

An expired API token kills every authenticated tool at once — the same
all-at-once failure mode the 7.2 `auth` removal just demonstrated
(ADR 055), except with no code fix available: the operator simply
discovers one day that everything returns auth errors. Zabbix exposes
`token.get` (5.4+) with per-token `expires_at`, but nothing in the server
ever looked at it.

## Decision

Teach `check_connection` — the tool an operator naturally reaches for
first — to warn when any **enabled** API token expires within 30 days,
listing name and days-left (or "EXPIRED Nd ago"). Never-expiring
(`expires_at == 0`) and disabled tokens are skipped.

Degradation is silent by design: `token.get` may be unavailable (pre-5.4)
or denied to the token's role; the connection check's primary answer must
not become noisy or fail because the bonus check could not run. The
`token.get` call is wrapped and any API error ignored.

The selection logic lives in a pure helper `summarize_token_expiry(
tokens, now, warn_days=30)` (sorted soonest-first), unit-tested without a
server.

## Test approach

Four pure-helper tests (`TestSummarizeTokenExpiry`): expiring tokens
flagged and sorted soonest-first; never-expiring and disabled skipped;
far-future not flagged; already-expired reported with negative days.
The wiring is config-level over the tested helper.

## Consequences

- Tool count unchanged (161). Tests +4 (570 → 574).
- `check_connection` cost rises by one cheap `token.get` per call.
- The operator hears about a dying token weeks ahead, from the tool they
  already run when anything feels off.

## Not included

- **Identifying *this* server's own token.** Zabbix stores token values
  hashed, so the running token cannot be matched to a `token.get` row.
  Warning on every visible expiring token is strictly more useful anyway:
  Super Admin tokens see the whole instance's inventory.
- **A standalone `get_api_tokens` tool.** A read-only token inventory is
  trivial to add if ever needed; the warning is the part with operational
  value.
