# ADR 013: Disruption-detection composites + sortfield/NAT fixes

**Status:** Accepted
**Date:** 2026-04-30

## Problem

Three independent traffic-side signals (#116, #117, #118) and three
composites that lean on the building blocks shipped in ADR 012
(#120, #121, #122) close out the disruption-detection task family.
Two same-day bugs surfaced from a smoke test and ride along in this
commit because the first one made #119 unusable:

- **#123** — `get_outage_clusters` calls `problem.get` with
  `sortfield="clock"`. Zabbix 6.4 rejects this with
  `"Sorting by field 'clock' not allowed"`. The tool returned an
  error on every default-arg invocation and on every `group_by`
  variant. Same root cause as a `fetch_active_problems` fix already
  applied in zabbix-reports.
- **#124** — `get_idle_relays` flags NAT-mode relays (where the
  primary NIC routes traffic and tunnel interfaces don't exist by
  design) as idle. Reported in a smoke test (115 hits, top one was
  847 Mbps mgmt with zero on all tunnels). Option 3 from the ticket
  — docstring caveat — is the agreed fix; classifier-based filtering
  was deferred until ops reports the noise level.

## Decision

### #116 — `detect_service_port_split`

For each enabled host, compare service-port traffic against the sum of
management-NIC interfaces over a window/recent split. Labels:

| Label | Condition |
|-------|-----------|
| `split` | service drop ≥ 50%, mgmt drop < 10% |
| `full-outage` | both dropped (different concern; reported by other tools) |
| `ok` | neither dropped |
| `n/a` | insufficient data |

Service-port item key comes from new env var `ZABBIX_SERVICE_BPS_KEY`;
the tool returns a configuration message when the env is empty.

Pure helper `_classify_service_split` covers the 4-state matrix and
takes `service_drop_pct` / `mgmt_drop_pct` as arguments.

### #117 — `detect_regional_traffic_loss`

Region → item-key map comes from `ZABBIX_REGIONAL_TRAFFIC_KEYS` (JSON
object). For each configured region, sum trend records across all
matching items in the window vs the recent slice. A region is flagged
`collapsed` when its drop ≥ `drop_threshold` *and* at least one peer
region stayed within `±flat_threshold` of its baseline. When no peer is
flat, the label collapses to `solo-drop` — informative but
qualitatively different from a regional asymmetry.

Pure helper `_classify_regional_loss` exposes both thresholds and
returns the suspect-region list with deltas.

### #118 — `detect_disruption_wave`

For every enabled host, compare hourly inbound trends across a recent
slice (default 1h) against the prior baseline (default 6h - 1h = 5h).
Hosts that dropped at least `drop_pct` are clustered by 1h time window;
a wave fires when ≥`min_hosts` distinct hostids spanning ≥`min_subnets`
distinct /24s land in the same window. Output sorts by host count and
average drop.

Pure helper `_compute_waves(drops, window_sec, min_hosts, min_subnets)`
holds the greedy maximal-run grouping (same shape as
`_cluster_problems` in ADR 010, but on a different signal).

### #120 — `get_at_risk_hosts`

Composite ranking score combining three signals:

```
peers_score = log1p(peer_rotations_in_/24_last_window_days)
drift_score = lookup on detect_loss_drift label
              (loss-and-rtt 2.0, new-loss 1.5, loss-up 1.0, rtt-up 0.5, ok/n_a 0)
age_score   = log1p(min(days_since_last_rotation, 90))   # capped
total       = 1.5 * peers_score + 2.0 * drift_score + 0.5 * age_score
```

`days_since_rotation = None` (never rotated in window) is treated as
the 90-day cap, not zero — long-stable IPs are not artificially safe.
Drift weight dominates at parity because rising loss/RTT is the most
predictive of imminent disruption.

Pure helper `_compute_risk_score(peer_rotations_7d, drift_label,
days_since_rotation)` returns `(total, components_dict)` and is unit-
tested across the four input axes.

### #121 — `get_recovery_score` + table footer

Two changes in `tools/ip_history.py`:

1. New `_aggregate_recovery_scores(rotations)` helper: counts by
   outcome, denominator excludes `n/a`, returns `rate_pct = None` when
   no rotation had a determinable outcome.
2. New tool `get_recovery_score` walks the audit log fleet-wide and
   emits a single KPI block (total, recovered, partial, still-down,
   n/a, rate). The existing `get_external_ip_history` per-host table
   now also uses this aggregate for its footer line.

The CEO-report integration mentioned in the original task is deferred
— the standalone tool is the building block; `generate_ceo_report` can
embed it as a one-line append later without touching the helper.

### #122 — `get_disruption_blast_radius`

Given a host that recently dropped, build the (product, tier, country)
cohort with self excluded, fetch connection-count items
(`KEY_CONNECTIONS`) for every peer, and compare a `window_min`-minute
average pre/post the drop event.

Per-peer label is purely on the connection-count delta:

| Label | Condition |
|-------|-----------|
| `absorbing` | post − pre ≥ +10% (peer took the candidate's load) |
| `stable` | within ±10% |
| `draining` | post − pre ≤ −10% (the disruption spread to this peer) |
| `n/a` | pre missing or zero, or post missing |

Pure helper `_compute_blast_radius(pre, post)` returns
`(label, delta_pct)` and is unit-tested across all four buckets.

### #123 — sortfield="clock" → "eventid" + Python sort

`tools/correlation.py` `problem.get` call now uses
`sortfield="eventid"`. The tool re-sorts by clock in Python after the
fetch. `eventid` is monotone with creation time, so the only thing
the API-side sort affected was the `LIMIT` cutoff — sorting by eventid
is functionally equivalent there.

### #124 — NAT-mode caveat

`get_idle_relays` docstring gains a "Caveat — NAT-mode relays"
paragraph explaining that hosts that route through the primary NIC by
design will appear as false positives. The classifier-based filter
is left for a follow-up if ops reports the noise level.

## Test approach

26 new tests in `test_analytics.py`:

- 5 for `_classify_service_split` (split/full-outage/ok/n/a/threshold tuning)
- 4 for `_classify_regional_loss` (collapsed/solo-drop/below-threshold/missing-data)
- 4 for `_compute_waves` (fire/subnet-collapse-blocks/window-boundary/severity)
- 5 for `_compute_risk_score` (zero/peer-monotone/drift-monotone/age-cap/None-as-cap)
- 4 for `_compute_blast_radius` (absorbing/draining/stable/n/a)
- 4 for `_aggregate_recovery_scores` (basic/all-na/unknown-label/empty)

Bug fixes #123 and #124 ride along without dedicated tests — both
require live Zabbix data (the API rejection #123 patches and the NAT
detection #124 punts on). Existing registration / smoke tests verify
the tool still loads and is invokable.

## Consequences

- 307 tests pass (281 pre-change + 26 new).
- Tool count goes from 147 to 153.
  - New: `detect_service_port_split`, `detect_regional_traffic_loss`,
    `detect_disruption_wave`, `get_at_risk_hosts`,
    `get_disruption_blast_radius`, `get_recovery_score`.
- `WRITE_TOOLS` is unchanged — every new tool is read-only.
- Three new env vars: `ZABBIX_SERVICE_BPS_KEY`,
  `ZABBIX_REGIONAL_TRAFFIC_KEYS` (JSON), and the existing
  `ZABBIX_CONNECTIONS_KEY` is now also consumed by
  `get_disruption_blast_radius`. Tools that need an unset env return a
  configuration message instead of running — no breakage on existing
  deployments.
- `get_outage_clusters` becomes usable on Zabbix 6.4 again.
- `get_idle_relays` output is unchanged but the docstring now sets
  expectations about NAT-mode false positives.

## Not included

- Embedding the recovery KPI inside `generate_ceo_report`. The
  standalone tool is the building block; the report integration is a
  one-line append once the report layout team confirms placement.
- A NAT-mode classifier inside `get_idle_relays`. Per the #124
  ticket, option 3 ships now and option 1/2 are revisited if ops
  finds the noise level too high.
- Maintenance-window awareness in any of the new tools. Same trade-
  off as in ADR 010: rare in practice, deferred until it bites.
