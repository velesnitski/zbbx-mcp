# ADR 098: one traffic conversion everywhere — plus the cycle and the search term it exposed

**Status:** Accepted
**Date:** 2026-07-28

## Problem

ADR 087 established a single raw→Mbps divisor (`TRAFFIC_DIVISOR`, which
honours `ZABBIX_TRAFFIC_UNIT`) — but only routed `get_peak_analysis` through
it. **24 call sites across 9 modules kept dividing by a literal `1e6`.**

That is correct only for the default bits/s configuration. Under a bytes/s
deployment every one of them reads **8× low**. Ratio-based verdicts survive
(both sides carry the same error), but **absolute thresholds do not**: a host
genuinely doing 30 Mbps reads `3.75`, falls under the 5.0 Mbps
`min_baseline` gate, and is classified `ARTIFACT` — dropped from block
analysis entirely, with no row and no warning. The same floor guards
`detect_disruption_wave` and the acute regional detector. Two tools that had
been migrated would meanwhile report figures 8× apart from the rest for the
same host.

The regression lock was equally narrow: `test_traffic_units.py` asserted the
rule for `executive.py` and nothing else.

Fixing this surfaced two further defects:

- **A latent import cycle.** `data` and `fetch` import each other; `data`
  re-exported fetch's symbols eagerly at module scope. So
  `import zbbx_mcp.fetch` **as the first import raised ImportError** — a
  partially-initialised module. It had never fired only because every entry
  point happens to import `data` first. Any new module importing `fetch`
  directly would have hit it.
- **A sixth instance of the ADR 094 wildcard bug.** `get_predictive_alerts`
  holds its search term in a config dict and assigns it via
  `params["search"] = cfg["search"]`, so the ADR 094 scanner — which reads
  `client.call` dict literals — could not see it. Its disk metric queried an
  exact key, fetched **zero items, and the entire disk forecast never ran.**
  Combined with the sign inversion fixed in ADR 097, disk prediction was
  dead twice over.

## Decision

- Add named converters beside the divisor: `to_mbps()`, `to_kbps()`,
  `from_mbps()`. Named rather than inlined so the conversion is greppable,
  self-documenting at the call site, and — critically — so a bare literal
  becomes guardable. They tolerate the `None`/`""` junk Zabbix returns.
- Sweep all 24 sites across `traffic`, `disruption`, `diagnose`,
  `ip_history`, `geo_traffic`, `inventory_load`, `dashboard_report`,
  `analysis` and `predictive`.
- Widen the regression lock from one file to an **AST sweep of the whole
  tool tree** for the literals `1e6` / `1_000_000` / `125_000`. Comments are
  invisible to the AST, so the historical notes explaining the old constants
  do not trip it. `1e9` and `1000` are deliberately absent — bytes→GB and
  Mbps→Gbps are legitimate. The one genuine collision (a memory rate that
  also divides by 1_000_000) is resolved by naming it: `MB_DECIMAL` /
  `GB_DECIMAL` in `data.py`.
- Break the import cycle with PEP 562: `data.__getattr__` resolves the fetch
  re-exports **lazily**, on first attribute access, so there is no
  import-time edge at all. A `TYPE_CHECKING` block declares the same names
  for linters and type checkers without executing at runtime.
- Fix the predictive search term, and extend the wildcard guard to the
  **variable-built** form (`params["searchWildcardsEnabled"] = True`) that the
  inline scanner structurally cannot see. Scoped to exactly that form —
  including modules that set the flag inline would false-positive on their
  unrelated bare-term searches, which are correct substring matches.

Also in scope, from the same review: **`detect_disruption_wave` summed every
matching NIC per host.** `bond0` and its slaves are all in `TRAFFIC_IN_KEYS`,
but the bond *is* its slaves, so a bonded host counted twice — halving the
effective `min_baseline` floor. Worse, baseline and recent were accumulated
independently, so an interface with baseline rows but no recent rows (renamed
item, removed NIC) inflated only the baseline side and **manufactured a drop
that never happened**. Now: max across interfaces (matching `build_max_map`,
which `fetch.py` already uses for this key list), over interfaces present in
**both** windows.

## Test approach

`tests/test_traffic_units.py` — the tree-wide AST guard, a non-vacuity check,
and helper round-trips including junk input. `tests/test_traffic_aggregation.py`
(+11) — the cycle is proven in a **fresh interpreter** in both import orders
(the bug is order-dependent, so an in-process assertion would be worthless),
lazy re-exports still resolve, an unknown attribute still raises; plus the
aggregation invariants: bond+slaves do not double-count, an interface missing
from the recent window cannot fabricate a drop, a real drop is still detected,
sub-hosts fold to the parent. 845 → 856.

## Consequences

- Every traffic figure and every absolute floor is correct under both unit
  configurations, and the guard covers the whole tree rather than one file.
- `zbbx_mcp.fetch` is importable on its own.
- The disk forecast actually runs.

## Not included

- **`get_host` enrichment** (inventory / cost macro / recent traffic) — a
  real gap, but an enhancement rather than a defect.
- **A name-pattern hint when a country filter matches nothing.** The ADR 093
  extractor was the defect; this is a separate safety net.
