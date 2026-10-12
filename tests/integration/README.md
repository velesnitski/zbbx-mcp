# Integration tests

End-to-end tests that start the MCP server as a stdio subprocess and drive it
over raw JSON-RPC against a real, throwaway Zabbix 7.0 LTS stack. They catch
what the recorded-fake wire tests (`tests/wiretest.py`) cannot: API
parameters a live server rejects (the `-32602` class), selector renames, and
behaviour that depends on real data (quoted item keys, sub-host problems,
trend reads).

Without `ZBBX_IT_URL` every test here is **skipped** with a stated reason, so
`uv run pytest` stays green on a machine without Docker. CI runs them from
`.github/workflows/integration.yml` (manual, nightly, and on pushes to `dev`
that touch `src/`).

## Run locally (needs Docker)

```bash
export COMPOSE_FILE=tests/integration/docker-compose.yml
export ZBBX_IT_URL=http://localhost:8080
export ZBBX_IT_STATE=/tmp/zbbx-it-seed.json

docker compose up -d                       # postgres + zabbix-server + zabbix-web
uv run python tests/integration/seed.py --trends-sql /tmp/zbbx-it-trends.sql
docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -q -U zabbix -d zabbix -f - < /tmp/zbbx-it-trends.sql
uv run pytest -m integration -q tests/integration
docker compose down -v                     # throw the stack away
```

The first start creates the database schema and takes a minute or two;
`seed.py` waits for the API (`--wait`, default 300 s).

## What the seed creates

`seed.py` logs in with the official image's default administrator account —
the public default every fresh container ships with, used only against this
disposable stack — and creates, idempotently:

| Object | Values |
|--------|--------|
| Host groups | `app_free`, `app_paid` |
| Hosts | `srv-aq9001`..`srv-aq9004`, `srv-bv9001`, and the sub-host `srv-aq9001 aq9002` (agent interfaces on 192.0.2.0/24, `{$COST_MONTH}` macros) |
| Trapper items | `net.if.in[eth0]`, `net.if.in["enp3s0"]` (quoted key, on `srv-bv9001` only), `system.cpu.util[,idle]` |
| Trigger | `CPU idle below 5% on {HOST.NAME}` on `srv-aq9001`, fired by the pushed values |
| API token | `zbbx-mcp-integration`, handed to the MCP |

Live values go in through `history.push` (falling back to the trapper
protocol on port 10051); a week of hourly trends is written as SQL
(`--trends-sql`) because the server only computes the current hour's trend.
Ids and the token are written to `ZBBX_IT_STATE` (mode 0600), which the
fixtures in `conftest.py` read.

## Knobs

| Variable | Purpose |
|----------|---------|
| `ZBBX_IT_URL` | Zabbix frontend URL; unset = skip the suite |
| `ZBBX_IT_STATE` | Seed state file (default: `<tempdir>/zbbx-it-seed.json`) |
| `ZBBX_IT_TOKEN` | Use this API token instead of the one in the state file |
| `ZBBX_IT_CMD` | Command that starts the server (default: the env's `zbbx-mcp`; e.g. `uv run zbbx-mcp`) |
| `ZBBX_IT_ADMIN_USER` / `ZBBX_IT_ADMIN_PASSWORD` | Seed login, if the stack was started with non-default credentials |
