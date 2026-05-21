# ADR 026: `diagnose_host` composite tool

**Status:** Accepted
**Date:** 2026-05-21

## Problem

Two recent operational incidents — one where a server host stayed
healthy at the agent layer but its traffic crashed to near zero,
and one where a domain endpoint's HTTPS check was failing — both
followed almost identical diagnostic chains, run by hand each
time:

1. `search_hosts` to confirm the host exists.
2. `get_problems(host=…)` to see active alerts.
3. `get_server_dashboard(host=…)` for traffic / CPU / agent state.
4. `get_external_ip_history(host=…)` for recent rotations.
5. `get_active_problems` for broader-fleet context.
6. `get_domain_status(search=…)` for domain-mode hosts.

Each call is ~5–30 seconds of round-trip; the full chain takes 3–5
minutes plus operator focus on which tools to call in which order.
The LLM also has to keep the dependency graph in its head ("first
check if it's a domain host so I know whether to skip the IP
rotation step").

Telemetry from the previous 16 days shows operators reach for
`search_hosts` (190 calls), `get_problems` (45), and
`get_server_dashboard` (35) — the building blocks of this chain —
constantly, but never bundled.

## Decision

Ship one composite tool `diagnose_host(host)` that runs the chain
server-side and returns one unified verdict-plus-action report.

### Mode classification

The tool auto-detects whether the host is server-mode or domain-mode
by inspecting its registered items. A pure helper
`_classify_host_mode(host_record, items)` returns:

- `server` when items include `net.if.in[*]` traffic keys or
  `agent.ping` / `agent.version`.
- `domain` otherwise (typical for HTTPS-check-only hosts whose IP
  field is empty and items are web-scenario checks).

This decides which downstream queries fire. Domain-mode skips
traffic and IP-rotation queries.

### Verdict synthesis

Pure helper `_classify_verdict(...)` takes already-aggregated facts
(agent state, traffic vs baseline, open problems, HTTPS state) and
emits one verdict label plus a one-line action recommendation.

Traffic-collapse threshold: baseline ≥ 5 Mbps AND recent < 10% of
baseline. The 5 Mbps floor stops idle micro-traffic hosts from
producing false signal (e.g. 0.5 → 0.05 Mbps drop is 90% but not
signal-bearing). Mirrors the floor used by `detect_traffic_drops`
and `detect_disruption_wave`.

### Chain composed server-side

For `server` mode:

1. `host.get` with hostid + name + status + interfaces + groups
2. `item.get` for the host (used both for mode classification and
   for finding the `agent.ping` and `net.if.in[*]` items)
3. `problem.get` for active problems within `problem_hours`
4. `trend.get` ×2 (baseline + recent windows) for traffic
5. Read `agent.ping` lastvalue + lastclock
6. `auditlog.get` for IP rotations in `rotation_days`

For `domain` mode the trend / IP-rotation steps are skipped; the
HTTPS-check item is read directly.

### Output

Markdown report with `Identity` / `Agent` / `Traffic` /
`IP rotation history` / `Active problems` sections, prefixed by
the verdict line and recommended action. Domain-mode skips the
agent / traffic / rotation sections.

## Test approach

12 new pure-helper tests in `test_analytics.py` covering
`_classify_host_mode` (server vs domain detection paths) and
`_classify_verdict` (every verdict label, plus the 5 Mbps baseline
floor edge case). The async tool wrapper is configuration-level
(calls existing Zabbix APIs) — covered by the registration /
smoke test.

## Consequences

- 405 tests pass (393 pre-change + 12 new helper tests).
- Tool count 156 → 157.
- Lands in `core` tier — replaces a multi-tool workflow that every
  operator runs constantly.
- `WRITE_TOOLS` unchanged.
- No new env vars.

## Not included

- A `diagnose_cluster(name)` companion for multi-host outages. The
  existing `get_outage_clusters` + `get_host_floods` already cover
  that surface; the per-host chain was the bigger pain.
- An auto-acknowledge action on adverse verdicts. Diagnostic tools
  should not mutate state without an explicit call; the
  recommended-action text points at `get_external_ip_history` /
  hosting console / etc. — operator initiates remediation.
- Streaming output. The full chain takes ~2–5 seconds in practice
  (5 sequential API calls); buffered response is fine.
- A "compare with peer" section. Useful but a separate concern
  best handled by `get_disruption_blast_radius` when called
  explicitly.
