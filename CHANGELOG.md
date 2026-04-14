# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

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
  - VPN Uptime by Country (SLA dashboard)
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
- Traffic anomaly detection and geo-block monitoring
- Cost management via host macros
- Slack integration
- Structured JSON logging with Sentry support
- Docker support with docker-compose
- GitHub Actions CI (Python 3.10–3.13)
