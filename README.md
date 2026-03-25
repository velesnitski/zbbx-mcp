# zbbx-mcp

Zabbix MCP server for [Claude Code](https://claude.com/claude-code), [n8n](https://n8n.io), and any MCP-compatible client. Talk to your Zabbix monitoring in natural language.

## Quick start

### 1. Get a Zabbix API token

In Zabbix UI: **User settings** → **API tokens** → **Create API token**.

### 2. Install in Claude Code

**Option A — via CLI** (recommended):

> **Prerequisite:** [uv](https://docs.astral.sh/uv/) is required. Install with: `curl -LsSf https://astral.sh/uv/install.sh | sh`

```bash
claude mcp add zabbix \
  -e ZABBIX_URL=https://your-zabbix.example.com \
  -e ZABBIX_TOKEN=your_api_token \
  -- uvx --from "git+https://github.com/velesnitski/zbbx-mcp" zbbx-mcp
```

**Option B — manually edit settings:**

Edit `~/.claude/settings.json` (global) or `.claude/settings.json` in your project root (per-project):

```json
{
  "mcpServers": {
    "zabbix": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/velesnitski/zbbx-mcp", "zbbx-mcp"],
      "env": {
        "ZABBIX_URL": "https://your-zabbix.example.com",
        "ZABBIX_TOKEN": "your_api_token"
      }
    }
  }
}
```

> **Troubleshooting: MCP server not found**
>
> If Claude Code can't find `uvx` at startup (e.g., `/mcp` doesn't show the server), use the full path instead:
>
> ```json
> "command": "/full/path/to/uvx"
> ```
>
> Find the full path with `which uvx` (typically `~/.local/bin/uvx` or `/opt/homebrew/bin/uvx`).

### 3. Restart Claude Code

```bash
claude
```

You should see `zabbix` listed when Claude starts. Try asking: *"Show current problems with severity >= Warning"*

## What it does

**72 tools** across 29 modules:

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
| **Costs** | `import_server_costs`, `set_bulk_cost`, `get_cost_summary` |
| **Traffic** | `detect_traffic_anomalies`, `detect_traffic_drops`, `get_traffic_report` |
| **Reports** | `generate_server_report`, `generate_infra_report`, `export_dashboard`, `generate_full_report` (Excel) |
| **Health** | `check_connection` |

### Report filtering

The `generate_full_report` tool supports filtering by country and product:

```
generate_full_report(country="in")                    # India servers only
generate_full_report(product="Premium")               # Premium tier only
generate_full_report(country="de", product="Free")    # German free servers
```

Traffic tools also support country filtering:

```
detect_traffic_drops(country="in")                    # India traffic drops
detect_traffic_anomalies(country="nl")                # Netherlands anomalies
get_traffic_report(country="us")                      # US traffic sorted
```

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
| `SENTRY_DSN` | No | Sentry DSN for error tracking — just set the env var, SDK is included |
| `{$BW_LIMIT}` | No | Per-host Zabbix macro for bandwidth limit in Mbps (default: 800). Set on hosts for accurate BW Util % |
| `ZABBIX_LOG_FILE` | No | Error log path (default: `~/.zbbx-mcp/zbbx-mcp.log`) |
| `ZABBIX_ANALYTICS_FILE` | No | Analytics log path (default: `~/.zbbx-mcp/analytics.log`) |

## Multi-instance setup

Connect multiple Zabbix servers to a single MCP server. Each tool gets an optional `instance` parameter — the LLM picks the right instance from context.

### Configuration

Set `ZABBIX_INSTANCES` to a comma-separated list of instance names, then provide `ZABBIX_{NAME}_URL` and `ZABBIX_{NAME}_TOKEN` for each:

```bash
ZABBIX_INSTANCES=prod,staging
ZABBIX_PROD_URL=https://zabbix.prod.company.com
ZABBIX_PROD_TOKEN=prod_token
ZABBIX_STAGING_URL=https://zabbix.staging.company.com
ZABBIX_STAGING_TOKEN=staging_token
```

Instance names are arbitrary — use whatever makes sense: `prod,staging`, `dc1,dc2`, etc. The name is uppercased to form the env var prefix (`staging` → `ZABBIX_STAGING_URL`).

### How it works

- **No `ZABBIX_INSTANCES`** — single-instance mode, fully backward compatible. Uses `ZABBIX_URL` / `ZABBIX_TOKEN` as before.
- **First instance** falls back to unprefixed `ZABBIX_URL` / `ZABBIX_TOKEN` if its prefixed vars are not set.
- **Explicit `instance` parameter** — every tool accepts an optional `instance` param to target a specific instance.
- **Default** — if no instance is specified, the first configured instance is used.
- **Global settings** — `ZABBIX_READ_ONLY` and `DISABLED_TOOLS` apply to all instances.

### Claude Code example

```bash
claude mcp add zabbix \
  -e ZABBIX_INSTANCES=prod,staging \
  -e ZABBIX_PROD_URL=https://zabbix.prod.company.com \
  -e ZABBIX_PROD_TOKEN=prod_token \
  -e ZABBIX_STAGING_URL=https://zabbix.staging.company.com \
  -e ZABBIX_STAGING_TOKEN=staging_token \
  -- uvx --from "git+https://github.com/velesnitski/zbbx-mcp" zbbx-mcp
```

Then just ask:

```
You: show problems on the staging instance
```

## Verify the server works

**Check that `uv` is installed** (required for the `uvx` command):

```bash
uv --version
```

If not installed, get it with:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Test the server starts and responds** by sending a JSON-RPC `initialize` request via stdio:

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"capabilities":{},"clientInfo":{"name":"test","version":"1.0.0"},"protocolVersion":"2024-11-05"}}' \
  | ZABBIX_URL="https://your-zabbix.example.com" \
    ZABBIX_TOKEN="your_api_token" \
    uvx --from git+https://github.com/velesnitski/zbbx-mcp zbbx-mcp
