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
- Unset, behaviour is unchanged.

The built-in table stays as the default. Nothing about it is asserted to be
wrong here; the change is that a deployment can now correct or extend it
without waiting for a release.

## Consequences

- `resolve_datacenter` has one more lookup pass, ordered ahead of the existing
  two. Its fallthrough is unchanged: built-in table, then provider-only
  detection, then `("Other", "")`.
- The city half of the answer becomes configurable independently of the
  provider half, which already was.
- `DATACENTER_CIDRS` remains hand-maintained. Generating it would need a
  geolocation dataset rather than routing data, which brings a licensing
  question for a package that ships its data; not attempted here.

## Verification

Both configured forms accepted; configured ranges consulted ahead of the
built-in table; most-specific-wins among configured ranges; unusable input
disabling rather than half-applying; an unparseable address still answering
`("Unknown", "")`; an address outside every range reporting no city rather than
a guessed one. Plus a test pinning that the built-in lookup list is ordered
most-specific-first, since that ordering is what makes overlapping ranges
resolve correctly.
