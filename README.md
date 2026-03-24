# zbbx-mcp

Zabbix MCP server for Claude Code, n8n, and any MCP-compatible client.

Gives LLMs direct access to your Zabbix monitoring — hosts, problems, host groups, and acknowledgements.

## Quick start

### 1. Get a Zabbix API token

In Zabbix UI: **User settings** → **API tokens** → **Create API token**.

### 2. Add to Claude Code

```bash
claude mcp add zabbix \
  -e ZABBIX_URL=https://your-zabbix.example.com \
  -e ZABBIX_TOKEN=your_api_token \
  -- uvx --from "git+https://github.com/velesnitski/zbbx-mcp" zbbx-mcp
```

### 3. Verify

```
You: show current problems with severity >= Warning
```

## What it does

**62 tools** across 24 modules:

| Category | Tools |
|----------|-------|
| **Hosts** | `search_hosts`, `get_host`, `create_host`, `update_host`, `delete_host` |
| **Problems** | `get_problems`, `get_problem_detail`, `acknowledge_problem` |
| **Host Groups** | `get_hostgroups`, `create_hostgroup`, `delete_hostgroup` |
| **Triggers** | `get_triggers`, `create_trigger`, `update_trigger`, `delete_trigger` |
| **Templates** | `get_templates`, `link_template`, `unlink_template` |
| **Items & Metrics** | `get_host_items`, `create_item`, `update_item`, `delete_item`, `get_item_history`, `get_graphs` |
| **Events & Trends** | `get_events`, `get_trends` |
| **Dashboards** | `get_dashboards`, `get_dashboard_detail` |
| **Maintenance** | `get_maintenance`, `create_maintenance`, `delete_maintenance` |
| **Discovery** | `get_discovery_rules` |
| **Configuration** | `export_configuration`, `import_configuration` |
| **Scripts** | `get_scripts`, `execute_script` |
| **Services & SLA** | `get_services`, `get_sla` |
| **Macros** | `get_host_macros`, `get_global_macros`, `set_host_macro`, `delete_host_macro` |
| **Inventory** | `get_server_map`, `get_product_summary`, `get_server_load`, `get_high_cpu_servers`, `get_underloaded_servers`, `get_provider_summary` |
| **Rollback** | `get_rollback_history`, `rollback_last`, `rollback_by_index` |
| **Alerts** | `get_alerts`, `get_alert_summary` |
| **Users** | `get_users` |
| **Proxies** | `get_proxies` |
| **Maps** | `get_maps`, `get_map_detail` |
| **Media & Actions** | `get_media_types`, `get_actions` |
| **Slack** | `send_slack_message`, `send_slack_report` |
| **Reports** | `generate_server_report` (Excel export) |
| **Health** | `check_connection` |

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `ZABBIX_URL` | Yes | Zabbix server URL (e.g., `https://zabbix.example.com`) |
| `ZABBIX_TOKEN` | Yes | Zabbix API token |
| `ZABBIX_READ_ONLY` | No | Set to `true` to disable write operations |
| `DISABLED_TOOLS` | No | Comma-separated tool names to disable |
| `ZABBIX_ALLOW_HTTP` | No | Set to `1` to allow non-HTTPS connections |
| `ZABBIX_PRODUCT_MAP` | No | JSON file path or inline JSON mapping host groups to products |
| `SLACK_WEBHOOK_URL` | No | Slack webhook URL for `send_slack_message` / `send_slack_report` |

## Multi-instance setup

Connect to multiple Zabbix servers simultaneously:

```bash
claude mcp add zabbix \
  -e ZABBIX_INSTANCES=prod,staging \
  -e ZABBIX_PROD_URL=https://zabbix.prod.company.com \
  -e ZABBIX_PROD_TOKEN=prod_token \
  -e ZABBIX_STAGING_URL=https://zabbix.staging.company.com \
  -e ZABBIX_STAGING_TOKEN=staging_token \
  -- uvx --from "git+https://github.com/velesnitski/zbbx-mcp" zbbx-mcp
```

Then specify the instance in your queries:

```
You: show problems on the staging instance
```

## Alternative installation

### From local clone

```bash
git clone https://github.com/velesnitski/zbbx-mcp.git
cd zbbx-mcp
pip install -e .
```

```bash
claude mcp add zabbix \
  -e ZABBIX_URL=https://your-zabbix.example.com \
  -e ZABBIX_TOKEN=your_api_token \
  -- zbbx-mcp
```

### HTTP transport (for n8n, Make.com, etc.)

```bash
ZABBIX_URL=https://your-zabbix.example.com \
ZABBIX_TOKEN=your_token \
zbbx-mcp --transport sse --port 8000
```

## Docker

```bash
docker compose up -d
```

Or build and run directly:

```bash
docker build -t zbbx-mcp .
docker run -p 8000:8000 \
  -e ZABBIX_URL=https://your-zabbix.example.com \
  -e ZABBIX_TOKEN=your_api_token \
  zbbx-mcp
```

## Development

```bash
git clone https://github.com/velesnitski/zbbx-mcp.git
cd zbbx-mcp
pip install -e ".[test]"
pytest
```

## Security

- API tokens are passed via environment variables, never hardcoded
- HTTPS enforced by default (override with `ZABBIX_ALLOW_HTTP=1`)
- Error messages truncated to 200 chars to prevent internal detail leaks
- Optional read-only mode via `ZABBIX_READ_ONLY=true`
- Individual tools can be disabled via `DISABLED_TOOLS`

## Requirements

- Python 3.10+
- Zabbix 6.0+ (JSON-RPC API)
- [uv](https://docs.astral.sh/uv/getting-started/installation/) (recommended) or pip

## License

MIT