```

A successful response looks like:

```json
{"jsonrpc":"2.0","id":1,"result":{"serverInfo":{"name":"zabbix"},"capabilities":{"tools":{}},...}}
```

If you see `command not found: uvx`, install `uv` first (see above).

## Setup for Windows

**1. Install uv** (Python package runner):

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Restart your terminal after installation.

**2. Get a Zabbix API token:**

In Zabbix UI → **User settings** → **API tokens** → **Create API token** → copy the token.

**3. Add the MCP server to Claude Code:**

```powershell
claude mcp add zabbix `
  -e ZABBIX_URL=https://your-zabbix.example.com `
  -e ZABBIX_TOKEN=your_api_token `
  -- uvx --from git+https://github.com/velesnitski/zbbx-mcp zbbx-mcp
```

> **Note:** If Claude Code can't find `uvx`, use the full path. Find it with `where uvx` (typically `%USERPROFILE%\.local\bin\uvx.exe`) and set `"command"` to that path in your settings.

**4. Restart Claude Code** and try: *"Show current Zabbix problems"*

## Alternative installation

### From local clone

```bash
git clone https://github.com/velesnitski/zbbx-mcp.git
cd zbbx-mcp
pip install -e .
```

Then use in settings:

```json
{
  "mcpServers": {
    "zabbix": {
      "command": "zbbx-mcp",
      "env": {
        "ZABBIX_URL": "https://your-zabbix.example.com",
        "ZABBIX_TOKEN": "your_api_token"
      }
    }
  }
}
```

### Run directly with Python

```bash
git clone https://github.com/velesnitski/zbbx-mcp.git
cd zbbx-mcp
pip install -e .
zbbx-mcp
```

## Using with n8n, Langchain, and other HTTP clients

By default the server uses **stdio** transport (for Claude Code). For integration with **n8n**, **Langchain**, **OpenAI Agents SDK**, or any HTTP-based MCP client, start the server in **SSE** or **streamable-http** mode:

### Start the server with SSE transport

```bash
ZABBIX_URL="https://your-zabbix.example.com" \
ZABBIX_TOKEN="your_api_token" \
uvx --from git+https://github.com/velesnitski/zbbx-mcp zbbx-mcp --transport sse --port 8000
```

The server will be available at `http://localhost:8000/sse`.

### Start with streamable HTTP transport

```bash
ZABBIX_URL="https://your-zabbix.example.com" \
ZABBIX_TOKEN="your_api_token" \
uvx --from git+https://github.com/velesnitski/zbbx-mcp zbbx-mcp --transport streamable-http --port 8000
```

