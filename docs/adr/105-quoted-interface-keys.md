# ADR 105 — An entire template was invisible to every traffic tool

**Status**: Accepted (2026-08-14)
**Affected**: `src/zbbx_mcp/fetch.py` (`is_physical_traffic_in_key`),
`src/zbbx_mcp/tools/traffic_shaping.py` (item discovery + disclosure),
`tests/test_diagnose.py`, `tests/test_traffic_shaping.py`.
**Extends**: ADR 078 (one definition of "carries inbound traffic"), ADR 103.

## Context

Reported from the field: three hosts with an obvious traffic anomaly, and
`detect_traffic_shaping` returned *"No host is pinned against a throughput
ceiling"*. The data was not ambiguous — one of them ran 70–177 Mbps peaks until
a cliff, then 1–20 Mbps afterwards, a ~98% collapse. The tool had simply never
looked at those hosts, and its output gave no hint of that.

Two independent causes, both of which made "not measured" render as "nothing to
report":

**1. Quoted interface names.** Zabbix's stock *Linux by Zabbix agent* template
emits `net.if.in["enp3s0"]`; the in-house template emits `net.if.in[eth0]`.
`is_physical_traffic_in_key` did `key.split("[", 1)[1].rstrip("]")`, which
strips the bracket but not the quotes, so the interface token was `"enp3s0"`
— quote included — and failed `startswith(("eth", "eno", "enp", …))`. Every
host on the stock template was therefore invisible to **every** traffic tool
built on ADR 078's shared predicate. `net.if.in["eth0"]` returned False.

**2. Discovery by item NAME.** `detect_traffic_shaping` (inherited from
`detect_traffic_erosion`) searched `item.get` for the item *name* `"Incoming
network traffic"`. The stock template calls the same metric `"Interface enp3s0:
Bits received"`. A name search examines one fleet and reports silence for the
other.

## Decision

- The shared predicate unquotes the interface token before the prefix test.
  This is the right place: it is ADR 078's single definition, so one fix
  restores every key-based consumer at once. The virtual-interface exclusions
  are unaffected and pinned by test — `docker0`, `tun0`, `veth0`, `lo` stay
  out whether quoted or not.
- `detect_traffic_shaping` discovers by **key** (`*net.if.in[*` +
  `is_physical_traffic_in_key`), never by name. Item names are template
  cosmetics; the key is the contract.
- A host in scope that could not be examined is **named in the output**:
  *"N host(s) in scope had no usable physical-NIC trend data and were NOT
  examined … Absent from this table means unmeasured, not healthy."* This is
  what would have caught both bugs on day one — the result looked clean, which
  is the only reason it survived.

## Consequences

- Still name-based, and so still blind to the stock template:
  `tools/traffic.py:558`, `tools/geo_traffic.py:59`,
  `tools/dashboard_report.py:119`, `tools/traffic_erosion.py:307`,
  `fetch.py:533`. The predicate fix does not reach them, because their
  `item.get` never returns those items in the first place. Each needs the same
  key-based switch; deferred rather than done blind, since four of them have
  their own scoping tests and one wrong move silently changes what the fleet
  reports.
- `fetch_traffic_map` is already tag/key-based and is fixed by the predicate
  change alone.

## Verification

925 tests pass (+11). The load-bearing ones: quoted physical keys recognised,
quoted virtual keys still rejected (the fix must not widen the filter), the
wire test asserting `search.key_` with `searchWildcardsEnabled` and no `name`
key, and the disclosure test for an unexaminable host.
