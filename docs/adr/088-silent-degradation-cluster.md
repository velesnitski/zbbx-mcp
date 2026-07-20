# ADR 088: Silent-degradation cluster — removed/renamed API fields

**Status:** Accepted
**Date:** 2026-07-17

## Problem

A live audit (the API-contract pass behind ADR 085) flagged five removed/renamed
Zabbix fields. Empirically, on this 7.4.9 instance, unknown `output`/`select`
fields are **silently ignored** rather than rejected — so four of the five did
not crash like `user.get` (ADR 085) but instead made a column permanently
blank/wrong. Confirmed live: `search_hosts`, `get_discovery_rules`,
`get_domain_status` all succeeded, they just returned nothing for the affected
field. Four such defects:

1. **`host.get` output `available`** (`search_hosts`, `get_capacity_planning`).
   The host object lost `available` in Zabbix 6.0 — passive availability moved
   to the interface; the host-level field is now `active_available` (same 0/1/2
   encoding). The `[available]`/`[unavailable]` tag and the "agent unavailable"
   count were therefore never populated.
2. **`discoveryrule.get` output `lastclock`** (`get_discovery_rules`). An LLD
   rule object has no `lastclock`, so the "last run" column showed a permanent
   `1970-01-01`.
3. **`item.get` `selectHosts: [..., "groups"]`** (`get_domain_status`). Host
   groups cannot be nested inside a `selectHosts` subquery (the same class as
   ADR 077), so the domain "Group" data was always empty.
4. **`maintenance.get` `selectGroups`** (`get_maintenance`). Renamed to
   `selectHostGroups` in 7.0. It happens to still work on 7.4.9, but the
   client's 6.x→7.2 shim (`_GROUP_SELECT_METHODS`) did not cover
   `maintenance.get`, so it was fragile across Zabbix variants.

## Decision

- **`available` → `active_available`** in both host outputs and the two
  consumers (`format_host_list`, `get_capacity_planning`). Same encoding, no
  extra calls.
- **Drop `lastclock`** from the discovery-rule output and its render — LLD
  rules carry no last-poll timestamp.
- **Drop `"groups"` from the item's `selectHosts`** and resolve host groups in
  one separate `host.get` with `selectGroups` (translated by the shim), keyed
  by the item hosts — so the Group data actually populates.
- **Add `maintenance.get`** to the client's `_GROUP_SELECT_METHODS` shim, so
  `selectGroups`↔`selectHostGroups` and the `groups`↔`hostgroups` response
  aliasing apply there too.
- **Extend the output-field guard** (ADR 085) with `host.get: {available}` and
  `discoveryrule.get: {lastclock}` so neither can return.

## Test approach

`test_api_contract_fixes.py` (+4, wire): `search_hosts` requests
`active_available` not `available` and still renders `[available]` in list
mode; `get_discovery_rules` omits `lastclock` and the "last:" column;
`get_domain_status`'s `item.get` selectHosts drops `groups` and a separate
`host.get` with `selectGroups` resolves them. `test_client.py` (+1): the shim
translates `maintenance.get selectGroups`. Guard now denies the two output
fields. 774 → 779.

## Consequences

- Availability, discovery last-state, and domain-group columns reflect real
  data (or are honestly absent), and `get_maintenance` is portable across 6.2+
  and strict 7.2+. Tool count unchanged (163).

## Not included

- **Interface-level host availability.** `active_available` is the host-level
  active-agent field; per-interface passive availability (as `availability.py`
  already reads) is a fuller but larger change, deferred.
