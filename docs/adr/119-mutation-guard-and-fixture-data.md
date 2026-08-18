# ADR 119 — Smart tests: a mutation guard, and fixtures as published data

**Status**: Accepted (2026-08-17)
**Affected**: `tests/test_mutation_guard.py` (new),
`tests/test_fixture_data_guard.py` (new), test fixture addresses.

## Context

Two questions the suite could not answer about itself.

**Does it pin anything?** A green run proves the tests execute, not that they
hold behaviour down. Three defects in a single day were all of that shape: a
threshold that let the real case through, a ported rule that scored a fully
dead protocol as perfect, and a name that silently shadowed a dict. Two were
caught by a synthetic control written by hand; one by luck.

**Is the fixture data synthetic?** A fixture carrying an address from some real
network is a portability bug waiting to happen: it can collide with a host the
test runner can actually reach, it makes the result depend on where the suite
runs, and it dates the moment that network is renumbered. Addresses in tests
should be *obviously* invented. The existing docs guard covered only
`CHANGELOG`, `README`, `CLAUDE.md` and the ADRs — `tests/` was never in scope.

## Decision

### Mutation guard

Off-the-shelf mutation testing re-runs the whole suite per mutant — hours for a
thousand tests, which cannot share a CI job. So narrow the question rather than
the rigour: mutate only the pure functions where the decisions live, and check
each mutant against **oracles stated in the guard itself**. No subprocess, no
suite re-run. **Roughly 0.7s for 51 mutants across three modules.**

Operators are semantic, not noise: comparison flips, boolean `and`/`or` swaps,
numeric-constant doubling, boolean negation. A mutant that passes every oracle
*survived* — that behaviour is not pinned, and the guard names the exact
function, line and mutation.

Site counts are **pinned per target, not floored**. A drop means a branch or
threshold was deleted and the guard silently got easier; a rise means new
decisions arrived that nobody wrote an oracle for. Either way the number
deserves a look rather than drift.

It found a gap immediately — in its own oracles. Two mutants of the score clamp
survived, and killing them needed a case with a **partial** uptime: with
full-uptime protocols a doubled scale saturates back to 100 and every other
assertion still holds. That case is now an oracle.

### Fixture data guard

Two layers, because one half is universal and the other is per-deployment.

**Structural, always on.** Addresses in fixtures must come from the private,
special-purpose or RFC 5737 documentation ranges. A bright line beats a
judgement call: *"is this address real?"* invites an argument, *"is it from a
documentation range?"* does not. The four conventional dummies already present
were migrated to `192.0.2.x` / `198.51.100.x`. Multicast and reserved ranges are
allowed because they can never be a host address and fixtures use them as
invalid-input samples. The magnitude patterns now cover `tests/` too.

**Configurable, per deployment.** Identifiers specific to whoever runs this
server cannot be enumerated in the package itself. They come from
`ZBBX_SENSITIVE_STRINGS` (a file path, or an inline comma-separated list) and
are enforced whenever it is set. Unset, the test **skips loudly** rather than
passing silently, because a guard that quietly does nothing is worse than none:
it reads green.

When it fires it names the offending **files but never the term** — CI output
is not a place to repeat a configured term back.

Both guards exempt their own files. Each must contain deliberately invalid
samples to prove it is not vacuous, and scanning them would make every guard
fail on its own evidence.

## Consequences

- Adding a decision to a guarded module now requires an oracle, or the pinned
  site count fails and says so.
- A real address can no longer reach a fixture unnoticed, whatever the author
  intended.
- The deny-list is available locally and in a private CI without the terms ever
  entering this repository.
- Not covered, and deliberately: protocol and product names in fixture keys
  cannot be checked structurally without naming them. That is what layer two is
  for.

## Verification

1035 tests pass, 1 skips by design. The guards' own not-vacuous tests: a
deliberately broken implementation must be rejected by the oracles, the address
rule must reject something just outside each allowed range, and the deny-list
parser handles both configured forms.
