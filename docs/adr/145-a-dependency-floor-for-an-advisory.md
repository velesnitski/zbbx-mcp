# 145. A dependency floor for an advisory

## Status

Accepted (v1.16.68)

## Context

GitHub flagged the lock file: `anyio` below 4.14.2 carries two published
vulnerabilities, one rated critical. This project never imports `anyio`
directly; it arrives through the MCP SDK and the HTTP client, and the lock
had resolved it to 4.12.1. A transitive dependency is still a dependency
the shipped server runs on.

ADR 083 set the pattern for the SDK's own advisories: raise the floor in
`pyproject`, not only in the lock, so that an install from source cannot
resolve back below the fixed version, and record the decision.

## Decision

**`anyio>=4.14.2` is a declared floor.** The lock is re-resolved with the
package upgraded; on the interpreters this project supports it resolves to
4.14.2, and to a later line where a newer interpreter requires it. Nothing
in the code changes: the suite, the linter and the type checker are the
evidence that the upgrade is inert for this server.

**The floor lives in `pyproject`, the resolution in the lock.** A lock-only
bump silently expires the next time anyone re-resolves without the
upgrade; a floor does not.

## Consequences

The advisory closes on this repository. The same transitive package sits
in every Python server of the fleet built on the same SDK; each needs the
same one-line floor and a re-resolved lock, checked repo by repo rather
than assumed.
