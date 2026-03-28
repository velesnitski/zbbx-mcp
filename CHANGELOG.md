# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [1.4.0] - 2026-03-28

### Added
- **98 tools** — 8 new tools:
  - `generate_ceo_report`: full executive HTML report with fleet KPIs, alerts, traffic trends, SLA, capacity, risk, provider distribution, expansion opportunities, shutdown candidates, and strategic recommendations
  - `get_peak_analysis`: peak vs off-peak traffic by hour-of-day from trend data
- 93 hosting providers (368 CIDR ranges) in IP classification database — covers hyperscalers, European/US/Asian hosting, CDN, and niche providers
- Datacenter city resolution via CIDR ranges (`resolve_datacenter`)
- Region filters on geo/traffic tools (LATAM, APAC, EMEA, NA, CIS)
- UK → GB country code normalization

### Changed
- Expansion report thresholds: OVERLOADED at 3000 Mbps/server (was 500), added HIGH tier
- Pre-compiled regex patterns in response compression
- Host fetch caching (60s TTL) for repeated calls
- Extracted Zabbix key constants (`KEY_CPU_IDLE`, etc.)
- Batched trend fetch (200/chunk) to avoid Zabbix 500 on large fleets

### Fixed
- Country trend direction derived from aggregated daily data (was last-host-wins)
- `get_month_over_month` period parsing (was unparseable "60d-30d")
- `get_sla_dashboard` 0% uptime (was counting servers without VPN check as DOWN)
- `get_executive_dashboard` TrendRow attribute error
- HTML report trend data lookup (uses Host ID directly)
- Noisy trend labels for countries with <0.05 Gbps traffic

## [1.3.0] - 2026-03-27

### Added
- **96 tools** — 12 new tools:
  - `get_server_clusters`: detect host clusters from naming patterns, infer primary/secondary roles
  - `search_hosts_by_location`: compound query with country + group + product + traffic filter
  - `get_event_frequency`: flapping detection — count events per host/trigger with avg interval
  - `get_correlated_events`: find same problem on multiple hosts within a time window
  - `get_expansion_report`: regional coverage gap analysis with capacity headroom per country
  - `get_regional_density_map`: server density by country with traffic, CPU, datacenter info
  - `get_latency_estimate`: nearest server by geographic distance (haversine)
  - `get_executive_dashboard`: single-call KPI summary for leadership (<2000 chars)
  - `get_month_over_month`: compare two periods on traffic, CPU, countries
  - `get_fleet_risk_score`: composite risk per country (provider, capacity, redundancy)
  - `get_sla_dashboard`: uptime % by product/country weighted by traffic
  - `get_report_snapshot`: save KPIs as JSON for historical comparison
- `resolve_datacenter(ip)` — IP-to-datacenter-city resolution via CIDR ranges (zero API calls)
- Executive summary section in HTML report with action badges and country traffic bars
- `region` filter on `get_geo_traffic_trends`, `detect_traffic_drops`, `detect_traffic_anomalies` (LATAM/APAC/EMEA/NA/CIS/ALL)
- `product` filter on `detect_geo_blocks`
- `country` param on `search_hosts` with exact `extract_country()` match
- Shared region mapping (`REGION_MAP`, `CAPITAL_COORDS`) in `data.py`
- CLAUDE.md, REVIEW.md, AGENTS.md for Claude Code / Warp agent context
- `py.typed` marker (PEP 561), `__all__` on all core modules
- CI lint job (ruff), ruff config in pyproject.toml
- Multi-stage Dockerfile with non-root user and healthcheck
- Codex CLI setup section in README

### Changed
- `search_hosts` now uses substring matching (`*query*`) instead of prefix-only
- Refactored shared helpers into `data.py`: `fetch_enabled_hosts`, `fetch_traffic_map`, `fetch_cpu_map`, `group_by_country`, `host_ip`
- Trimmed 12 tool docstrings (−3,740 chars, −12% token cost)
- Version bump to 1.3.0

### Fixed
- `search_hosts_by_location`: Zabbix API -32500 error (removed unsupported `sortfield: lastvalue`)
- `get_host_items`: search now uses substring match for name and key
- Country filter fixed in `report.py`, `html_report.py`, `full_report.py` (was substring, now exact)
- `get_server_availability_report`: defaults to `exclude_product="infrastructure,monitoring"`
- `get_health_assessment`: groups identical issues, `max_results=30` default
- Codebase cleanup: consistent generic labels across all output
- Unused variables and imports cleaned up (ruff)