The server will be available at `http://localhost:8000/mcp`.

### CLI options

| Flag | Default | Description |
|------|---------|-------------|
| `--transport` | `stdio` | Transport protocol: `stdio`, `sse`, or `streamable-http` |
| `--host` | `0.0.0.0` | Host to bind to |
| `--port` | `8000` | Port to bind to |

### n8n setup

1. Start the server in SSE mode (see above)
2. In n8n, add an **MCP Client** node
3. Set the MCP server URL to `http://localhost:8000/sse`
4. The Zabbix tools will be available as actions in your n8n workflows

### Docker

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

Override transport and port:

```bash
docker run -d -p 9000:9000 \
  -e ZABBIX_URL="..." -e ZABBIX_TOKEN="..." \
  zbbx-mcp --transport streamable-http --port 9000
```

## Product mapping

The inventory tools (`get_server_map`, `get_product_summary`, etc.) can classify hosts by business product. Without configuration, they use Zabbix host group names as-is.

To define custom mapping, set `ZABBIX_PRODUCT_MAP` to a JSON file path or inline JSON:

```json
{
  "free_servers": ["MyProduct", "Free"],
  "premium_servers": ["MyProduct", "Premium"],
  "templates": ["skip"]
}
```

See `product_map.example.json` for a reference.

## Development

```bash
git clone https://github.com/velesnitski/zbbx-mcp.git
cd zbbx-mcp
pip install -e ".[test]"
pytest
```

## Security

- Tokens are passed via environment variables — never hardcoded
- In **stdio** mode, the server has no network exposure (local pipes only)
- In **SSE/HTTP** mode, the server listens on a network port — bind to `127.0.0.1` if you don't need external access, or use a reverse proxy with authentication for production
- HTTPS enforced by default (non-HTTPS URLs are blocked unless `ZABBIX_ALLOW_HTTP=1`)
- Consider using a token with minimal required permissions (read-only if you don't need write tools)
- Error messages truncated to 200 chars to prevent leaking internal API details
- Optional read-only mode via `ZABBIX_READ_ONLY=true`
- Individual tools can be disabled via `DISABLED_TOOLS`

### Logging and privacy

All logs are stored **locally** on your machine at `~/.zbbx-mcp/`:

- `zbbx-mcp.log` — errors and warnings (JSON)
- `analytics.log` — tool call names, timing, and parameters (JSON)

**No data is sent externally** unless you explicitly set `SENTRY_DSN`. Logs never contain tokens, passwords, or sensitive content — only tool names, safe parameters, and error messages (truncated to 200 chars).

### Read-only mode

To disable all write operations (create, update, delete hosts/items/triggers, maintenance, scripts, macros):

```bash
ZABBIX_READ_ONLY=true
```

### Disable specific tools

Block individual tools by name (comma-separated, case-insensitive):

```bash
DISABLED_TOOLS=delete_host,execute_script,import_configuration
```

This removes the specified tools from the MCP server entirely — clients won't see them.

## Requirements

### Python 3.10+

Check your version:

```bash
python3 --version
```

If not installed or below 3.10:

- **macOS** (via [Homebrew](https://brew.sh)):
  ```bash
  brew install python@3.12
  ```
- **Ubuntu / Debian**:
  ```bash
  sudo apt update && sudo apt install python3 python3-pip
  ```
- **Windows**: download from [python.org](https://www.python.org/downloads/) or use `winget install Python.Python.3.12`

### uv (recommended)

[uv](https://docs.astral.sh/uv/) is a fast Python package manager. The `uvx` command (included with `uv`) is used to run the MCP server without a manual install.

```bash
uv --version   # check if already installed
```

If not installed:

- **macOS / Linux**:
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```
  Then restart your shell or run `source $HOME/.local/bin/env`.

- **macOS** (Homebrew alternative):
  ```bash
  brew install uv
  ```

- **Windows**:
  ```powershell
  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
  ```

If you prefer not to use `uv`, see [Alternative installation](#alternative-installation) for `pip`-based setup.

### Zabbix 6.0+

Requires Zabbix 6.0 or later with JSON-RPC API enabled. Tested on Zabbix 6.4.

## License

MIT
