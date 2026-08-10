# ADR 102: `cryptography` Bleichenbacher oracle — and the second advisory the sweep found

**Status:** Accepted
**Date:** 2026-07-30

## Problem

A High advisory was reported against this repo's lockfile:

- **GHSA-g6cj-pr64-35w5** / CVE-2026-69247 — *cryptography: PKCS#7
  EnvelopedData decryption exposes a Bleichenbacher oracle through
  distinguishable errors and timing.* Vulnerable `>= 44.0.0, < 50.0.0`;
  the lock carried **49.0.0**.

**Are we exploitable?** No. Nothing in `src/` or `tests/` imports
`cryptography` at all, let alone the PKCS#7 decryption API the oracle lives in
— the padding-oracle needs an attacker able to submit chosen ciphertexts to a
decryption endpoint, and this server exposes none. The package is present for
two reasons: it arrives transitively via `pyjwt[crypto]` ← `mcp`, and it was
promoted to a *declared* dependency earlier purely as a security floor after a
previous advisory. We shipped the vulnerable version without being able to
reach the vulnerable code.

That is still worth fixing, on the same reasoning as ADR 083: a dependency we
ship is part of our supply chain whether or not our own call graph touches it,
and "not exploitable *today*, given our current call graph" is a statement with
an expiry date.

**The second finding.** ADR 083's lesson was that the reported CVE was only the
tip — so the lock was swept in full rather than patched at the named package.
`pip-audit` could not run in this environment (its isolated-venv builder dies in
`ensurepip`), so all 117 runtime packages were queried against the GitHub
Advisory Database instead — the same source that raised the original alert. That
turned up one more:

- **GHSA-6hr6-w5qg-qmwg** (medium) — *h2: duplicate `Host` header could
  facilitate request smuggling.* Vulnerable `<= 4.4.0`; the lock carried
  **4.3.0**.

This one is **closer to us than the reported High**: `h2` arrives through
`httpx[http2]`, and this client sets `http2=True`, so the vulnerable code is
genuinely on our request path. It was not in the alert.

## Decision

- `cryptography`: raise the declared floor `>=46.0.7` → **`>=50.0.0`** and
  re-lock (49.0.0 → 50.0.0). Raising the floor rather than only re-locking
  encodes the constraint, so a future resolution cannot quietly land back on a
  vulnerable version.
- `h2`: re-lock to **4.4.1** (pulling `hpack` 4.2.0 with it). Deliberately **not**
  promoted to a declared dependency. It is transitive, nothing caps it, and the
  lockfile already pins it; inventing a direct dependency to floor a transitive
  package adds a maintenance surface that becomes the next over-tight cap —
  precisely the anti-pattern ADR 082 was written about, where our own ceiling
  blocked the fix we needed.
- Verified after the change: **0 advisories across all 117 runtime packages.**

## Consequences

- Both advisories cleared; the sweep, not the alert, is what made that true.
- Recorded for the next time: `pip-audit` is unusable on this machine, so the
  fallback is `gh api /advisories -f ecosystem=pip -f affects=<pkg>@<ver>` over
  the exported requirements. It is authoritative (same database as the alert)
  and needs no local venv.

## Not included

- **Dropping `cryptography` as a declared dependency.** It is now doing real
  work as a floor; removing the declaration would hand version choice back to
  whatever `pyjwt[crypto]` happens to resolve.
- **Adding a CI advisory gate.** Worth doing, but it needs a scanner that works
  here — the one this repo reached for does not, which is itself the finding.