## [1.2.0] - 2026-03-26

### Added
- `ZABBIX_COMPACT` env var — strips markdown from tool responses (bold, headers, table separators). 5–15% token savings per response
- `ZABBIX_RESPONSE_BUDGET` env var (default: `8000`) — truncates responses exceeding budget at clean line break. Prevents 50K+ responses from consuming tokens
- Response compression wrapper applied to all 84 tools alongside analytics logging
- Token optimization for 6 tools (tasks 63–68):
  - `get_server_availability_report`: `only_problems=True` default, `exclude_product`, `max_results`
  - `get_capacity_planning`: `max_results=30`, `min_priority` filter
  - `get_geo_traffic_trends`: `min_traffic=0.1` Gbps, skip near-zero countries
  - `get_health_assessment`: `min_severity=WARNING`, skip INFO items

### Fixed
- Country filter across geo/health tools now uses `extract_country()` for exact match instead of substring (task 67)
- Generic VPN labels in all output columns and docstrings

## [1.1.1] - 2026-03-26

### Added
- `ZABBIX_COMPACT_TOOLS` env var (default: `true`) — strips redundant Args section from tool descriptions, saving ~5,000 tokens per message (32% reduction). Args info is already in the JSON schema. Set `false` to restore full descriptions.

## [1.1.0] - 2026-03-25

### Added
- `get_protocol_failure_matrix`: per-country VPN protocol status matrix with recommendations
- `get_block_timeline`: when blocks started per country from daily trend data
- HTML report grouped by Product → Dashboard → Tab (task 61)
- Dashboard tab links: server names link to Zabbix dashboard page+tab (task 62)
- `dashboardid` and `page_index` in `ServerRow` and `graph_context`
- Multi-product include/exclude filters on `generate_html_report` and `generate_full_report`
- `ZabbixClient.frontend_url` property for Zabbix UI link construction
- Print/PDF CSS: light theme, readable badges, page breaks

## [1.0.9] - 2026-03-25

### Added
- `get_protocol_failure_matrix`: per-country VPN protocol status (per-protocol OK/DOWN/PARTIAL) with recommendations
- `get_block_timeline`: when blocks started per country, duration, pre-block traffic vs current

## [1.0.8] - 2026-03-25

### Added
- Multi-product and exclude filters on `generate_html_report` and `generate_full_report`
  `products="ProductA,ProductB"` or `exclude_product="Infrastructure,Monitoring"`
- `ZabbixClient.frontend_url` property for reliable Zabbix UI link construction

### Fixed
- HTML report: Zabbix links now work (used `frontend_url` instead of raw API URL)
- Print/PDF: comprehensive `@media print` CSS — light background, readable text, colored badges with borders, proper page breaks

## [1.0.7] - 2026-03-25

### Changed
- HTML report: server names link to Zabbix latest data page, added IP and Groups columns
- Health assessment and shutdown candidates now show IP address per server
- `ServerRow` includes `hostid` for Zabbix deep linking

## [1.0.6] - 2026-03-25

### Added
- **3 new geo monitoring tools** (82 total):
  - `detect_geo_blocks`: country-level VPN block detection. Groups servers by country, compares current traffic to baseline, flags countries where >50% of servers show >50% traffic drop. Cross-references with VPN health status.
  - `get_geo_traffic_trends`: per-country traffic over time (30d daily). Shows total Gbps per country, trend direction, growth/decline percentage.
  - `get_server_availability_report`: VPN protocol uptime per server (per protocol). Calculates hours UP vs DOWN from trend data. Country-level summary.
- `disk_read` and `disk_write` added to `METRIC_KEYS` (sda, vda, nvme0n1)

## [1.0.5] - 2026-03-25

### Added
- Cluster/location pattern detection in `get_health_assessment` (task 39)
  Automatically groups issues by country and flags "CLUSTER DEAD" (all servers
  critical) or "CLUSTER DEGRADED" (>50% critical) — catches ISP blocks,
  datacenter outages affecting entire locations
- Zabbix agent availability in `get_health_assessment` (task 41)
  Shows "Agent unavailable" with score -30. Distinguishes offline servers
  from servers with working agent but broken VPN/traffic

