# ADR 049: Diagnosis reads agent / traffic across the whole canonical group

**Status:** Accepted
**Date:** 2026-06-04

## Problem

ADR 046 broadened the diagnosis problem query to the whole canonical
group, but the *other* server-mode facts — agent ping and traffic —
were still read from the representative (parent) record's items alone.

On a multi-VIP physical machine, traffic is instrumented on the
sub-host VIP interfaces, not the parent. So `_collect_diagnosis_inner`
found no `net.if.in[*]` items on the rep and reported "No traffic items
/ trend data available" — and the verdict could not distinguish
`traffic_lost` from healthy on exactly the boxes most likely to be
multi-VIP (the prem / netproxy clusters).

This was observed directly: `diagnose_host` on a parent whose VIPs
carried the load came back with no traffic data, leaving the block
question unanswerable for that box. It is the recurring "traffic lives
on the VIPs" thread flagged as deferred in ADR 036, 039, and 046.

## Decision

Read items across every hostid in the canonical group.

- **`diagnose_host`** now fetches `item.get` for `group_hostids` (the
  whole box) instead of the rep alone.
- **`_run_bulk_diagnosis`** fetches items for the union of every box's
  VIP hostids in its existing single batch, then maps each VIP's items
  back to its canonical group so each rep is handed the whole box's
  items — no extra round-trip per host.
- **Traffic** already sums all `TRAFFIC_IN_KEYS` item trends; with
  group-wide items it now sums across the box's VIP interfaces — the
  box's real throughput.
- **Agent ping** picks the freshest `agent.ping` across the group via
  the new pure helper `_freshest_agent_ping(items)`. A stale sub-host
  record must not override the parent's live agent, so the selection is
  by max `lastclock`, not first-found.

## Test approach

4 unit tests in `test_analytics.py::TestFreshestAgentPing`:

- no ping item → None;
- the freshest reading wins across VIP records (a stale down-ping does
  not override a live up-ping);
- a single ping is returned;
- a missing `lastclock` is treated as zero (oldest).

The item-gathering broadening is configuration-level over the tested
helper and the existing trend-sum logic; the async wrappers are covered
by the registration / smoke tests. ADR 046's `_group_hostids` tests
already confirm the group hostids are correctly assembled.

## Consequences

- Tool count unchanged (161).
- Test count 548 → 552.
- `WRITE_TOOLS` unchanged. No new env vars.
- **Behaviour change**: a multi-VIP box now gets a real traffic verdict
  (summed across its VIPs) instead of "No traffic data," so it can be
  classified `traffic_lost` / `down` like a single-host box. Single-host
  diagnoses are unchanged.
- `diagnose_host` reads items for the group (a few extra item rows);
  `bulk_diagnose` reuses its single batch, just widened to all VIP
  hostids.

## Not included

- **Per-VIP traffic breakdown in the rendered report.** Traffic is
  summed to one box figure; the report doesn't show each VIP's share.
  The verdict is correct; per-VIP detail is a presentation nicety.
- **Maintenance-suppress (ADR 044) in the diagnosis problem query.**
  Still not applied — a box in maintenance could read `degraded`.
  Tracked separately (carried over from ADR 046's "not included").
- **Applying the freshest-reading rule to other per-host item reads**
  (e.g. the domain-mode HTTPS item). Domain hosts are not multi-VIP in
  practice, so the first-match there is fine; revisit only if a
  multi-record domain host appears.
