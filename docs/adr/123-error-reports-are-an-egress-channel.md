# ADR 123 — An error report is an egress channel

**Status**: Accepted (2026-08-18)
**Affected**: `src/zbbx_mcp/logging.py` (`_scrub_value`, `_deny_terms`),
`tests/test_sentry_scrub.py`.
**Related**: ADR 119 (`ZBBX_SENSITIVE_STRINGS`).

## Context

Sentry reporting scrubbed a fixed list of credential words — `token`, `secret`,
`password`, `dsn`, `key`, `auth`, `credential`. Anything else in an exception
message was sent verbatim.

Error strings quote infrastructure constantly. *"connect to X failed"*, *"no
items on Y"*, *"host Z unreachable"* — the identifier is the useful part of the
message, which is exactly why it is in the message. So every one of those has
been leaving the process intact:

| Message | Sent as |
|---|---|
| `invalid token=…` | `[REDACTED]` |
| `connect to <address>:10050 failed` | **verbatim** |
| `host <name> unreachable` | **verbatim** |

This was found from a real report: a malformed tool call produced a validation
error whose text embedded the caller's argument — a live host address — and it
reached the service unredacted.

The gap is notable for *where* it was. Considerable effort has gone into
keeping identifiers out of the repository, its history, its tags and its
release notes. None of that touches a runtime channel that ships strings to a
third party on error.

## Decision

Redact anything address- or machine-shaped before an event leaves the process:

- **Addresses** (IPv4 and IPv6 literals) → `[IP]`
- **Hyphenated machine-shaped names** → `[HOST]`, with a small allow-list for
  ordinary hyphenated words that appear in error text (`read-only`,
  `content-type`, `timed-out`)
- **Two letters followed by digits** → `[HOST]`. A compound name puts the
  sibling in a bare trailing token with no hyphen, so a hyphen-based rule
  removes the parent and leaves the sibling — the *more* specific of the two.
- **Credentials** and **configured deny-list terms** → the whole string is
  dropped, since a secret's shape is unknown and partial redaction is not safe

The deny list is `ZBBX_SENSITIVE_STRINGS`, the same variable the fixture guard
uses. One configured list, two enforcement points: it keeps terms out of the
repository and out of error reports.

**Deliberately over-broad.** Redacting a harmless value costs a little
debugging detail; missing one ships infrastructure outside the system. The
trade is not symmetric, so the rule errs toward redaction — bounded by tests
that ordinary error text survives, because a message nobody can read is its own
failure.

## Consequences

- Reports keep the *shape* of an error while losing the identifiers, which is
  usually what debugging needs: `connect to [IP]:10050 failed` still says a
  connection to an agent port failed.
- Over-redaction will occasionally hit a hyphenated word not on the allow-list.
  Adding one is a one-line change; the reverse mistake is not recoverable.
- Structured `extra` fields are scrubbed by key name **and** by value, at any
  depth. Keying alone assumed a sensitive value always sits under a revealing
  name, but an address under `target` or `arg` identifies just as well as one
  under `host`. The recursion guard fails **closed**: strings are scrubbed at
  any depth and only an over-deep container is dropped, because a guard that
  passes the branch through unexamined is not a guard.

## Verification

Addresses, hyphenated names and compound siblings are removed and the original
value is asserted absent, not merely that a placeholder appeared. Credential
words and configured terms drop the whole string. Ordinary error text survives
verbatim. A not-vacuous test asserts the scrubber changes something at all,
without which every other assertion could pass on a no-op.
