# ADR 028: `bulk_diagnose` + `diagnose_subnet`

**Status:** Accepted
**Date:** 2026-05-21

## Problem

`diagnose_host` (ADR 026) takes a single host and returns a verbose
per-host verdict. Real ops workflows consistently want a different
shape:

- "Diagnose every host in this outage cluster" (5–20 hosts on a /24).
- "Diagnose every host in country X."
- "Diagnose this list of hosts I just pulled from a Slack alert."

Today this means looping `diagnose_host` by hand and reading N
verbose reports. The verdict label is the only field the operator
actually scans first; the rest is detail-on-demand.

## Decision

Ship two composite tools sharing the `diagnose_host` data-gather
pipeline. Both live in `tools/diagnose.py`.

### `bulk_diagnose(hosts="", group="", country="")`

Fan-out wrapper. Resolves the target set from three composable
filters:

- `hosts` — comma- or space-separated names (explicit list).
- `group` — Zabbix host-group name.
- `country` — ISO-2 / ISO-3 / English name (uses
  `resolve_country()`).

Internally:

1. One `host.get` call to resolve all matching hosts (with
   `selectInterfaces` + `selectGroups`).
2. One `item.get` call batching all hostids together; results
   grouped by `hostid` in Python.
3. Per-host fan-out with `asyncio.Semaphore(10)` calling
   `_collect_diagnosis_inner` (the shared helper extracted from
   `diagnose_host`).
4. Renders a compact markdown table sorted by verdict severity
   (`down → traffic_lost → https_down → degraded → healthy`).

Safety: `max_hosts` clamped to a module-level cap of 50 per call.
The rotation-history step is skipped (`rotation_days=0`) in bulk
mode to keep fan-out responsive — operators can drill in to any
flagged host with `diagnose_host` if they want the rotation
detail.

Lands in the `ops` tier (incident-response surface).

### `diagnose_subnet(subnet)`

Thin wrapper over the same bulk runner, expanding a CIDR or dotted
prefix to its in-subnet host set. Designed as the canonical
follow-on when `get_outage_clusters` returns a row like
"5 hosts on 1.2.3.0/24" — the operator pastes the CIDR in and gets
per-host verdicts for the cluster.

Supports three input forms:

- `"1.2.3.0/24"` — /24 CIDR (via the existing `subnet24()` helper).
- `"1.2.0.0/16"` — /16 CIDR (matches first two octets).
- `"1.2.3"` — dotted prefix without slash.

Unsupported forms (/28, garbage input, IPv6) return `False` from
the matcher — safer than producing a wrong match.

Lands in the `ops` tier.

### Shared helpers

Three new pure helpers in `diagnose.py`:

- `_verdict_primary_signal(facts) -> str` — one-line summary fitting
  a table cell (e.g. `"traffic 256→2.3 Mbps"` for `traffic_lost`,
  `"HTTPS down ~17h"` for `https_down`, `"3 active problem(s)"`
  for `degraded`).
- `_render_bulk_table(rows, total) -> str` — markdown table with
  flagged-count header. Sorts by verdict severity, truncates long
  action strings to 70 chars + ellipsis.
- `_ip_matches_subnet(ip, subnet) -> bool` — pure CIDR-or-prefix
  matcher. Tested for /24, /16, dotted prefix, malformed input,
  empty inputs, unsupported bit counts.

The existing `diagnose_host` was refactored to call the shared
`_collect_diagnosis_inner` (renamed from the inline body) plus a
new `_render_full_report` for the verbose output. Behaviour
unchanged.

## Test approach

18 new pure-helper tests in `test_analytics.py`:

- `_verdict_primary_signal` (5 cases): healthy, down, traffic_lost
  with Mbps formatting, https_down with hour formatting, degraded
  with problem count.
- `_render_bulk_table` (4 cases): empty list, severity sort order,
  flagged-count header, long-action truncation.
- `_ip_matches_subnet` (9 cases): /24 match + no-match, /16 match +
  no-match, dotted prefix match + no-match, empty inputs,
  unsupported bit count, malformed input.

The async tool wrappers are covered by the registration / smoke
test (asserts 160 tools and presence of `bulk_diagnose` +
`diagnose_subnet`).

## Consequences

- Tool count 158 → 160.
- Test count 421 → 439.
- `WRITE_TOOLS` unchanged.
- No new env vars.
- API-compat: `diagnose_host` callers see no behaviour change; the
  internal refactor is transparent.
- The `_BULK_CONCURRENCY = 10` and `_BULK_MAX_HOSTS = 50` constants
  are module-level — tune in place if Zabbix server CPU starts
  feeling the load.

## Not included

- **`diagnose_cluster(cluster_id)`** — the natural pairing with
  `get_outage_clusters`. Blocked because `get_outage_clusters`
  doesn't currently emit stable cluster keys. Plan: extend the
  cluster output to surface a `key` field, then wire this on top.
  Tracked as a follow-up.
- **Auto-acknowledge on adverse verdicts.** Diagnostic tools stay
  read-only by design; remediation runs through the existing
  `acknowledge_problem` / `bulk_acknowledge` surface. Operators
  (or the LLM client) compose them explicitly.
- **Streaming progress updates.** With concurrency=10 and ≤50
  hosts, fan-out finishes in under 10 seconds in practice. Adding
  MCP streaming for a one-shot table is over-engineering.
- **A `--verbose` flag** that returns per-host full reports
  instead of table rows. If you need details for one row, call
  `diagnose_host` on that hostname. Two clean tools beat one
  parameterised tool here.
