# ADR 121 — Host-shaped example names are allow-listed, not pattern-matched

**Status**: Accepted (2026-08-18)
**Affected**: `tests/test_hostname_guard.py`.
**Related**: ADR 079 (qualitative scale), ADR 119 (fixture data).

## Context

Example host names appear throughout docstrings, fixtures and ADRs. They must
be invented rather than carried in from a running system — but *"is this name
invented?"* is not a question a test can answer by looking at the name.

Several syntactic rules were tried. Each was checked against a real inventory
and each was disproved:

- **"at most N digits"** — operational numbering is not confined to a digit
  width; short forms and long forms both occur.
- **"N or more digits is suspect"** — would reject the tests that exercise
  wide-number parsing, which need those widths to be meaningful.
- **"numbers in a reserved band are synthetic"** — no band is reserved in
  practice, so any band chosen on paper turns out to be in use.
- **"normalise, then bound the value"** — leading zeros make short and long
  forms collapse onto each other, so the bound admits the wrong ones.

The conclusion is not that a better rule exists. Operational numbering occupies
**the same shape space** as legitimate examples, so no property of the string
separates them. The distinguishing fact is not in the name — it is whether the
name exists somewhere else.

A deny-list fails for a different reason: it enumerates what is *forbidden*,
which is unbounded. Each sweep matches the patterns its author thought of, so
each new sweep finds something the previous one missed — a prefix gets checked
while numbering does not, a hyphenated form gets checked while the bare form
does not.

## Decision

Invert the question. Rather than *"does this look forbidden?"*, ask *"is this
one of the names we agreed to use?"* — a finite set, written down.

A token is **host-shaped** when it ends in a two-letter country code followed by
digits, optionally preceded by hyphenated segments. Every host-shaped token in
a tracked file must appear in `ALLOWED_HOSTS` (deliberate examples) or
`NOT_HOSTNAMES` (tokens that merely collide with the shape — worksheet
variables, hex colours, advisory identifiers).

The country-code set is **ISO codes plus the aliases `country.py` already
recognises**, because at least one code in common use is not an ISO code, and a
guard built on ISO alone silently ignores every name using it. The guard's own
not-vacuous test caught that before it shipped.

The cost is a deliberate edit: adding an example means adding it here. **That
edit is the control.** It is the moment to check the name against the live
system, which is the only check that works.

## Consequences

- A name copied from real infrastructure fails immediately, whatever its shape.
- The allow-list is reviewable in one place, and a companion test forces removal
  of entries no longer used, so the exemption cannot quietly widen.
- Verification needs the live system, so it happens at edit time by a human —
  not in CI, which has no credentials and should not have any.
- The guard cannot see host names carrying no country code. That shape has not
  appeared here; widening the matcher would trade precision for noise.
- **This document, and the guard, contain only invented names.** A write-up that
  illustrates the rule with real values defeats the rule — the first draft of
  this ADR did exactly that, and is why the point is stated here.

## Verification

The guard rejects invented names of the shapes that matter (multi-digit
numbering, prefixed forms, compound parent/child pairs), accepts the sanctioned
generics, fails on a dead allow-list entry, and asserts the country dataset is
loaded — without which every other assertion would pass vacuously.
