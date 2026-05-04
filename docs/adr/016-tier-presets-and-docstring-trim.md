# ADR 016: ZABBIX_TIER presets + docstring trim for handshake-token savings

**Status:** Accepted
**Date:** 2026-05-04

## Problem

A measurement of the `tools/list` handshake on a 154-tool catalog
showed it costs ~30k tokens per session start (compact mode on,
default). For most sessions the LLM uses ~30–50 of the 154 tools;
the rest is dead weight that the LLM context pays for at every
session boot.

Two cost components contribute:

- **Per-tool JSON schema** for parameters — `~167 tokens / tool` average.
  Multiplying by 154 dominates the bill.
- **Description bodies** for the new ADR 010–015 tools — multi-paragraph
  prose with caveats and examples. Compact mode strips the `Args:`
  block already, but kept the narrative body, which now averaged
  ~99 chars/tool but spiked to 700+ on the worst offenders
  (`get_idle_relays`, `detect_disruption_wave`, `get_host_floods`).

## Decision

Two complementary changes. Combined, they let a typical ops session
pay ~11k tokens at handshake instead of ~30k — a ~63% reduction.

### Tier presets via `ZABBIX_TIER`

A new env var bundles tool subsets:

| Tier | Tools | Handshake | Use case |
|------|------:|----------:|----------|
| `core` | 34 | ~5k tokens | Read-only Zabbix querying |
| `ops` | 59 | ~11k tokens | Incident response — adds correlation, disruption, risk, IP history |
| `finance` | 56 | ~10k tokens | Costs and billing audits |
| `reports` | 71 | ~13k tokens | Executive reporting — adds reports + geo + inventory |
| `full` | 154 | ~29k tokens | Default — every tool |

Implementation lives in three small pieces:

- **`tools/tiers.py`** — defines `CORE_TOOLS`, `OPS_EXTRA`,
  `FINANCE_EXTRA`, `REPORTS_EXTRA`, `TIER_PRESETS`, and a pure helper
  `resolve_tier_disabled(tier_name, all_tools) -> frozenset[str]`.
  Tiers compose: every non-core tier extends `CORE_TOOLS`. Unknown
  tier names fall back to "no restriction" (safer than disabling
  everything; surfaces a typo without breaking the server).
- **`tools/__init__.py`** — exposes `ALL_TOOLS`, the canonical 154-name
  set. Was previously duplicated only inside `tests/test_registration.py`
  as `EXPECTED_TOOLS`. The test now asserts the local copy stays in
  sync with `ALL_TOOLS`.
- **`config.py`** — when `ZABBIX_TIER` is set, computes
  `disabled = resolve_tier_disabled(tier, ALL_TOOLS) ∪ DISABLED_TOOLS`
  and feeds it through the existing `register_all(...)` plumbing. No
  other code paths change.

`DISABLED_TOOLS` still applies on top so a user can pin a tier and
strip a few specific tools they don't want.

### Docstring trim

Eleven verbose tool docstrings (added in ADRs 010–015) collapsed from
multi-paragraph prose to a single summary line plus the `Args:` list.
The long-form context (motivating cases, caveats, decision rationale)
is already in the ADRs, which is the right place for it. Compact mode
already strips `Args:` so this only affects the narrative body.

Example: `get_idle_relays` 696 chars → 199 chars (compact ON view).

## Test approach

Seven new tests in `test_registration.py`:

- `test_expected_matches_canonical` — `EXPECTED_TOOLS == ALL_TOOLS`
  (catches duplication drift).
- `test_full_tier_returns_empty_disabled` — `"full"` and `""` are
  pass-through.
- `test_unknown_tier_falls_back_to_no_restriction` — typos do not
  cripple the server.
- `test_core_tier_disables_non_core_tools` — exact-set equality on
  the disabled output.
- `test_ops_tier_includes_correlation_and_disruption` — spot-check
  that the new ADR-010-through-013 tools land in `ops`.
- `test_finance_tier_includes_cost_tools` — spot-check finance.
- `test_reports_tier_includes_executive_and_html` — spot-check
  reports.
- `test_tier_preset_via_setup` — end-to-end through the env var,
  config parser, and FastMCP registration; the registered set is a
  subset of `CORE_TOOLS` after `ZABBIX_TIER=core`.

The docstring trim is verified by the existing tool-loaded /
tool-has-description tests (`test_server.py`); no new tests needed
since behaviour is unchanged.

## Consequences

- 332 tests pass (324 pre-change + 8 new tier tests; the
  `test_expected_matches_canonical` test counts inside the existing
  `TestToolRegistration` class).
- Tool count and registration behaviour are unchanged when
  `ZABBIX_TIER` is unset (full default).
- `ALL_TOOLS` becomes a public symbol of `zbbx_mcp.tools`.
- README documents the new env var and the savings table.
- Compact-mode handshake size at full tier drops from ~107k chars to
  ~104k chars (~30k → ~29k tokens). Marginal on its own but
  multiplicative with the tier savings.

## Not included

- Per-tool feature-flag schemas (further trimming optional parameters
  to shave the per-tool JSON schema cost). Risky — changes the API
  surface for a small additional saving.
- A meta-tool dispatcher (`zabbix_query(category, action, params)`)
  that would collapse the catalog into one entrypoint. Saves more
  tokens but kills LLM autonomy: the LLM cannot see available actions
  in `tools/list` and must be prompted with the dispatch schema.
- Splitting into multiple separate MCP servers (`zbbx-mcp-core`,
  `zbbx-mcp-ops`, etc.). Same effect as `ZABBIX_TIER` but with more
  installation friction.
- Auto-detecting the right tier from session usage. Stateless server;
  no cross-session memory.