All open tasks in tasks.md are now either implemented, already covered by
existing tools, or require Zabbix-side investigation (not MCP code changes).

## [1.0.4] - 2026-03-25

### Added
- **79 tools** across 31 modules
- `generate_html_report`: dark-themed responsive HTML with KPI cards, color-coded server table, traffic bars, 7d trend data, provider distribution. Printable to PDF via browser.
- `get_capacity_planning`: find overloaded servers needing upgrade. Detects sustained CPU overload, BW saturation, hardware inefficiency (CPU/traffic ratio vs peers), rising traffic trends. Multi-signal scoring with recommended actions.
- `get_shutdown_candidates`: find servers to decommission. Categories: DEAD (traffic+CPU near zero), ZOMBIE (high CPU, no traffic), BROKEN (VPN DOWN + low traffic), IDLE (below thresholds). Includes VPN health per candidate.
- `get_health_assessment` enhanced: idle/zombie detection (task 34), VPN health check (task 36), "recently died" vs "always idle" distinction (task 40)
- `get_underloaded_servers` now shows traffic column alongside CPU
- `get_trends_batch`: daily breakdown via `aggregation="daily"`, tier filter, iowait/softirq metrics
- `get_server_dashboard`: daily breakdown table by default
- `compare_servers`: CPU/100Mbps efficiency metric, BW headroom calculation
- `extract_country` regex fixed for Lite hostnames (`srv-nl01-lite` → IN)
- `generate_full_report`: country/product filters, recalculated summary stats

## [1.0.3] - 2026-03-25

### Added
- `get_host` accepts hostname (auto-resolves to hostid)
- `get_problems` falls back to visible name search when exact match fails
- `country` filter on `get_high_cpu_servers` and `get_underloaded_servers`
- `get_server_load` traffic fetch parallelized with CPU/load/memory (2x faster)
- `__all__` exports, `__slots__` on `ServerRow`, `GB_BYTES` constant

### Fixed
- `get_server_load` used hardcoded `eno` search; now uses `TRAFFIC_IN_KEYS` (all NIC patterns)
- Narrowed bare `except Exception` to specific types across codebase
- Module-level imports in `server.py` and `slack.py` (were late imports inside functions)
- Removed dead `PRODUCT_MAP = None` from `inventory.py`

## [1.0.2] - 2026-03-25

### Added
- `country` and `product` filters on `generate_full_report` — generate per-country or per-product reports
- `country` filter on `detect_traffic_drops`, `detect_traffic_anomalies`, `get_traffic_report`
- `{$BW_LIMIT}` host macro support — per-server bandwidth limit for accurate BW Util % (falls back to 800 Mbps)
- `hostid` now shown in `search_hosts` output — enables `get_host_items` → `get_trends` chain
- `TRAFFIC_IN_KEYS` / `TRAFFIC_OUT_KEYS` constants shared across all traffic tools (DRY)

### Fixed
- Traffic tools now use fast NIC filter instead of slow name search (consistent with reports)
- All 3 traffic tools accept `country` parameter for per-region analysis

## [1.0.1] - 2026-03-25

### Changed
- Traffic fetch optimization: 2.3x faster reports (3.5s → 1.2s) using fast filter by known physical NIC keys with fallback for uncommon interfaces
- Host cache (60s TTL) avoids redundant host.get calls across tools

### Added
- 46 new tests (78 → 124): excel.py, logging.py, utils.py, data helpers
- Full physical NIC coverage (28 interface key patterns)

### Security
- Slack webhook URL only from env var (removed `webhook_url` parameter)
- `asyncio.gather(return_exceptions=True)` for graceful partial failures
- Secret macros (type=1) scrubbed from rollback snapshots
- PRODUCT_MAP restricted to .json files only
- Enhanced Sentry scrubbing (exceptions, breadcrumbs, expanded patterns)
- Connection cleanup via atexit handler
- `classify.py` extracted to eliminate circular import

### Fixed
- Consolidated 6 duplicate late imports in full_report.py
- Removed unused problem.get stub from data.py

## [1.0.0] - 2026-03-24

