# ADR 094: `searchWildcardsEnabled` turns a bare term into an exact match

**Status:** Accepted
**Date:** 2026-07-27

## Problem

With `searchWildcardsEnabled: true`, Zabbix stops wrapping a `search` term
in `%…%` and only translates `*` into `%`. A literal containing no `*` is
therefore an **exact** match. No item key equals a bare prefix like
`net.if.in[`, so five call sites queried for a prefix, matched nothing, and
returned a fully-formed answer built from zero rows:

- `analyze_server_roles` classified the **entire fleet as "idle" at
  0.0 Mbps** (verified live against hosts measured at over 100 Mbps).
- `get_service_uptime_report`'s per-hour traffic gate (ADR 081) never
  engaged — `traffic_hours` was always empty, so it silently fell back to
  the window-wide boolean it was built to replace.
- `get_low_disk_servers` saw no `vfs.fs.*` items at all.
- `get_web_scenario_status` could never report a failure: response status
  was always absent, so `only_failed=True` returned nothing, always.
- `fetch_traffic_map`'s tag-based NIC discovery was dead, permanently
  falling back to the legacy fixed key list.

The codebase already knew the rule — the user-query paths wrap terms with
`q = s if "*" in s else f"*{s}*"`. Only the hardcoded literals missed it.

## Decision

Make the wildcards explicit at all five sites (`*<term>*`), matching the
existing idiom, and add an AST guard: any `client.call` dict literal that
sets `searchWildcardsEnabled: True` alongside a literal `search` value with
no `*` fails the suite. Inline literals only — the variable-built sites are
the ones taking a caller's query, and they already wrap it.

## Test approach

`tests/test_guards.py::TestSearchWildcardGuard` — every literal search term
under the flag carries a `*`, plus a non-vacuity assertion that the scanner
actually reaches the guarded call sites.

## Consequences

- Five tools return real data instead of a confident empty result. The
  uptime traffic gate works as ADR 081 intended.
- The failure mode was invisible precisely because it produced no error;
  the guard makes it a CI failure instead.
