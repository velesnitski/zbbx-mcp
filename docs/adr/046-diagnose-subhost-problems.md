# ADR 046: Diagnosis queries problems across the whole canonical group

**Status:** Accepted
**Date:** 2026-06-04

## Problem

`diagnose_host` and `bulk_diagnose` run `_collect_diagnosis_inner`,
which queried `problem.get` for the representative (parent) hostid
only. ADR 039 folded the *input* host list so a multi-VIP physical
machine produces one diagnostic row — but the row's facts came from the
parent record alone.

On a multi-VIP box, problems fire per-VIP (each sub-host has its own
service checks). A problem on a sub-host VIP was therefore invisible to
the verdict: the box could show `healthy` while one of its VIPs had an
active problem. That is a **false-negative** — the dangerous direction
for a diagnostic, the opposite of the false-positive work in the rest
of this release line.

ADR 039 flagged exactly this as a "not included" follow-up: "a
sub-host-specific problem wouldn't show."

## Decision

Query problems across every hostid in the canonical group.

- `_collect_diagnosis_inner(..., group_hostids=None)` — when provided,
  `problem.get` uses those hostids; otherwise it falls back to the rep
  alone, so single-host callers are unchanged.
- `_dedupe_records_by_canonical` attaches `_group_hostids` (every VIP's
  hostid) to each representative record, threaded into
  `_collect_diagnosis_inner` by `_run_bulk_diagnosis`.
- `diagnose_host` (single-host path) fetches the canonical group — a
  `host.get` searching the canonical name, filtered to records whose
  canonical name matches — and passes their hostids.

The verdict's open-problem count now reflects the whole physical
machine. The other facts (agent ping, traffic, IP rotations) still come
from the representative; broadening *those* to the group is a separate
concern (the traffic-location split noted in ADR 036/039) and is not in
scope here — this ADR is specifically the problem-visibility gap.

## Test approach

2 tests added to `test_analytics.py::TestBulkDiagnosePreFold`:

- a parent + 3 VIPs yields a rep whose `_group_hostids` is all four
  hostids;
- a standalone host's `_group_hostids` is just itself.

The existing pre-fold tests confirm the dedup behaviour is otherwise
unchanged. The `problem.get` fan-out is configuration-level over the
tested dedup helper; the async wrappers are covered by the
registration / smoke tests.

## Consequences

- Tool count unchanged (161).
- Test count 535 → 536.
- `WRITE_TOOLS` unchanged. No new env vars.
- **Behaviour change**: a multi-VIP box with a problem on any VIP now
  surfaces that problem in its diagnosis and can move the verdict from
  `healthy` to `degraded`. Single-host diagnoses are unchanged.
- One extra `host.get` per `diagnose_host` call (to resolve the group);
  `bulk_diagnose` reuses the dedup it already did, so no extra call
  there.

## Not included

- **Broadening agent / traffic facts to the group.** The agent ping and
  traffic still read the representative's items. On boxes where traffic
  lives on the sub-host VIPs (the split noted in ADR 036/039), the
  rep-only traffic read can still be incomplete. That is a deeper
  data-location refactor of `_collect_diagnosis_inner`; deferred.
- **Per-VIP attribution in the rendered problem list.** Problems are
  merged into one list for the box; the output does not label which VIP
  each came from. The count and verdict are correct; per-VIP labelling
  is a presentation nicety for later.
- **Maintenance-suppress filtering in the diagnosis** (ADR 044). The
  diagnosis problem query does not yet drop suppressed problems; a box
  in maintenance could read `degraded`. Tracked as a follow-up to ADR
  044's "not included" note.