### Added
- **72 tools** across 29 modules — most feature-rich Zabbix MCP server
- **4 Excel report tools**: `generate_server_report`, `generate_infra_report`, `export_dashboard`, `generate_full_report`
- **Full report** with 8 sheets: All Servers (29 columns), Health Overview, Product Analytics, Country Analytics, Dashboard Tabs, Provider × Product matrix, Bandwidth Analysis, Off-Dashboard
- **Traffic anomaly detection**: `detect_traffic_anomalies` (peer comparison), `detect_traffic_drops` (trend-based ISP blocking detection), `get_traffic_report`
- **Cost management**: `import_server_costs`, `set_bulk_cost`, `get_cost_summary` via `{$COST_MONTH}` host macros
- **Slack integration**: `send_slack_message`, `send_slack_report` (env var only, no URL parameter)
- **Alerts**: `get_alerts`, `get_alert_summary` (notification history)
- **Users**: `get_users` with roles, groups, media
- **Proxies**: `get_proxies` with host counts
- **Maps**: `get_maps`, `get_map_detail`
- **Media & Actions**: `get_media_types`, `get_actions`
- **Host group CRUD**: `create_hostgroup`, `delete_hostgroup`
- **Provider detection** from IP CIDR ranges (OVH, Scaleway, Hetzner, AWS, Cogent, Melbicom, Psychz, Selectel, Vultr, GTHost, InterKVM)
- **Country extraction** from hostnames
- **VPN health columns**: VPN protocol status with color coding
- **Agent version** and **templates** in reports
- **Bandwidth color coding**: dark red ≥800, red ≥650, orange ≥500, green ≥200 Mbps
- **Structured logging**: JSON error logs + analytics to `~/.zbbx-mcp/`
- **Sentry integration** with comprehensive token/secret scrubbing
- **`@logged` decorator** on all tools (duration, params, response size)
- **Server integration tests** (JSON-RPC subprocess harness)
- **CI/CD**: GitHub Actions test matrix (Python 3.10–3.13)
- **Docker**: Dockerfile + docker-compose.yml
- **GitHub templates**: PR template, bug report, feature request
- **Documentation**: CONTRIBUTING, CODE_OF_CONDUCT, SECURITY, comprehensive README

### Security
- Slack webhook URL only from env var (no parameter injection)
- `asyncio.gather(return_exceptions=True)` for graceful partial failures
- Secret macros (type=1) scrubbed from rollback snapshots
- PRODUCT_MAP restricted to .json files only
- Sentry scrubbing covers exceptions, breadcrumbs, expanded patterns
- Config warnings use structured logging
- Connection cleanup via atexit handler
- Circular import eliminated (classify.py standalone module)

## [0.2.0] - 2026-03-24

### Added
- 60 tools across 24 modules (was 12)
- **Rollback system**: `get_rollback_history`, `rollback_last`, `rollback_by_index` with pre-mutation snapshots
- **Business inventory**: `get_server_map`, `get_product_summary`, `get_server_load`, `get_high_cpu_servers`, `get_underloaded_servers`, `get_provider_summary`
- **Triggers**: `get_triggers`, `create_trigger`, `update_trigger`, `delete_trigger`
- **Templates**: `get_templates`, `link_template`, `unlink_template`
- **Maintenance**: `get_maintenance`, `create_maintenance`, `delete_maintenance`
- **Events & Trends**: `get_events`, `get_trends`
- **Discovery**: `get_discovery_rules`
- **Configuration**: `export_configuration`, `import_configuration`
- **Scripts**: `get_scripts`, `execute_script`
- **Services & SLA**: `get_services`, `get_sla`
- **Macros**: `get_host_macros`, `get_global_macros`, `set_host_macro`, `delete_host_macro`
- **Host/Item CRUD**: create, update, delete
- **Dashboards**: `get_dashboards`, `get_dashboard_detail`
- Configurable product mapping via `ZABBIX_PRODUCT_MAP`

## [0.1.1] - 2026-03-24

### Changed
- `create_server()` factory replaces module-level side effects
- Conditional tool registration instead of post-registration removal
- Atomic request IDs via `itertools.count`

### Added
- `check_connection` health tool
- Startup validation for missing URL/token
- Transport error handling in all tools

## [0.1.0] - 2026-03-23

### Added
- Initial release with 6 tools
- Multi-instance support, read-only mode, HTTPS enforcement
- Async HTTP/2 client with connection pooling
