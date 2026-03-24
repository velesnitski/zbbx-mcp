# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [0.2.0] - 2026-03-24

### Added
- 31 new tools across 10 modules (12 → 43 total)
- **Rollback system**: automatic pre-mutation snapshots for all CRUD operations
  - `get_rollback_history`: view all undoable operations
  - `rollback_last`: undo the most recent write
  - `rollback_by_index`: undo any specific operation from history
  - Bounded log (50 entries) per client instance
  - Captures full object state before updates and deletes
- **Business inventory tools** (unique — no competitor has these):
  - `get_server_map`: Product → Tier → Server → IP tree with filtering
  - `get_product_summary`: all products with Free/Paid breakdown
  - `get_server_load`: CPU/memory/traffic sorted by utilization
  - `get_high_cpu_servers`: find overloaded servers above threshold
  - `get_underloaded_servers`: find idle servers below threshold
  - Configurable product mapping via `ZABBIX_PRODUCT_MAP` (JSON file or env var)
- 28 new tools across 9 modules (12 → 40 total)
- **Triggers**: `get_triggers`, `create_trigger`, `update_trigger`, `delete_trigger`
- **Templates**: `get_templates`, `link_template`, `unlink_template`
- **Maintenance**: `get_maintenance`, `create_maintenance`, `delete_maintenance`
- **Events & Trends**: `get_events`, `get_trends`
- **Discovery**: `get_discovery_rules`
- **Configuration**: `export_configuration`, `import_configuration`
- **Scripts**: `get_scripts`, `execute_script`
- **Services & SLA**: `get_services`, `get_sla`
- **Macros**: `get_host_macros`, `get_global_macros`, `set_host_macro`, `delete_host_macro`
- **Host CRUD**: `create_host`, `update_host`, `delete_host`
- **Item CRUD**: `create_item`, `update_item`, `delete_item`
- 18 write tools blocked in read-only mode
- Human-readable value formatting for trends and history

## [0.1.1] - 2026-03-24

### Changed
- Replaced module-level side effects with `create_server()` factory
- Conditional tool registration instead of post-registration removal via private API
- Extracted global policy (`read_only`, `disabled_tools`) from per-instance config
- Atomic request IDs using `itertools.count`
- Removed dead `_domain_map` code from resolver

### Added
- `check_connection` health tool (calls `apiinfo.version`)
- Client `close()` method for proper connection lifecycle
- Startup validation for missing URL or token
- Transport error handling (`httpx.HTTPError`, `ValueError`) in all tools

## [0.1.0] - 2026-03-23

### Added
- Initial release
- Tools: `search_hosts`, `get_host`, `get_problems`, `get_problem_detail`, `acknowledge_problem`, `get_hostgroups`
- Multi-instance support via `ZABBIX_INSTANCES`
- Read-only mode and per-tool disabling
- HTTPS enforcement with localhost exception
- Async client with HTTP/2 and connection pooling
