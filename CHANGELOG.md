# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

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
