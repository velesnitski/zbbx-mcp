# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [1.9.4] - 2026-05-27

### Fixed — Parent / sub-host fold in `get_shutdown_candidates`
- `get_shutdown_candidates` now pre-folds sub-hosts into canonical
  groups before classification. The previous per-Zabbix-host loop
  could surface one multi-record physical machine as N separate
  DEAD / ZOMBIE / BROKEN / IDLE candidates, **and** count its
  sub-hosts as N peers in the cohort headroom math — inflating
  both the candidate count and the apparent peer capacity.
- Aggregation rules (mirroring ADR 032 conventions):
  - `cpu_avg` = **MAX** across the group (worst-case CPU)
  - `traffic_avg` = **SUM** across the group (each VIP has its
    own interface)
  - `service` = **WORST** across the group (DOWN > PARTIAL > OK)
- The peer-headroom cohorts are also built from canonical groups
  so capacity reflects physical machines. Cohort traffic peak +
  avg also SUM across sub-hosts.
- Display: candidate rows annotate `parent (+N sub)` when the
  group has sub-hosts.
- See ADR 037.

### Tooling
- 482 tests → 488 (+6 new metric-aggregation sanity tests:
  CPU=MAX, traffic=SUM, service=WORST; the all-idle and
  busy-sub-host-rescues-parent bug cases).

## [1.9.3] - 2026-05-27

### Fixed — Parent / sub-host fold in inventory + traffic tools
- Seven more per-host aggregators now collapse sub-host records to
  one canonical row each. Same bug shape ADRs 032 / 033 / 034
  addressed for the cost, outage-cluster, and service-check
  surfaces.
- Tools refactored (each with the worst-wins sort that fits its
  semantic):
  - `get_high_cpu_servers` — highest CPU per canonical wins.
  - `get_underloaded_servers` — lowest CPU per canonical wins.
  - `get_low_disk_servers` — highest disk% per canonical wins.
    Now fetches hostnames for **all** flagged hosts (not just top
    N) so the fold runs before the truncate.
  - `get_low_memory_servers` — lowest free memory per canonical
    wins. Same upfront-fetch change.
  - `get_stale_servers` — oldest last-data per canonical wins.
  - `detect_traffic_drops` — biggest drop % per canonical wins
    (via `fold_rows_by_canonical_host`).
  - `get_traffic_report` — different semantics: traffic and
    connections **SUM** across sub-hosts (each VIP has its own
    interface and session counter); `bw_per_client` is recomputed
    from the summed totals.
- See ADR 036.

### Tooling
- 479 tests → 482 (+3 new pattern-sanity tests for the inline
  fold loops: tuple worst-wins, hostid indirection with host_map
  lookup, traffic-report SUM fold).

## [1.9.2] - 2026-05-27

### Fixed — `generate_full_report` crash on save (Sentry dc717f4d)
- `excel.py` used a lazy-init pattern: the module-level fill
  constants (`HEADER_FILL`, `RED_FILL`, …) were `None` at import
  time and only rebound inside `_init_openpyxl()`. Consumers doing
  `from zbbx_mcp.excel import RED_FILL` at *their* module level
  captured the `None` binding — the later rebind never propagated.
- `full_report.py` was the one consumer using that import shape;
  the others import openpyxl lazily inside functions and so always
  saw a freshly-constructed fill.
- Symptom: every `generate_full_report` call raised
  `TypeError: expected <class 'openpyxl.styles.fills.Fill'>` from
  `wb.save()` because the cell `.fill` descriptor received `None`.
- Fix: removed `_init_openpyxl()`; module-level fills are now
  constructed eagerly (openpyxl is a hard dependency anyway, so
  the lazy-import saving was illusory). The other style-using
  tools are unaffected.
- See ADR 035.

