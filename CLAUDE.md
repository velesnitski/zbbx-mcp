# CLAUDE.md

Instructions for Claude Code when working in this repository.

## Project

Zabbix MCP server — 127 tools across 43 modules. Python 3.10+, FastMCP framework, async httpx HTTP/2 client.

## Commands

```bash
uv run pytest                              # run all tests (~180 tests, ~2s)
uv run pytest tests/test_registration.py   # tool registration only
uv run pytest tests/test_server.py         # JSON-RPC subprocess test
uv run pytest -k "test_name"               # single test by name
```

## Architecture

### Core modules

| File | Purpose |
|------|---------|
| `server.py` | Entry point, `create_server()` factory, response compression |
| `client.py` | `ZabbixClient` — async httpx HTTP/2, JSON-RPC, rollback log |
| `config.py` | Env var loading, multi-instance config |
| `data.py` | Shared data fetching, `ServerRow`, `extract_country()`, constants, region maps |
| `fetch.py` | Shared fetch helpers (`fetch_traffic_map`, `fetch_cpu_map`, `fetch_enabled_hosts`) |
| `classify.py` | Host classification + provider detection (93 providers, 368 CIDRs). No tools/ imports |
| `rollback.py` | Pre-mutation snapshots, `SNAPSHOT_CONFIG` |
| `excel.py` | Shared Excel formatting (bandwidth thresholds, color fills) |
| `formatters.py` | Output formatters (severity labels, host/trigger formatting) |
| `logging.py` | JSON structured logging + Sentry integration |

### Tool modules (43 files in `tools/`)

| Module | Tools | Purpose |
|--------|-------|---------|
| `hosts.py` | 7 | `search_hosts`, `get_host`, CRUD, `get_server_clusters`, `search_hosts_by_location` |
| `problems.py` | 3 | `get_problems`, `get_problem_detail`, `acknowledge_problem` |
| `hostgroups.py` | 3 | CRUD for host groups |
| `triggers.py` | 4 | CRUD for triggers |
| `templates.py` | 3 | `get_templates`, link/unlink |
| `items.py` | 7 | `get_host_items`, `search_items`, CRUD, `get_item_history`, `get_graphs` |
| `events.py` | 6 | `get_events`, `get_trends`, `get_event_frequency`, `get_correlated_events` |
| `dashboards.py` | 3 | `get_dashboards`, `get_dashboard_detail`, `find_host_dashboard` |
| `maintenance.py` | 3 | CRUD for maintenance windows |
| `discovery.py` | 1 | `get_discovery_rules` |
| `configuration.py` | 2 | `export_configuration`, `import_configuration` |
| `scripts.py` | 2 | `get_scripts`, `execute_script` |
| `services.py` | 2 | `get_services`, `get_sla` |
| `macros.py` | 6 | Host/global macros, `set_bulk_macro` |
| `rollback_tools.py` | 3 | `get_rollback_history`, `rollback_last`, `rollback_by_index` |
| `inventory_map.py` | 3 | `get_server_map`, `get_product_summary`, `get_provider_summary` |
| `inventory_load.py` | 8 | `get_server_load`, `get_high_cpu_servers`, `get_underloaded_servers`, `get_unknown_providers`, `identify_providers` |
| `alerts.py` | 2 | `get_alerts`, `get_alert_summary` |
| `users.py` | 1 | `get_users` |
| `proxies.py` | 1 | `get_proxies` |
| `maps.py` | 2 | `get_maps`, `get_map_detail` |
| `media.py` | 2 | `get_media_types`, `get_actions` |
| `slack.py` | 2 | `send_slack_message`, `send_slack_report` |
| `costs.py` | 4 | `import_server_costs`, `set_bulk_cost`, `import_costs_by_ip`, `get_cost_summary` |
| `traffic.py` | 5 | `detect_traffic_anomalies`, `detect_traffic_drops`, `get_traffic_report`, `detect_regional_anomalies`, `get_traffic_drop_timeline` |
| `trends_health.py` | 5 | `get_trends_batch`, `get_server_dashboard`, `get_health_assessment`, `get_shutdown_candidates`, `get_capacity_planning` |
| `trends_compare.py` | 3 | `compare_servers`, `get_stale_servers`, `get_recent_changes` |
| `geo_traffic.py` | 4 | `detect_geo_blocks`, `get_geo_traffic_trends`, `get_expansion_report`, `get_regional_density_map` |
| `geo_health.py` | 6 | `get_service_uptime_report`, `get_service_health_matrix`, `get_latency_estimate`, `get_servers_by_ping` |
| `health.py` | 4 | `check_connection`, `get_active_problems`, `get_agent_unreachable`, `get_error_rate` |
| `availability.py` | 2 | `get_host_availability`, `get_low_memory_servers` |
| `analysis.py` | 4 | `get_predictive_alerts`, `get_incident_report`, `correlate_logs`, `analyze_server_roles` |
| `audit.py` | 1 | `get_audit_log` |
| `domains.py` | 3 | `get_domain_list`, `get_domain_status`, `get_ssl_expiry` |
| `web_scenarios.py` | 2 | `get_web_scenarios`, `get_web_scenario_status` |
| `executive.py` | 8 | `get_executive_dashboard`, `get_month_over_month`, `get_fleet_risk_score`, `get_sla_dashboard`, `get_report_snapshot`, `get_peak_analysis`, `get_product_audit`, `generate_product_map` |
| `ceo_report.py` | 1 | `generate_ceo_report` — full HTML report with all analytics |
| `report.py` | 1 | `generate_server_report` (Excel) |
| `infra_report.py` | 1 | `generate_infra_report` (Excel) |
| `full_report.py` | 1 | `generate_full_report` (Excel, 8 sheets) |
| `html_report.py` | 1 | `generate_html_report` (HTML, dark theme) |
| `dashboard_report.py` | 2 | `export_dashboard`, `classify_external_ips` |

