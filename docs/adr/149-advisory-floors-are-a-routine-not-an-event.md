# 149. Advisory floors are a routine, not an event

## Status

Accepted (v1.16.71)

## Context

Nine days after ADR 145 closed one advisory on a transitive dependency,
sixteen more opened on two others: a JSON web token library pulled in by
the MCP SDK's authentication support, and the HTTP library behind the
error-reporting client. Neither is imported by this code. Thirteen of the
sixteen were for one package and one of those thirteen has no fixed
release yet.

ADR 145 argued for a declared floor over a lock-only bump, because a lock
re-resolves and a floor does not. It treated the case as singular. Two in
nine days is a rate, and the handling should not require an argument each
time.

## Decision

**Each advisory on a transitive package gets a floor at the first fixed
version and a re-resolved lock, in one commit, with the suite as the proof
that the upgrade is inert.** The floor is declared in `pyproject` next to
the others with the ADR that introduced it. An advisory without a fixed
release stays open and is named here rather than hidden behind a floor
that cannot exist yet.

**The advisory list is checked at every release**, not when GitHub
notifies, so a floor is added before the alert count grows.

## Consequences

Two floors added: the token library at 2.15.1 and the HTTP library at
2.8.0. Fifteen alerts close; the one without a patched version remains
open until upstream ships one, and this file is where that is recorded.
