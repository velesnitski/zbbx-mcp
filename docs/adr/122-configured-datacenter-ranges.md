# ADR 122 — Datacenter ranges are configurable

**Status**: Accepted (2026-08-18)
**Affected**: `src/zbbx_mcp/classify.py` (`get_extra_dc_nets`,
`resolve_datacenter`), `tests/test_datacenter_override.py`.
**Extends**: ADR 120.

## Context

`resolve_datacenter` maps an address to `(provider, city)` using
`DATACENTER_CIDRS`, a table compiled into `classify.py`. It has the shape ADR
120 replaced for provider detection: a fixed allocation table where a mistaken
entry does not surface as unknown — it names a city, confidently, in output
someone acts on.

Unlike the provider table, it is not derived from a public dataset. Routing
data answers *which AS announces this prefix*, which is a different question
from *where this address is*, so the same generator cannot produce it.

## Decision

Add `ZABBIX_DATACENTER_CIDRS`, mirroring ADR 120's mechanism exactly:

- `{"Provider": [["a.b.c.d/n", "City, CC"], ...]}` as inline JSON or a file path.
- Consulted **before** the built-in table, most-specific-first.
- Unparseable input disables the override rather than half-applying it. A
  partial merge resolves some addresses against configured data and others
  against the built-in table, with nothing in the output saying which.
- Unset, no city is reported — see below.

The built-in table is **emptied**. A city mapping is deployment-specific, and
a table compiled into the package is both incomplete for any given deployment
and impossible to correct without a release. Unconfigured, `resolve_datacenter`
reports the provider and no city — an honest partial answer rather than a
guessed one.

`scripts/bootstrap_datacenter_ranges.py` drafts the file from a deployment's
own inventory. Many providers encode the facility in reverse DNS, so it groups
addresses into `/24` blocks, looks up one PTR per block, and proposes a city
when the name carries a known code. Unmatched blocks are marked `UNKNOWN` for a
human to fill in, and the script writes a draft rather than the live file
because a wrong city is reported as confidently as a right one.

## Low coverage is disclosed

Emptying the table creates a quieter problem: with nothing configured every
city is blank, and a blank column reads as "location unknown" rather than "not
set up". The same already applied to providers — a large `Other` count looks
like a finding when it is usually missing configuration, and a table of counts
cannot distinguish the two.

So both say which it is. `provider_coverage_note` fires when under 80% of
addresses resolved, and names either the setup script or, when an override is
already configured, the tool that proposes additions.
`datacenter_coverage_note` fires when no mapping exists at all.

This is the rule the rest of the codebase follows for absent values: an
unmeasured quantity must never render as though it were measured.

## Consequences

- `resolve_datacenter` has one more lookup pass, ordered ahead of the existing
  two. Its fallthrough is unchanged: built-in table, then provider-only
  detection, then `("Other", "")`.
- The city half of the answer becomes configurable independently of the
  provider half, which already was.
- An unconfigured install loses datacenter cities entirely. That is the
  intended trade: the shipped table could only ever be right for whoever wrote
  it, and the bootstrap script rebuilds a deployment-specific one in a step.
- Reverse DNS is a hint, not a source of truth. The script's code table is a
  starting point and its output needs review before use.

## Verification

Both configured forms accepted; configured ranges consulted ahead of the
built-in table; most-specific-wins among configured ranges; unusable input
disabling rather than half-applying; an unparseable address still answering
`("Unknown", "")`; an address outside every range reporting no city rather than
a guessed one. Plus a test pinning that the built-in lookup list is ordered
most-specific-first, since that ordering is what makes overlapping ranges
resolve correctly.
