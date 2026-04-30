# ADR 012: Disruption-detection building blocks

**Status:** Accepted
**Date:** 2026-04-30

## Problem

The existing health tools answer "is this host having a problem right
now?" Three gaps remain when the question is "is service degrading,
already disrupted, or rotating to recover?":

1. **No history of external-IP rotations.** When a host's interface IP
   changes, there is no easy way to ask "did the rotation help?"
   The audit log has the events, but nothing correlates them with
   traffic deltas.
2. **No early signal before traffic dies.** A rising ping-loss or RTT
   trend is the canonical leading indicator of a disruption that has
   not yet hit traffic. None of the existing tools surface it.
3. **Outage clustering is fixed at /24 or hostgroup.** When a wave hits
   hosts spread across multiple /24s but inside one /16 or one
   provider AS, the existing `get_outage_clusters` returns nothing
   useful even though the wave is real.

These three gaps are the building blocks for a follow-on tier of
composite tools (#120 at-risk hosts, #121 recovery score in the CEO
report, #122 blast radius).

## Decision

### #114 — `get_external_ip_history` (`tools/ip_history.py`)

For each enabled host (filtered by `host=` exact match or `country=`),
walk `auditlog.get` for `resourcetype=2 (Host)` + `action=1 (Update)`
in the requested window, parse the `details` blob for IP-field
changes, and for each rotation compute traffic averages across a 24h
pre-window and a 24h post-window. Label by ratio:

| Label | Condition |
|-------|-----------|
| `recovered` | `post / baseline ≥ 0.7` |
| `partial` | `0.3 ≤ ratio < 0.7` |
| `still-down` | `ratio < 0.3` |
| `n/a` | Either window is missing trend data or baseline is zero |

The Zabbix audit-log `details` field has shifted shape across 6.x
versions (sometimes a list of update tuples, sometimes an object
keyed by field path). The parser handles both shapes and ignores
anything that is not an IP-field update with two distinct values.

### #115 — `detect_loss_drift` (`tools/loss_drift.py`)

Pull trend records over a configurable window (default 14d) for each
host's ping-loss and RTT items. The last 2 days are the recent
window; everything older is the baseline. Two thresholds, both
configurable:

- `loss_step` (default 5.0) — absolute percentage-points increase in
  loss between baseline and recent window flags `loss-up`. If the
  baseline was below 1% loss, it upgrades to `new-loss` (the most
  actionable label).
- `rtt_step_pct` (default 50.0) — percent increase in RTT flags
  `rtt-up`.

When both fire on a host, label collapses to `loss-and-rtt`. Output
sorts `new-loss > loss-and-rtt > loss-up > rtt-up`, so the operator
sees the signals most likely to predict a disruption first.

**Key configuration is environment-only.** Two new env vars,
`ZABBIX_PING_LOSS_KEY` and `ZABBIX_PING_RTT_KEY`, drive the
`item.get` filter; nothing in the module hardcodes a key. If neither
env var is set the tool returns a configuration message instead of
running.

### #119 — `get_outage_clusters` v2 (multi-level grouping)

`tools/correlation.py` gains a generic `_group_key(level, ip=, ...)`
helper that returns a cluster grouping key for any of:

| Level | Key |
|-------|-----|
| `subnet24` | host IP /24 (renamed from old `"subnet"`, alias kept) |
| `subnet16` | host IP /16 |
| `provider` | `detect_provider(ip)` from `classify.PROVIDER_CIDRS` |
| `hostgroup` | first hostgroup name |
| `auto` | walk `subnet24 → subnet16 → provider`, stop at the first level that produces clusters |

Provider-level grouping skips `Other` / `Unknown` results so unmapped
IPs do not get lumped together as a fake cluster.

The default changes from `"subnet"` to `"auto"` — most-specific level
that has hits wins. The string `"subnet"` is accepted as a backwards-
compatible alias for `"subnet24"`.

The output header now includes the effective level when auto picked
something other than the narrowest:

```
**5 outage clusters** (window 30m, ≥3 hosts, by subnet16 (auto))
```

## Test approach

22 new tests in `test_analytics.py` covering every helper:

- **5** for `_parse_ip_changes` (list shape, dict shape, no-op
  same-value, non-IP path, garbage input).
- **2** for `_score_recovery` (boundary values, n/a cases).
- **3** for `_split_baseline_recent` (partition by clock, missing
  sides, garbage values).
- **6** for `_compute_loss_drift` (new-loss priority, loss-up alone,
  rtt-up alone, combo, ok, n/a, configurable thresholds).
- **5** for `_group_key` and `_AUTO_LEVELS` (subnet24/16, provider
  with Unknown skipped, hostgroup, unknown level, auto-order
  invariant).

Sixteen of the twenty-two are new pure functions. The async
`@mcp.tool()` wrappers do only Zabbix I/O and rendering and are
covered by the registration / server smoke tests.

## Consequences

- 281 tests pass (259 pre-change + 22 new).
- Tool count goes from 145 to 147 (`get_external_ip_history`,
  `detect_loss_drift` are added; `get_outage_clusters` is extended).
- `WRITE_TOOLS` is unchanged — all three tools are read-only.
- `data.py` exposes two new env-driven keys (`KEY_PING_LOSS`,
  `KEY_PING_RTT`). Existing deployments that do not set them simply
  see `detect_loss_drift` return a configuration message instead of
  running — no breakage.
- `get_outage_clusters` default behaviour changes from `subnet` to
  `auto`. The old string is kept as an alias and downgrades to
  `subnet24`, so external callers who passed the old value keep
  working.

## Not included

- The composite tools that build on these blocks (#120 at-risk-hosts,
  #121 recovery-score KPI inside the CEO report, #122 blast radius).
  Each of those takes one or two of these helpers and adds its own
  scoring/aggregation layer; they ship in a follow-up commit.
- Provider-level grouping in `get_outage_clusters` does not use ASN
  numbers directly. `detect_provider()` returns the provider name
  the user already configures via the CIDR table; ASN lookups would
  require a separate data source.
- The recovery score in `get_external_ip_history` is computed
  inline. A follow-up could surface it as a KPI in
  `generate_ceo_report`; deferred to #121.