### Environment variables

| Variable | Purpose |
|----------|---------|
| `ZABBIX_URL` / `ZABBIX_TOKEN` | Required: Zabbix connection |
| `ZABBIX_SERVICE_CHECK_KEY` | Primary service health check item key |
| `ZABBIX_SERVICE2_CHECK_KEY` | Secondary service check key |
| `ZABBIX_SERVICE3_CHECK_KEY` | Tertiary service check key |
| `ZABBIX_CONNECTIONS_KEY` | Connection count item key |
| `ZABBIX_BILLING_RENAMES` | Billing name translations (`old1:new1,old2:new2`) |
| `ZABBIX_PRODUCT_MAP` | JSON file mapping host groups to products |
| `ZABBIX_HIDE_PRODUCTS` | Comma-separated products to hide from reports |
| `ZABBIX_TRAFFIC_UNIT` | Set `bytes` if net.if.in returns bytes/sec (default: bits/sec) |
| `ZABBIX_COMPACT` / `ZABBIX_COMPACT_TOOLS` | Token optimization |
| `ZABBIX_RESPONSE_BUDGET` | Max chars per response (default: 6000) |
| `ZABBIX_READ_ONLY` | Disable write operations |

## Adding a new tool

1. Add `@mcp.tool()` async function inside `register()` in the appropriate `tools/*.py` file
2. Gate with `if "tool_name" not in skip:`
3. If the tool mutates data, add it to `WRITE_TOOLS` in `tools/__init__.py`
4. Add the tool name to `EXPECTED_TOOLS` in `tests/test_registration.py`
5. Update the tool count assertion in `tests/test_server.py`
6. Run `uv run pytest` — all tests must pass

## Rules

- **Never commit `tasks.md`** — it's in `.gitignore`
- **No sensitive data in code or git** — no real hostnames, company names, product names, protocol names, or server naming patterns
- **No hardcoded service identifiers** — all service check keys configurable via env vars
- **Country filter** — always use `extract_country(hostname)` for exact 2-letter match
- **Compact by default** — use `max_results` with sensible defaults, group repetitive entries
- **Error handling** — catch `(httpx.HTTPError, ValueError)`, return user-friendly string
- **Token budget** — responses truncated by `ZABBIX_RESPONSE_BUDGET`. Design output to fit
- **Imports** — `classify.py` must not import from `tools/` (circular import risk)

## Branching

- `dev` — active development branch
- `main` — stable, always fast-forward merged from `dev`
- Always confirm before `git push`
