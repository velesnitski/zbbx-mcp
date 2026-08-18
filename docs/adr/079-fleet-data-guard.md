# ADR 079: Documentation describes scale qualitatively

**Status:** Accepted
**Date:** 2026-07-13

## Problem

Documentation written while working against a running system tends to absorb
that run's numbers — how many hosts a tool scanned, how wide an incident
spread, which regions appeared. Those are **one execution's output**, not facts
about this codebase. They date the instant anything changes, a reader cannot
verify them, and the argument they illustrate reads exactly the same without
them.

A string deny-list cannot catch this class at all: numbers and ISO country
codes are not strings on a list, so they are invisible to it by construction.

## Decision

Add a **magnitude guard** (`tests/test_guards.py`) that scans
`docs/adr/*.md`, `CHANGELOG.md`, `README.md` and `CLAUDE.md` for what the
string scan cannot see:

- `fleet of <n>`;
- *observed* counts — `returned|ranked|showed|found|reported|analysed <n>
  hosts|servers|nodes|clusters`;
- any deployment-scale count (three or more digits) of hosts/servers/nodes;
- subnet-spread counts (`<n> /24s`);
- **regional footprints** — two or more ISO-2 country codes in a row
  (`XX / YY`), validated against the repository's own ISO-3166 dataset rather
  than a hardcoded list.

The guard is deliberately scoped to *observed* magnitudes. Configured
thresholds and caps ("capped at N hosts per call", "fires when ≥N hosts on ≥M
distinct /24s") ARE design facts about this codebase and keep passing.

Documentation prose therefore describes scale qualitatively — "every host in
scope", "most of its /24s", "several unrelated regions". The reasoning in an
ADR is what carries its value; concrete magnitudes were only ever illustration,
and generalising them costs the argument nothing while making it age better.

## Test approach

`TestFleetDataGuard` (+3): each banned shape is flagged, the country pattern is
validated against the real ISO-2 set, and a not-vacuous test proves the
patterns actually fire. Its fixtures are **synthetic by construction** — a test
that hardcoded real magnitudes would put into the repository precisely what the
guard exists to keep out of it, so the country fixture draws its codes from the
ISO dataset at run time rather than naming any. 721 → 724.

## Consequences

- Deployment scale and geography cannot reach a public artefact through prose;
  CI fails instead of a reviewer having to notice.
- The pre-push scan is now understood as **necessary but not sufficient**: it
  covers strings, this guard covers magnitudes. Neither replaces the other.

## Not included

- **Two-digit observational counts.** By regex alone these are
  indistinguishable from configured thresholds ("50 hosts per call"), so they
  remain a review concern rather than a CI failure.
- **Non-prose surfaces.** The guard reads documentation. Test fixtures and
  source constants are governed by the existing scan and by review.
