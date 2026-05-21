# ADR 025: Evidence-based tier re-cut from 16 days of telemetry

**Status:** Accepted
**Date:** 2026-05-21

## Problem

Tier composition (ADR 016) was originally built from intuition about
"what does a typical session look like?" — necessary at the time,
because no usage data existed yet. ADR 024 shipped
`get_telemetry_summary` precisely to replace that intuition with
evidence.

16 days of live telemetry (2026-05-05 → 2026-05-21, 1145 calls
across 97 tools) now allow a data-driven re-cut. The key finding:
**12 of ~35 tools in the original `core` tier had zero calls over
the full window**. They were pulling weight in the handshake budget
without delivering value.

## Decision

Three changes to ``tools/tiers.py``, all evidence-driven:

### 1. Demote nine tools out of every tier — keep `full`-only

These had zero usage in 16 days and no obvious workflow that should
live in a tier preset:

| Tool | Why removed |
|------|-------------|
| `get_templates` | Templates are configured once; querying them at session start is not the workflow |
| `get_graphs` | Per-host graph listing — used in UI, not in MCP sessions |
| `get_maintenance` | Maintenance-window introspection — rare ops task |
| `get_services` | IT-services tree — separate Zabbix concept |
| `get_global_macros` | Global macros are infrastructure config, not session data |
| `get_users` | User list is admin-level introspection |
| `get_proxies` | Zabbix proxies (the infra component) — admin-level |
| `get_maps` | Network maps — UI feature, not MCP |
| `get_map_detail` | Same |

Still available with `ZABBIX_TIER=full` (or unset). Operators who
need them can opt in or call them directly via the resource catalog.

### 2. Demote three tools from `core` to thematically-fitting tiers

These had zero `core`-tier usage but native workflow homes:

| Tool | From | To | Rationale |
|------|------|-----|-----------|
| `acknowledge_problem` | core | `ops` | The ack workflow is an incident-response concern |
| `get_alerts` | core | `ops` | Live alert listing pairs with incident tools (`get_alert_summary` is already in ops) |
| `get_sla` | core | `reports` | Single-host SLA query — reporting concern, sits next to `get_sla_dashboard` |

### 3. No promotions

The data shows no under-represented tools that the operator reaches
for from outside their assigned tier. Top-used non-core tools
(`import_costs_by_ip`, `set_host_macro`, `get_cost_summary`,
`generate_ceo_report`, `get_geo_traffic_trends`,
`get_executive_dashboard`, `get_sla_dashboard`,
`get_shutdown_candidates`, `get_traffic_report`, `get_product_audit`)
all sit in correctly-targeted tiers already.

## Test approach

The existing tier tests in `test_registration.py` already cover the
invariants this change must preserve:

- `test_core_tier_disables_non_core_tools` — `search_hosts` still in
  core, `generate_ceo_report` still out.
- `test_ops_tier_includes_correlation_and_disruption` — correlation /
  disruption tools still in ops; this change adds two more.
- `test_finance_tier_includes_cost_tools` — unchanged.
- `test_reports_tier_includes_executive_and_html` — unchanged.
- `test_tier_preset_via_setup` — end-to-end via env var still works.

All 393 tests pass. No test additions needed because the change
removes from tiers (looser disabled set) rather than tightens.

## Consequences

Measured handshake reductions (compact mode on, default):

| Tier | Before | After | Saved |
|------|-------:|------:|------:|
| `core` | ~5k tokens / 35 tools | **~4k tokens / 25 tools** | -20% |
| `ops` | ~11k tokens / 60 tools | **~9k tokens / 52 tools** | -18% |
| `finance` | ~10k tokens / 57 tools | **~7k tokens / 47 tools** | -30% |
| `reports` | ~13k tokens / 72 tools | **~10k tokens / 63 tools** | -23% |
| `full` | ~25k tokens / 156 tools | ~25k tokens / 156 tools | unchanged |

`full` mode is unchanged because every tool is still registered —
the cut only affects tier presets.

Behaviour change is limited to which tools are *visible* at each
tier. No tool was removed from the codebase, no signature changed.
A caller who previously relied on `acknowledge_problem` being in
`ZABBIX_TIER=core` will now need either `ZABBIX_TIER=ops` or to
re-enable via `DISABLED_TOOLS` override (which already takes
precedence). Documented in the v1.8.2 changelog.

## Not included

- Removing the 59 zero-usage tools from the codebase entirely. Many
  serve niche workflows that may surface later; deleting them now
  is premature optimisation. Available in `full` for anyone who
  needs them.
- A periodic auto-re-cut. The data is small enough that a manual
  review every few months (or after a major usage shift) is fine.
  Building an automated tier-adjustment mechanism would be over-
  engineering for the current scale.
- Performance work on the slow outliers (`identify_providers` at 14s
  avg, `audit_external_ips` at 9s avg, `get_executive_dashboard`
  with 13% error rate). Separate follow-ups, tracked in tasks.md.
- A `prompts/` surface for common workflows (ADR-pending). Telemetry
  data also informs which prompts deserve to exist; comes after the
  next development cycle.
