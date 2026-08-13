# ADR 103: a swallowed enrichment failure is indistinguishable from an absent value

**Status:** Accepted
**Date:** 2026-08-13

## Problem

A colleague reported that `get_host` returned no cost for hosts that have one.
Their token was not Super admin; ours is.

The underlying rule is straightforward — host cost lives in the `{$COST_MONTH}`
user macro, so reading it needs `usermacro.get` **in addition to** host-group
read access, and those are separate permissions. A token that lists hosts
perfectly well can still return nothing for macros. (Verified against the Zabbix
docs: `usermacro.get` "is available to users of any type. Permissions to call
the method can be revoked in user role settings" — so the gate is host
visibility and the role's API-method list, not user type. Our `{$COST_MONTH}`
is *Text*, not *Secret*, so value masking is not involved.)

The defect is what the server did with that. `_host_context` (ADR 099) wraps
each enrichment block in `try/except … pass` so a failure can never break the
identity lookup. That intent is right. But `pass` meant a permission error and
"this host has no cost macro" produced **byte-identical output**: a host detail
with no Cost line and no explanation.

So the reader cannot distinguish:

- the macro genuinely is not set,
- the token may not read macros on this host,
- the role revoked `usermacro.get` outright.

Three very different situations, one indistinguishable rendering — and the
first is the one a reader will assume, because it is the mundane one. This is
the same confident-wrong-answer class the ADR 093–102 batch kept finding, this
time created by our own defensive `except: pass`.

## Decision

Enrichment stays best-effort — a failure still must not turn a working lookup
into an error — but it is no longer silent. Each block that fails records a
line in `ctx["_unavailable"]`, and the formatter renders them under a
**"Not shown"** heading with the explicit statement that these are missing
*because a call failed, not because the value is absent*, plus where to look
(host-group read permission, role API-method list).

Applied to all three fetched blocks: cost/bandwidth macros, current traffic,
and service-check state.

The README's token section now documents the three gates and, specifically,
that cost figures need `usermacro.get` on top of host read — the thing that is
easy to miss because listing hosts appears to work.

## Test approach

`tests/test_host_detail.py`: the existing "enrichment failure does not break
the lookup" test now asserts the host still renders **and** that the failure is
disclosed (it previously asserted `"Error" not in out`, which was too crude —
it matched the exception *type name*). Added the test that states the property
directly: a host with no macro and a host whose macros cannot be read must
produce **different** output, with only the latter carrying "Not shown".
895 → 896.

## Consequences

- A permissions problem now diagnoses itself from the tool output instead of
  looking like missing data.
- Reports built on these tools do not change: `_unavailable` is additive.

## Not included

- **Pre-flight permission probing.** Checking the role's API list up front
  would add a call to every request to pre-empt a rare failure; reporting
  accurately when it happens is cheaper and does not lie in the meantime.
