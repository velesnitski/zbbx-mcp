# ADR 079: No deployment magnitudes in public docs

**Status:** Accepted
**Date:** 2026-07-13

## Problem

This repository is public; the deployments it is operated against are not.
Documentation written while working on a live system tends to drift into
quoting that system's **operational magnitudes** — how many hosts a tool
scanned, how wide an incident spread, which regions the estate spans. Those
figures are illustrative in prose but they describe someone's private
infrastructure, and they have no place in a public artefact.

The pre-push sensitive scan cannot catch this. It is a **string deny-list**
(product names, protocol names, hostname prefixes). Numbers and ISO country
codes are not strings on a list, so this entire class is invisible to it by
construction — the scan is doing exactly what it was designed to do.

## Decision

Add a **fleet-data guard** (`tests/test_guards.py`) that scans
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
distinct /24s") are design facts about this codebase, not descriptions of
anyone's estate, and keep passing.

Documentation prose therefore describes scale qualitatively — "every host in
the fleet", "most of its /24s", "several unrelated regions". The reasoning in
an ADR is what carries its value; concrete magnitudes were only ever
illustration, and generalising them costs the argument nothing.

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
