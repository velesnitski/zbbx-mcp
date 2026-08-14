# ADR 109 — One definition of "find this host's traffic items"

**Status**: Accepted (2026-08-14)
**Affected**: `src/zbbx_mcp/fetch.py` (`physical_traffic_items`,
`is_physical_traffic_out_key`, `TRAFFIC_*_KEY_SEARCH`), `tools/traffic.py`,
`tools/traffic_erosion.py`, `tools/traffic_shaping.py`, `tools/geo_traffic.py`,
`tools/geo_health.py`, `tools/analysis.py`, `tools/dashboard_report.py`,
`tests/test_guards.py`, `tests/test_diagnose.py`.
**Extends**: ADR 078, ADR 094, ADR 105.

## Context

ADR 105 fixed traffic discovery in one tool and listed five other call sites
still searching by item **name** — blind to Zabbix's stock *Linux by Zabbix
agent* template, whose items are named `Interface enp3s0: Bits received` rather
than `Incoming network traffic on enp3s0`. Fixing them one at a time was the
plan.

Writing the guard that would stop a sixth copy appearing found **three more**
the list did not know about: `analysis.py`, `geo_health.py`, and
`traffic_shaping.py` — the tool ADR 105 had just fixed. Those three were
already key-based, so they were not blind; but that is eight independent copies
of one rule, which is why a fix to any one of them reaches only that one.

The pattern is the same as ADR 078, which exists because two definitions of
"physical NIC" had drifted apart. Discovery had drifted the same way, one level
up.

## Decision

`fetch.physical_traffic_items(client, hostids, direction=…, output=…)` is the
single definition. All eight sites call it.

- Discovery is by **key**, never by name. Item names are template cosmetics;
  the key is the contract.
- `key_` is always requested regardless of `output` — the physical filter needs
  it, and a caller that forgot would silently get everything back unfiltered.
- `is_physical_traffic_out_key` is added so the outbound half of the
  `fetch.py` fallback goes through the same rule instead of a seventh variant.
- A guard in `tests/test_guards.py` fails the build if any `item.get` searches
  traffic by name again, or if a raw `*net.if.in[*` search reappears outside
  the helper. The failure this prevents is silent and looks like a clean
  report, which is exactly the kind that survives review.

### The guard that consolidation weakened

`TestSearchWildcardGuard` scans **inline literals** for the ADR 094 mistake (a
term without `*` becomes an EXACT match under `searchWildcardsEnabled` and
returns nothing). Building the helper's term as an f-string made the last
remaining traffic search invisible to it, and its own vacuity check caught the
drop — the guard noticed its coverage shrinking.

Rather than lower a threshold quietly: the terms are now module-level string
constants, the vacuity floor moved 4 → 3 with the reason recorded inline, and
the lost assertion is made directly in `TestTrafficDiscoveryGuard`. Total
coverage is unchanged; only its location moved.

## Consequences

- Any host on the stock Linux template is now visible to every traffic tool,
  not just `detect_traffic_shaping`.
- Three sites gained the physical-interface filter they never had
  (`geo_traffic`, `dashboard_report`, and the `fetch.py` fallback previously
  matched by name and kept `docker0`/`tun*` alongside real NICs), so their
  numbers get *more* accurate — worth watching on the first run.
- The remaining task-181 work is done in one change rather than five, because
  the fix turned out to be deduplication, not six separate edits.

## Verification

943 tests pass (+8). The guard is proven non-vacuous by construction: it found
the three unlisted sites before any of them were converted.
