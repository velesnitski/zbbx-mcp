# ADR 089: A shared `exclude_test` seam for fleet verdicts

**Status:** Accepted
**Date:** 2026-07-17

## Problem

ADR 080 wired test-host exclusion into six tools, "with a demonstrated
distortion", and deferred the rest. But most fleet-verdict tools still counted
test boxes: of ~30 modules that fetch host sets, only five excluded them. The
tell was `detect_traffic_anomalies` and `get_traffic_report` sitting in the
*same file* as the already-fixed `detect_traffic_drops`, still unfiltered.

The naive fix — thread `include_test` + `partition_test_hosts` + a note into
~20 more tools by hand — is exactly the error-prone, signature-bloating change
ADR 080 warned about (and I twice put real host names into test fixtures doing
this kind of edit). Worse, a *blanket* sweep would be **wrong**: a test box
should still appear in `search_hosts`, `get_server_map`, macro-setting, and
other host-listing/CRUD tools — there, hiding it is the bug, not the fix. Only
tools that render a fleet **verdict** (a count, a rank, an anomaly, a report)
are distorted.

## Decision

**Add the exclusion to the shared seam** rather than to each caller.
`fetch_enabled_hosts` gains `exclude_test: bool = False`: when set it forces
`groups=True` (a test box is routinely in a *production* group, so the group
name is half the `is_test_host` signal), filters via `is_test_host`, is
cache-keyed on the flag, and logs the excluded count. Every fleet-report tool
that already routes through the seam adopts it with a one-word kwarg.

Wired now:
- **`detect_traffic_anomalies`** — the flagship sibling — gets the full
  `include_test` + `partition_test_hosts` + named-note treatment, identical to
  `detect_traffic_drops`, so its output *names* what it dropped.
- **`generate_ceo_report`, `generate_service_brief`, `get_expansion_report`**
  — whole-fleet aggregates — adopt `fetch_enabled_hosts(exclude_test=True)`.

## Test approach

`tests/test_test_hosts.py` (+4): the seam filters a test box and forces
`selectGroups`, and leaves everything when the flag is off; `RecordingClient`
gains a no-op cache so seam helpers are testable; `detect_traffic_anomalies`
excludes-and-names a test box and keeps them under `include_test=True`.
777 → 783.

## Consequences

- The mechanism now exists in one place; adopting it elsewhere is a one-line
  kwarg, not a per-tool re-implementation.
- Together with ADR 080's six tools, the highest-distortion verdicts
  (uptime/SLA, health matrix, at-risk, bulk diagnosis, traffic drops/anomalies,
  item search, the CEO/service/expansion reports) now exclude test boxes.
  Tool count unchanged (163).

## Not included (deliberately deferred, not overlooked)

- **Host-listing / CRUD / mapping tools** (`search_hosts`, `get_server_map`,
  `set_bulk_macro`, provider/product summaries): these should *show* test
  hosts, so they are intentionally left alone.
- **The direct-`host.get` verdict tools** that don't route through the seam
  (`inventory_load` "bad server" lists, `get_host_floods`, the `disruption`
  detectors, `detect_regional_anomalies`): each needs its own `partition` +
  note and, ideally, live verification. They are a distinct batch, best done
  incrementally rather than in one blind sweep — the seam and the pattern are
  now in place for them.
