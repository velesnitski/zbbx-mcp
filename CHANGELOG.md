# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [1.0.1] - 2026-03-25

### Changed
- Traffic fetch optimization: 2.3x faster reports (3.5s → 1.2s) using fast filter by known physical NIC keys with fallback for uncommon interfaces
- Host cache (60s TTL) avoids redundant host.get calls across tools

### Added
- 46 new tests (78 → 124): excel.py, logging.py, utils.py, data helpers
- Full physical NIC coverage (28 interface key patterns)

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
- **VPN health columns**: VPN Primary, VPN Secondary, VPN Tertiary status with color coding
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
