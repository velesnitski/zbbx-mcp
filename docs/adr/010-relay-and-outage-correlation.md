# ADR 010: Relay-idle and outage-cluster correlation tools

**Status:** Accepted
**Date:** 2026-04-30

## Problem

The existing per-host availability checks (`get_agent_unreachable`,
`get_active_problems`, `get_server_dashboard`) cover two failure modes well
— a host being unreachable, and a host having a high-severity trigger in
PROBLEM state. They miss two classes of failure that surfaced in recent
diagnostic sessions:

1. **The relay is up but routing nothing.** Management traffic on the
   primary NIC is healthy (host is reachable, agent is fine, monitoring
   is green), yet every tunnel-class interface reads zero bytes/sec.
   Clients see a dead service, monitoring sees a healthy one.

2. **A wave of independent alerts is really one event.** Six hosts in
   the same /24 hit `agent-unavailable + service-down + auth-down`
   inside a 21-minute window. `get_active_problems` already collapses
   identical-name alerts across hosts, but it does not look at the
   network or hostgroup adjacency — six different alert names on six
   adjacent hosts still print as eighteen separate rows.

## Decision

Two new tools in a new `tools/correlation.py` module:

### `get_idle_relays(min_mgmt_kbps, max_results, country, instance)`

For every enabled host, fetch all `net.if.in[*]` items in one batch and
bucket them:

- **Physical** = exact match against the curated `TRAFFIC_IN_KEYS` list
  (`eth0`, `eno1`, `enp*`, `ens*`, `bond0`, `ppp*`).
- **Tunnel** = anything else under `net.if.in[...]` that is not `lo`,
  `docker*`, or `br-*`.

A host is flagged idle when the aggregate physical-NIC throughput is at
or above `min_mgmt_kbps` *and* every tunnel interface reports exactly
zero bytes/sec *and* at least one tunnel interface is configured.

The split deliberately uses **exclusion-based** tunnel detection rather
than a known-prefix list — adding a new transport type does not require
a code change here, and the tool name does not advertise which tunnel
families exist on the fleet.

### `get_outage_clusters(window_min, min_hosts, group_by, min_severity, max_clusters, instance)`

Pull recent problems at or above `min_severity`, attach the host's IP
and groups, and project each problem onto a group key — either the
host's `/24` subnet (default) or its first hostgroup.

Within each key the records are sorted by clock and grouped by a
greedy run-length scan: a run extends as long as the first→last span
stays inside `window_min`. A run becomes a reported cluster only when
it covers at least `min_hosts` distinct hostids. Output collapses an N
- alert × M - host wave to one block per cluster with the time range,
  affected hostnames, severity ceiling, and a sample of problem names.

Greedy maximal-run grouping was chosen over fixed-width bins because
fixed bins fragment a wave that straddles a bin boundary; greedy runs
correctly fold "21-minute wave" into a single cluster regardless of
where the wall-clock minute fell.

## Test approach

The two tools each lift their decision logic into pure module-level
helpers — `_split_iface_metrics`, `_find_idle_relays`,
`_cluster_problems`, `_subnet24` — that take dicts/lists and return
dicts/lists. The async `@mcp.tool()` wrappers do nothing but Zabbix I/O
and rendering. Sixteen tests in `test_analytics.py` exercise the helpers
directly without any HTTP mocking; integration through the MCP
registration layer is covered by `test_registration.py` and
`test_server.py`.

## Consequences

- 246 tests pass (230 pre-change + 16 new for the helpers).
- Tool count goes from 143 to 145; both the registration set and the
  `tools/list` JSON-RPC count assertion are bumped.
- `WRITE_TOOLS` is unchanged — neither tool mutates Zabbix state.
- The `get_active_problems` cluster-dedup logic is unchanged. That tool
  collapses identical alert names across many hosts; this one collapses
  many alert names across many adjacent hosts. They are complementary,
  not redundant.

## Not included

- A maintenance-window suppression layer for `get_outage_clusters`. A
  planned reboot inside a maintenance window will currently surface as
  a cluster — Zabbix already suppresses individual triggers there, so
  this is rare in practice but worth revisiting if false positives
  appear.
- Per-tunnel last-clock checks in `get_idle_relays`. A tunnel reading
  exactly zero is treated as silent regardless of whether the item
  itself stopped polling. Stale-item detection is the job of
  `get_stale_items` (ADR-pending follow-up to #108) and a join between
  the two would belong in a higher-level "carrier health" report
  rather than inside either of these tools.