### Tooling
- 476 tests → 479 (+3 new regression tests for the Fill
  descriptor: module-level fills are PatternFill instances,
  a workbook using each fill saves cleanly,
  `full_report`'s module-level imports resolve to PatternFill).

## [1.9.1] - 2026-05-26

### Fixed — Parent / sub-host fold in service-check tools
- Four tools that count "failing servers" from service-check items
  were summing one row per Zabbix host. Multi-record physical
  machines therefore inflated the count, the same shape that
  ADR 032 fixed for cost tools and ADR 033 fixed for outage
  clusters.
- New shared helpers in `data.py`:
  - `canonical_host_name(name)` — promoted from `correlation.py`
    to be the single primitive used by every per-host fold.
  - `fold_rows_by_canonical_host(rows, name_key, sort_key)` —
    dedupes a row list by canonical name, keeps first / sorted-
    first occurrence, annotates `sub_count`.
- Tools refactored to use canonical fold at the main count site:
  - `generate_service_brief` — per-check failing-server lists
    collapse sub-hosts; "Servers Failing" totals reflect physical
    machines.
  - `detect_regional_anomalies` — anomaly table sorted worst
    severity first, then folded to canonical (worst sub-host
    wins).
  - `get_service_uptime_report` — per-host rows sorted by
    primary-check uptime ascending, then folded (lowest uptime
    sub-host wins).
  - `get_service_health_matrix` — per-country counts now iterate
    canonical groups; a group is "up" for a check only when every
    sub-host is up (or any sub-host is traffic-validated).
- See ADR 034.

### Tooling
- 471 tests → 476 (+5 new for `fold_rows_by_canonical_host`:
  pass-through, sub-host collapse with first-occurrence kept,
  sort-key picks worst, mixed standalone/sub, alternate name key).

## [1.9.0] - 2026-05-26

### Fixed — Outage-cluster dedupe by canonical host name
- `get_outage_clusters` previously counted Zabbix sub-hosts of one
  physical machine as separate "distinct hosts" when checking the
  `min_hosts` threshold. A multi-VIP box throwing one problem on
  each VIP could therefore satisfy a 3-host cluster gate while
  actually being a single machine misbehaving — exactly the
  false-positive shape ADR 032 fixed for cost tools.
- Fix: new pure helper `_canonical_host_name()` in `correlation.py`
  strips the `" <suffix>"` tail. `_cluster_problems()` now uses
  canonical names in the `uniq_hosts` set and the `hosts` output
  field, so the threshold check and the displayed cluster size
  both reflect physical machines.
- `get_host_floods` already canonicalised via `build_parent_map`;
  this brings outage clusters to the same standard. See ADR 033.

### Tooling
- 471 tests (+6 new for `_cluster_problems` canonical fold:
  parent + sub-hosts below threshold, distinct hosts still cluster,
  mixed parents/subs counted correctly, sub-hosts only also fold,
  canonical-name helper pass-through and strip).

## [1.8.9] - 2026-05-26

### Fixed — Parent / sub-host double-count in cost tools
- New shared helper `canonical_host_groups()` in `data.py` collapses
  parent + sub-host Zabbix records into one canonical group per
  physical machine. Aggregation rules:
  - **cost = MAX** across the group (sub-host `{$COST_MONTH}` macros
    typically duplicate the parent's bill — summing inflated spend).
  - **traffic = SUM** across the group (each VIP has its own
    interface).
  - **cpu = MAX** across the group (worst-case across VIPs).
- Three cost tools now iterate canonical groups instead of raw
  hosts:
  - `get_cost_efficiency` — the "Waste" list, by-country, and
    by-provider tables no longer multiply per-VIP. Waste rows
    annotate sub-host count: `parent (+N sub)`.
  - `get_cost_summary` — server counts in by-product and by-provider
    tables now reflect physical machines.
  - `get_cost_gaps` — "M without cost" counts physical machines, not
    individual sub-host records.
- See ADR 032.

### Deferred (queued for v1.9.0)
- `get_shutdown_candidates` — two-pipeline (candidates + cohorts)
  plus three metrics (cpu/traffic/service); fold takes a separate
  pass.
- `bulk_diagnose` / `diagnose_subnet` — sub-host rows currently
  dilute the table.
- `detect_traffic_drops` / `detect_traffic_anomalies` /
  `get_traffic_report` — drop counts inflate by sub-host count.
- `get_high_cpu_servers` / `get_underloaded_servers` /
  `get_low_disk_servers` / `get_low_memory_servers` /
  `get_stale_servers` — current inheritance pattern is correct but
  rows still over-count sub-hosts.

### Tooling
- 465 tests (+9 new for `canonical_host_groups`: standalone, parent
  + sub-fold, cost=MAX, traffic=SUM, cpu=MAX, cost=None when
  unpriced, mixed standalone/sub, orphan sub-host, malformed values
  ignored).

## [1.8.8] - 2026-05-26

### Security
- **Bumped three transitive dependencies past CVE-required minimums**
  via `uv lock --upgrade-package`:
  - `python-multipart` 0.0.26 → 0.0.29 (CVE-2026-42561, High)
  - `urllib3` 2.6.3 → 2.7.0 (CVE-2026-44432, CVE-2026-44431, High)
  - `idna` 3.11 → 3.16 (CVE-2026-45409, Moderate)
- Lockfile-only change; no source edits, no API change. See
  ADR 031.

## [1.8.7] - 2026-05-26

### Added — `redact_partial` flag on `get_cost_summary`
- New optional `redact_partial: bool = False` arg. When True, drops
  per-product and per-provider rows where some servers in the group
  have no `{$COST_MONTH}` macro set, recomputes the grand total from
  the kept rows, suppresses the "Servers with cost / Without" line,
  and appends a footer marking the output as filtered. Intended for
  externally-shared artifacts (board decks, partner readouts) where
  partial-coverage metadata is a finding about process maturity
  rather than the metric the audience wants. Default is unchanged:
  internal callers see the full breakdown.
- Renderer extracted into a pure helper `_render_cost_summary` for
  testability. See ADR 030.

### Tooling
- 456 tests (+8 new for `_render_cost_summary` covering: default
  preserves full output, redact drops partial product/provider rows,
  recomputes grand total, suppresses the "Without" line, appends
  footer, handles all-partial edge case, defensive keep-on-missing
  for keys absent from the totals map).

## [1.8.6] - 2026-05-21

### Fixed
- **`bulk_diagnose(country=...)` returned a small random sample.** The
  Python-side country filter ran *after* the Zabbix API's
  `limit: max_hosts + 1` truncation, so the country filter narrowed
  an already-truncated sample rather than the full fleet. Fix: when
  `country` is set, skip the API `limit` and request `selectInventory`
  so `resolve_country()` sees both hostname and inventory signals;
  then truncate to `max_hosts` at the end. The `hosts=` / `group=`
  paths are unaffected (their filters apply server-side already).

## [1.8.5] - 2026-05-21

### Added — Tag-based filtering across detection tools
- New shared module `zbbx_mcp.tag_filter` exposing
  `parse_tag_filter(spec) -> list[dict]`. Operators pass tags as
  `"key:value,key2:value2"` (AND-combined); bare key means
  "exists" check. Parser tolerates whitespace, empty pairs,
  trailing commas.
- `search_hosts`, `get_problems`, `get_active_problems`, and
  `get_triggers` all gain a new optional `tags: str = ""` arg that
  pipes the parsed filter into the Zabbix `host.get` / `problem.get` /
  `trigger.get` payload. Tools without tag plumbing yet can be
  extended the same way (one-line import + payload merge).

### Added — Dependency surfacing in `get_triggers`
- New optional `with_dependencies: bool = False` arg surfaces each
  trigger's `selectDependencies` list. Lets operators spot
  dependent triggers that are masked by a parent firing. Zero
  behaviour change when deps are not configured.

### Added — Native anomaly-trigger surfacing (Zabbix 6.4)
- **`get_anomaly_triggers(only_active=True)`** — lists triggers
  whose expression references Zabbix 6.4's built-in time-series
  functions (`anomalystl`, `baselinewma`, `baselinedev`,
  `trendstl`, `forecast`). Complements the MCP's client-side
  detectors (`detect_loss_drift`, `detect_disruption_wave`) by
  exposing what server-side anomaly alerting is already configured.
  Lands in the `ops` tier. See ADR 029.

### Tooling
- 161 tools across 55 modules.
- 447 tests (+8 new for `parse_tag_filter`).

## [1.8.4] - 2026-05-21

### Added — Bulk diagnostic composition
- **`bulk_diagnose(hosts="", group="", country="")`** — runs the
  `diagnose_host` pipeline across a target set and returns a compact
  table (one row per host: verdict, mode, primary signal, action).
  Supports three filter axes that compose: explicit host list,
  host-group name, or country (ISO-2 / ISO-3 / English name).
  Bounded concurrency (semaphore=10), capped at 50 hosts per call.
  Output rows are sorted by verdict severity. Lands in the `ops`
  tier.
- **`diagnose_subnet(subnet)`** — follow-on to `get_outage_clusters`:
  when a cluster row reports "5 hosts on 1.2.3.0/24", paste that
  CIDR in here to get a verdict for each host. Accepts /24, /16, or
  dotted prefix forms. Internally resolves to a host list and reuses
  the bulk pipeline. Lands in the `ops` tier.

### Changed — Internal refactor
- `diagnose.py` factored into a shared async data-gather helper
  (`_collect_diagnosis_inner`) and a shared bulk runner
  (`_run_bulk_diagnosis`). Both new tools and the existing
  `diagnose_host` share these helpers. No behaviour change for
  `diagnose_host`; the rotation-history step is now skipped on
  bulk calls (set `rotation_days=0`) to keep fan-out responsive.

### Tooling
- 160 tools across 55 modules.
- 439 tests (+18 new for `_verdict_primary_signal`,
  `_render_bulk_table`, `_ip_matches_subnet` pure helpers).

## [1.8.3] - 2026-05-21

### Added — Zabbix-version introspection
- **`get_zabbix_version`** — wraps `apiinfo.version` and surfaces a
  feature-availability matrix derived from the parsed version.
  Operators (and the LLM client) can see at a glance which optional
  APIs the connected server supports: API token API (5.4+),
  unacknowledge / severity-change actions (6.0+), suppress /
  unsuppress (5.2+), cause/symptom rank actions (6.4+), connector
  API / proxy groups / HA cluster (7.0+). Lands in the `core` tier.
  See ADR 027.

### Changed — Enhanced acknowledge actions
- **`acknowledge_problem`** and **`bulk_acknowledge`** now accept
  two new optional params:
  - `severity: int = -1` — change the problem severity (0-5) in the
    same call. Maps to Zabbix `event.acknowledge` action bit 8.
  - `unack: bool = False` — unacknowledge instead of acknowledge.
    Maps to action bit 16 (mutually exclusive with the ack bit).
  Existing callers are unaffected; the new params default to no-op.
  The action-bitmask computation is now a pure helper
  (`_build_ack_action`) with 8 dedicated unit tests.

### Tooling
- 158 tools across 55 modules.
- 421 tests (16 new for pure-helpers: `_build_ack_action` +
  `_parse_zabbix_version` + `_feature_matrix`).

## [1.8.2] - 2026-05-21

### Added — Composite diagnostic
- **`diagnose_host(host)`** — one MCP call composes host.get +
  item.get + trend.get + problem.get + auditlog.get into a unified
  per-host report with verdict + recommended action. Auto-detects
  server-mode hosts (with agent / traffic items) vs domain-mode
  hosts (HTTPS-check only). Replaces the multi-tool chain operators
  ran by hand for every "is this host healthy?" question. Lands in
  the `core` tier. See ADR 026.

### Changed — Tier re-cut (evidence-based)
- 16 days of `get_telemetry_summary` data drove a data-driven re-cut
  of the tier composition (ADR 025). 12 tools in the original
  `core` tier had zero calls in the window:
  - 9 demoted to `full`-only: `get_templates`, `get_graphs`,
    `get_maintenance`, `get_services`, `get_global_macros`,
    `get_users`, `get_proxies`, `get_maps`, `get_map_detail`.
  - 3 demoted to thematic tiers: `acknowledge_problem` and
    `get_alerts` → `ops`; `get_sla` → `reports`.
- Handshake reductions (compact mode on):
  - `core`     5k → 4k tokens (-20%)
  - `ops`      11k → 9k       (-18%)
  - `finance`  10k → 7k       (-30%)
  - `reports`  13k → 10k      (-23%)
  - `full`     unchanged at 25k

### Tooling
- 157 tools across 55 modules.
- 405 tests (12 new for `diagnose_host` pure helpers).

## [1.8.1] - 2026-05-05

### Changed — Public-repo hygiene
- **`REGION_MAP` and `CAPITAL_COORDS` expanded to full ISO 3166-1
  coverage.** Both tables previously held curated subsets (65 and 49
  countries respectively); the inclusion list was a soft hint at
  market footprint. Now every ISO 3166-1 country is present in both
  tables (200 entries each). `get_latency_estimate` works for any
  source country, not just the previously-curated subset.
  - REGION_MAP grouping unchanged in spirit (NA, LATAM, EMEA, APAC,
    CIS); Central Asia / Caucasus countries appear in two regions
    where the geography genuinely overlaps.
  - CAPITAL_COORDS adds capital lat / lon for every country not
    previously listed.
- **Hostname-pattern placeholder cleanup.** A comment in
  `costs_import.py` used a `prem-*` example matching a banned
  hostname-pattern (the operator's actual fleet prefix); replaced
  with generic `parent-a a-b`.

No tool added or removed; tool count and behaviour unchanged. 393
tests pass; ruff + mypy + sensitive scan all clean.

## [1.8.0] - 2026-05-05

### Added — Self-introspection
- `get_telemetry_summary`: reads the analytics log written by the existing
  `logged()` decorator and reports per-tool call counts, error rate,
  average + max latency, and average response size. Args: `hours`
  (lookback window, 0 = all-time), `top`, `log_path`. Pure helper
  `_summarise_records` covered by unit tests; handles both epoch and
  ISO 8601 timestamps. Lands in the `core` tier so introspection
  works in every session. See ADR 024.

### Changed — Code organisation (no behaviour change)
- `data.py` split: country-specific reference data
  (`REGION_MAP`, `CAPITAL_COORDS`, `_COUNTRY_NAMES` table,
  `extract_country` / `normalize_country` / `resolve_country` /
  `countries_for_region`) extracted to new module
  `src/zbbx_mcp/country.py`. `data.py` re-exports the public symbols
  for back-compat — every existing `from zbbx_mcp.data import ...`
  callsite keeps working. `data.py` shrank from 659 to 334 lines.
- `costs.py` split: the 2173-line / 14-tool monolith broken into
  four cohesive modules. `costs_common.py` (shared helpers + tags),
  `costs_import.py` (6 ingestion tools), `costs_audit.py` (5 audit
  and reconciliation tools), `costs_summary.py` (3 read-only summary
  tools). Tool count and names unchanged.
- Output formatters `_format_value` and `_format_age` promoted to
  public `format_value` and `format_age` in `formatters.py`.
  Analytics helpers `_subnet24`, `_parse_ip_changes`,
  `_compute_loss_drift`, `_split_baseline_recent` had their
  underscore prefixes dropped (they are imported across tool
  modules and were never module-private in practice).

### Changed — Robustness
- `server.py` gained a single shared `_iter_registered_tools` helper
  with graceful fallback if FastMCP renames its private
  `_tool_manager._tools` attributes. Both `_compact_descriptions`
  and the tool-wrapping loop now degrade with a logged warning
  instead of raising `AttributeError` at startup.

### Added — CI gates
- `mypy` typecheck job runs `mypy src/zbbx_mcp` on every push / PR.
  `tools/` excluded for now (~180 accumulated type smells); core
  modules (`data.py`, `fetch.py`, `formatters.py`, `classify.py`,
  `config.py`, `client.py`, `server.py`, `logging.py`,
  `rollback.py`, `resolver.py`, `utils.py`, `excel.py`,
  `country.py`) are clean.
- `pytest --cov=zbbx_mcp --cov-fail-under=15` runs in the test job;
  prevents silent coverage regression below the current floor.

### Added — Documentation
- `docs/adr/README.md`: index of all 24 ADRs grouped by theme
  (cost-import pipeline, infrastructure, outage correlation,
  disruption detection, trends / traffic / problems, token
  efficiency and hygiene, observability and architectural hygiene).
- `CONTRIBUTING.md`: new "Sensitive content" section with the
  public-repo hygiene rules and the reproducible pre-commit scan
  command. The new CI gates (ruff, mypy, coverage) listed in the
  code-style section.

### Tooling
- 156 tools across 54 modules.
- 393 tests.
- ADRs 010 through 024.

## [1.7.0] - 2026-05-05

### Added — Outage correlation (ADR 010, 015, 022)
- `get_idle_relays`: relay hosts whose mgmt NIC has traffic but tunnel
  interfaces report zero. Exclusion-based detection plus a physical-NIC
  regex fallback so unused secondary adapters don't bucket as tunnels.
- `get_outage_clusters`: greedy time-window grouping of active problems.
  Supports `subnet24` / `subnet16` / `provider` / `hostgroup` / `auto`.
- `get_host_floods`: single-host outage detector — N simultaneous
  problems on one machine. Sub-host (parent + " " + suffix) merges.

### Added — Disruption detection (ADR 012, 013, 014, 020)
- `detect_loss_drift`: ping-loss / RTT drift vs 14d baseline.
  Env-driven (`ZABBIX_PING_LOSS_KEY`, `ZABBIX_PING_RTT_KEY`).
- `detect_service_port_split`: service-port traffic dropped while
  management is healthy. Env-driven (`ZABBIX_SERVICE_BPS_KEY`).
- `detect_regional_traffic_loss`: regional-bucket traffic collapse vs
  flat peers. Env-driven JSON map (`ZABBIX_REGIONAL_TRAFFIC_KEYS`).
- `detect_disruption_wave`: many hosts × many /24s in the same hour.
  Diurnal-safe defaults, country-cohesion guard, and peer-relative
  drop pre-filter (host vs same-cohort peers) to suppress diurnal
  false positives.

### Added — Risk and impact (ADR 013, 014)
- `get_at_risk_hosts`: composite score over peer rotations + ping/RTT
  drift + IP age. Skips hosts with no peer churn AND no drift signal.
- `get_disruption_blast_radius`: cohort connection-count delta
  pre/post a host drop. Reuses `KEY_CONNECTIONS`.

### Added — External IP history (ADR 012, 013, 019)
- `get_external_ip_history`: per-host IP rotation timeline with
  recovery scoring against a 24h pre/post traffic comparison.
- `get_recovery_score`: fleet-level recovery KPI aggregator.

### Added — Trigger / problem analysis (ADR 011, 019)
- `get_trigger_timeline`: OK ↔ PROBLEM transitions for a trigger.
- `bulk_acknowledge`: acknowledge many events at once.
- `get_problem_age_buckets`: per-severity histogram (<1d / 1-3d /
  3-7d / 7d+) — fills the visibility gap on the actionable
  1–7d band.
- `get_stale_items` cascade-aware mode (`collapse_dependencies`) —
  folds downstream stale dependents into stale master via
  `master_itemid` walk.

### Added — Token efficiency (ADR 016, 017)
- `ZABBIX_TIER` env var bundles for focused sessions: `core` (~5k
  tokens), `ops` (~11k), `finance` (~10k), `reports` (~13k), or
  `full` (default, ~25k). Cuts 60–80% off the tools/list handshake
  for typical sessions.
- Schema `title` field strip + cost-tool docstring trim — knocked
  ~5k tokens off the full-tier handshake.

### Added — Country normalization (ADR 023)
- `normalize_country()` and `resolve_country()` in `data.py`.
  `search_hosts`, `search_hosts_by_location`, `get_server_clusters`
  now accept ISO-2, ISO-3, or English country name. Result header
  surfaces the resolved code so the caller sees that the input was
  understood. Hosts without a country segment in their hostname fall
  back to Zabbix host inventory.

### Changed — Accuracy and noise reduction (ADR 014, 015, 018, 020, 021, 022)
- `get_active_problems`, `get_correlated_events`, and
  `get_outage_clusters` collapse host-embedded triggers (`Foo on
  host-a` / `Foo on host-b`) under the same dedup key via a new
  `normalize_problem_name` helper. Affected hostnames remain
  visible in the affected-hosts column.
- `detect_disruption_wave` defaults retuned for diurnal safety
  (window 6h → 12h, recent 1h → 2h, drop 30% → 50%) plus a new
  `min_baseline_mbps=5.0` floor.
- Service-check tools (`fetch_service_status`, `generate_service_brief`,
  `get_health_assessment`, `detect_regional_anomalies`,
  `get_service_uptime_report`, `get_service_health_matrix`) now skip
  unsupported / stale-lastclock items instead of reading their
  lingering 0 as service-down.
- `get_outage_clusters` and `get_host_floods` gain a `max_age_hours`
  recency filter (default 0 = unlimited preserves existing
  behaviour). Both surface the cluster / flood age in the output via
  a shared `_format_age` helper.
- `detect_disruption_wave` and `get_outage_clusters` use canonical
  hostid (`build_parent_map`) so a parent + sub-host pair counts
  once in cohesion / unique-host calculations.
- `detect_traffic_drops` skip-breakdown footer surfaces what was
  dropped (no-history / no-baseline-window / below-floor).
- `get_shutdown_candidates` peer-headroom safety check
  (SAFE / RISKY / SOLO).
- `get_outage_clusters` `problem.get` switched to
  `sortfield="eventid"` (Zabbix 6.4 rejects sortfield=clock).
- `get_idle_relays` NAT-mode caveat softened — observed false-
  positive rate is low.

### Fixed
- `get_item_history` accepts ISO date, ISO datetime, relative
  ("24h", "7d"), and epoch int.
- `get_problems` time-window filters: `time_from`, `time_till`,
  `include_resolved`, `event_eventid` (problem timeline).
- `search_hosts` markdown table preserved at scale.
- `get_at_risk_hosts` skips hosts that score on age alone (no peer
  churn, no drift) — was returning every host at the same floor
  score.
- `import_from_xlsx` localised header is now env-driven
  (`ZABBIX_BILLING_IP_HEADER`); no non-ASCII literal in source.

### Tooling
- 155 tools across 49 modules.
- 386 tests (pure-helper coverage on every new analytic).
- ADRs 010 through 023 documenting design decisions.

## [1.6.0] - 2026-03-30

### Added
- **112 tools** across 35 modules
- 4 analysis tools: `analyze_server_roles`, `correlate_logs`, `audit_host_ips`, `classify_external_ips`
- `detect_regional_anomalies`: detect unusual patterns within a geographic region
- `find_host_dashboard`: quick host-to-dashboard lookup
- `generate_product_map`: auto-create starter product config from Zabbix groups
- `get_product_audit`: categorize servers by product as active/dead/idle with cluster awareness
- `get_audit_log`: Zabbix audit log for host creation dates and change history
- `get_host_availability`, `get_recent_changes`: host uptime and config change tracking
- `get_service_health_matrix`, `get_service_uptime_report`: service-level monitoring
- `get_error_rate`: error rate analysis per host/group
- `get_incident_report`: incident reporting with timeline
- `get_traffic_drop_timeline`: traffic drop analysis over time
- `get_unknown_providers`: group unclassified server IPs by /16 prefix
- `identify_providers`: auto-detect unknown hosting providers via reverse DNS
- Exclusion-based tunnel detection (replaces hardcoded checks)
- Tag-based NIC discovery for traffic instead of hardcoded interface key list
- `HIDE_PRODUCTS` env var to exclude products from CEO report fleet composition
- Sentry error capture and logging integration
- GitHub Copilot and Codex CLI setup guides in README
- MCP resources support

### Changed
- Service check keys fully configurable via env vars (no hardcoded values)
- Lazy-load openpyxl (~15MB RAM savings on startup)
- Tool descriptions trimmed: 6010 → 5332 chars compacted (−11%)
- Virtual interface blacklist replaced with physical NIC whitelist
- CEO report uses service fleet counts (excludes infra/monitoring from KPIs)
- Cluster-aware product audit: detects secondaries of active primaries
- Version bump to 1.6.0

### Fixed
- Trend sanity: change < −30% with "stable" now correctly shows "dropping"
- Trend sanity: change > +30% with "stable" now correctly shows "rising"
- Traffic unit: removed incorrect ×8 multiplier (values already in bits/sec)
- CEO report change %: uses trend-vs-trend comparison (same data source)
- CEO report avg: uses TrendRow.avg (proper mean) instead of broken daily running average
- Dead server count: requires actual traffic monitoring data (was counting hosts without items)
- TrendRow.daily: proper sum/count mean replaces broken (old+new)/2 running average
- `ZABBIX_TRAFFIC_UNIT` env var: set to `bytes` for deployments where net.if.in returns bytes/sec
- All traffic conversions use configurable divisor (bits: /1M, bytes: /8M)
- Dependabot: bumped pytest>=9.0.3, pytest-asyncio>=1 (CVE fix)
- Domain CSV export: 19 fields including SSL expiry days, issuer, response time, HSTS, IPv6
- Provider "Unknown": hosts without IP skipped from distribution
- UK → GB country code normalization
- Service DOWN: don't mark as broken if server has real traffic (>2 Mbps)
- Off-by-one in trend sanity boundary values

### Security
- All service check keys configurable via environment variables
- 93 hosting providers (368 CIDR ranges) in classification database
- Comprehensive code and history audit for data hygiene
- Test assertions use generic examples only

## [1.5.0] - 2026-03-29

### Added
- **100 tools** — configurable service keys, product filtering
- `generate_ceo_report`: full executive HTML report with all analytics sections
  - Executive Summary with auto-generated alerts
  - Traffic by Country with trend badges and bar charts
  - Service Uptime by Country (SLA dashboard)
  - Capacity Planning (Mbps/server density)
  - Risk Assessment (provider concentration, redundancy)
  - Fleet Composition (product breakdown cards)
  - Shutdown Candidates (dead/broken/idle with server table)
  - Provider Distribution (stacked bar chart + concentration risk)
  - Expansion Opportunities (LATAM/APAC/EMEA tables)
  - Strategic Recommendations (immediate/short/medium-term actions)
  - Country Deep Dives (auto-detected + manual via `deep_dive_country` param)
  - Traffic Redistribution Analysis (where traffic goes when servers go down)
  - Status Legend (severity labels explained)
- `get_peak_analysis`: peak vs off-peak traffic by hour-of-day
- `get_executive_dashboard`: single-call KPI summary
- `get_month_over_month`: period comparison on traffic/CPU/countries
- `get_fleet_risk_score`: composite risk per country
- `get_sla_dashboard`: uptime % by product/country
- `get_report_snapshot`: save KPIs as JSON
- `get_expansion_report`: regional coverage gap analysis
- `get_regional_density_map`: server density by country with datacenter info
- `get_latency_estimate`: nearest server by geographic distance (haversine)
- `get_event_frequency`: flapping detection
- `get_correlated_events`: find same problem on multiple hosts
- `get_server_clusters`: detect host clusters from naming patterns
- `search_hosts_by_location`: compound query with country/group/product/traffic filter
- `resolve_datacenter(ip)`: IP-to-datacenter-city via CIDR ranges
- Region filters on geo/traffic tools (LATAM/APAC/EMEA/NA/CIS/ALL)
- Region mapping (`REGION_MAP`, `CAPITAL_COORDS`) in data.py
- Shared fetch helpers: `fetch_enabled_hosts`, `fetch_traffic_map`, `fetch_cpu_map`, `group_by_country`, `host_ip`
- Pre-compiled regex patterns in response compression
- Host fetch caching (60s TTL)
- Batched trend fetch (200/chunk) to avoid Zabbix 500 on large fleets
- CLAUDE.md, REVIEW.md, AGENTS.md for AI agent context
- `py.typed` marker (PEP 561), `__all__` on all core modules
- CI lint job (ruff), ruff config in pyproject.toml
- Multi-stage Dockerfile with non-root user and healthcheck

## [1.0.0] - 2026-03-24

### Added
- Initial release with comprehensive Zabbix MCP server
- Multi-instance support, read-only mode, HTTPS enforcement
- Async HTTP/2 client with connection pooling
- Rollback system with pre-mutation snapshots
- Excel and HTML report generation
- Traffic anomaly detection and regional-loss monitoring
- Cost management via host macros
- Slack integration
- Structured JSON logging with Sentry support
- Docker support with docker-compose
- GitHub Actions CI (Python 3.10–3.13)
