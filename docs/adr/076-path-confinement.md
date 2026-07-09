# ADR 076: Filesystem confinement for caller-supplied paths

**Status:** Accepted
**Date:** 2026-07-09

## Problem

A sibling MCP (yt-mcp) disclosed advisory GHSA-99mq-fjjc-6v9j: its
`add_attachment` tool took a caller-controlled `file_path`, read it with
only an existence check — no directory confinement or traversal guard —
and uploaded the bytes to a YouTrack issue, collapsing arbitrary local
file read and external egress into one tool (CWE-22/CWE-73, CVSS 7.5). A
secondary write-traversal let a caller-derived name escape a snapshot
directory.

Validating zbbx-mcp against the same class found the root cause present,
in a weaker form:

1. **Unconfined caller-path reads.** `audit_external_ips`,
   `analyze_cost_import`, `reconcile_billing_audit`,
   `find_stale_billing_ips`, `import_costs_by_ip`, `import_cluster_ip_fees`,
   `import_from_xlsx`, and the `export_cost_audit` source workbook all took
   a `file_path`/`source_xlsx` and did `expanduser` → `open()` with only an
   existence check. `get_telemetry_summary` read a caller `log_path` the
   same way. Under indirect prompt injection a caller could read
   `~/.claude.json` (holds the MCP tokens), SSH keys, `/etc/*`, etc.; the
   content lands in model context and can be relayed onward via
   `send_slack_message`. Not the one-shot 7.5 (our Slack tools generate
   from live Zabbix data and do not read files), but the same
   confused-deputy read primitive.
2. **Inconsistent write confinement.** `safe_output_path` existed but used
   `path.startswith(root)` — a prefix match with no separator boundary, so
   `~/Downloads-evil` passed (the exact "sibling-prefix" case yt-mcp
   hardened with `commonpath`) — and did not guard the `filename`
   component. Several report/export writers (`generate_server_report`,
   `generate_full_report`, `generate_infra_report`, `export_dashboard`,
   `analyze_cost_import`, `reconcile_billing_audit`, `import_from_xlsx`,
   `export_cost_audit`, `import_costs_by_ip` export) bypassed it entirely
   with a raw `os.path.join` on a caller `output_dir`.

## Decision

One shared confinement layer in `utils.py`, applied to every
caller-supplied read and write path:

- **`_allowed_roots()`** — realpath'd allowlist: `~/Downloads`,
  `~/Documents`, `~/Desktop`, `/tmp`, and the system temp dir, extendable
  via the `ZBBX_FILE_ROOTS` env var (`os.pathsep`-separated).
- **`_within_roots(resolved)`** — membership via `os.path.commonpath`
  (not `startswith`), so a sibling like `<root>-evil` cannot pass.
- **`confined_input_path(path, max_bytes=100 MB)`** — `realpath`
  (symlink-resolved *before* the check, so a symlink planted in a root
  can't redirect the read out), root check, existence check, size cap.
  Raises `ValueError` on any violation.
- **`safe_output_path(dir, filename)`** — now confines via
  `_within_roots` and rejects any `filename` that is not a bare basename.
- **`confined_output_path(path)`** — for tools taking a full output file
  path; confines the parent dir to the roots and creates it.

Every reader now resolves its path through `confined_input_path`; every
caller-`output_dir`/`output_path` writer routes through
`safe_output_path`/`confined_output_path`. Reader `except` clauses that
did not already catch `ValueError` were widened so a rejected path returns
a friendly message instead of propagating.

## Consequences

- A prompt-injected caller can no longer read `~/.ssh`, `~/.claude.json`,
  `/etc/*`, or any path outside the user-data roots, nor write reports
  outside them. Legitimate workflows (cost CSV/XLSX in `~/Downloads`) are
  unaffected; operators needing another location set `ZBBX_FILE_ROOTS`.
- The `safe_output_path` sibling-prefix bug is fixed; output filenames can
  no longer carry traversal.
- Tool count unchanged (163). +19 tests, 685 → 704.

## Not included

- **Read-only gating of the read tools.** Confinement is the primary
  control and closes the sensitive-file class regardless of
  `ZABBIX_READ_ONLY`; reclassifying `audit_external_ips` etc. into
  `WRITE_TOOLS` was judged unnecessary and behaviour-changing.
- **Operator-configured paths** (`ZABBIX_PRODUCT_MAP`, analytics/log
  defaults, the `~/.zbbx-mcp` snapshot dir) are set by the operator via
  env, not by tool callers, and are left unconfined by design.
