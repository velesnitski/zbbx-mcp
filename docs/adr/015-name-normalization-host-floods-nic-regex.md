# ADR 015: Trigger-name normalisation, host-flood detector, NIC regex fallback

**Status:** Accepted
**Date:** 2026-05-04

## Problem

A real ops session today turned up three independent gaps in the
existing tools:

1. **Per-name dedup is defeated by hostnames inside trigger names.**
   Today's report had 18 rows for 25 active events because trigger
   names of the form `<service>: on <host> error` (and the sub-host
   variant `<service>: on <parent> <child> error`) never collapse —
   each host produces a distinct name. The `get_active_problems`
   "5+ hosts × same name → one row" rule fires on identical names
   only.
2. **Single-host outages slip through both dedup tools.** A single
   host with N triggers (agent down, service down, ping fail, etc.)
   reports as N separate rows in `get_active_problems` and never
   makes a cluster in `get_outage_clusters` (which requires ≥3 hosts
   in a /24). One whole host going down is the most common outage
   shape and the report had no row for it.
3. **`_split_iface_metrics` mis-buckets unused physical NICs as
   tunnels.** The exclusion-based detection treats anything not in
   the curated `TRAFFIC_IN_KEYS` list as a tunnel. Hosts with
   secondary or USB ethernet adapters (`eno3`, `enp130s0f0`,
   `enxMACADDR`) showed those NIC names in the "Sample" column of
   `get_idle_relays` output. The verdict was right (real tunnels
   were genuinely zero) but the label was misleading.

The first two are already fixed in the zabbix-reports port; this
commit keeps MCP behaviour in sync.

## Decision

### #127 — `normalize_problem_name(name, hostname)`

New helper in `formatters.py`. Strips ` on <hostname>` from a problem
name, tries the sub-host form (`parent child`) before the bare parent,
collapses internal whitespace, and returns the cleaned name. Match is
case-insensitive on the keyword `on` and uses `\b` word boundaries
around the hostname so substring collisions cannot misfire.

Applied at three sites that actually do per-name grouping:

| Site | What it does | Before / after |
|------|--------------|---------------|
| `get_active_problems` (`tools/health.py`) | Storm detection + correlated-problem cluster grouping | Counter keys + grouping tuples now use the normalised name; hostname goes in the affected-hosts column unchanged |
| `get_correlated_events` (`tools/events.py`) | Time-windowed clustering of repeating triggers | `by_trigger` keys now use the normalised name |
| `get_outage_clusters` (`tools/correlation.py`) | Per-cluster sample-trigger dedup | Each record's `name` field is normalised before clustering |

The original task ticket listed three more tools (`get_problems`,
`get_alerts`, `get_alert_summary`, `get_event_frequency`) but on
inspection they don't dedup by name — they either list events
verbatim or key by trigger ID, which is already per-trigger. Skipping
those is intentional; it would change rendering without changing
correctness.

### #128 — `get_host_floods(min_problems=5, min_severity=2, ...)`

New tool in a new module `tools/floods.py`. Pulls active problems at or
above `min_severity`, groups by parent host (via the existing
`build_parent_map`), and filters where the per-parent count meets
`min_problems`. Output: host, problem count, max severity, earliest
clock, sample triggers (set-deduplicated, capped at 5), and a
`+N sub-hosts` annotation when children fold into the parent.

Sub-host merge prevents a parent + child outage from appearing as two
separate rows. The same convention is used in `data.build_parent_map`
for the existing trend tools.

The decision was inline-badge vs. standalone tool. The standalone
tool wins because it composes cleanly: the badge variant would need
a new code path inside every problem-rendering tool; the standalone
form is cheap to call after `get_active_problems` and produces an
operator-readable list.

Pure helper `_group_host_floods(records, parent_map, min_problems)`
holds the grouping/filter/sort logic and is tested across the
threshold, sub-host merge, earliest-clock pick, sample-trigger
dedup-and-cap, and count-vs-severity sort cases.

### #129 — physical-NIC regex fallback

`_split_iface_metrics` in `tools/correlation.py` now treats interface
names matching `^(?:eno|enp|enx|eth|ens|bond|ppp|wlan)\d` as physical
even if the full `net.if.in[<iface>]` key isn't in the curated
`TRAFFIC_IN_KEYS` list. Catches:

- Unused secondary NICs (`eno3`, `enp130s0f0`)
- USB ethernet (`enxMACADDR`)
- Wireless (`wlan0`) — rare on relays but harmless to include

The regex is a fallback, not a replacement: explicit-list matches still
win, so behaviour for already-known NICs is unchanged. No verdict
changes for `get_idle_relays` — the tunnels that were zero are still
zero. The "Sample" column is just cleaner now.

Same patch also softens the NAT-mode caveat in the `get_idle_relays`
docstring. The original wording (`will appear as false positives`)
implied a high rate. Today's smoke test verified all sampled hits
were true positives — language now reads `may appear` and notes the
observed rate is low, with the cross-check guidance preserved.

## Test approach

17 new tests in `test_analytics.py`:

- 7 for `normalize_problem_name` (basic strip, sub-host preference,
  whitespace collapse, no-match case, missing-hostname case,
  identical-after-normalisation invariant, case-insensitive `on`).
- 6 for `_group_host_floods` (threshold filter, threshold met,
  sub-host merge, earliest-clock pick, sample dedup-and-cap, sort
  order).
- 4 for the NIC regex (unused-NIC bucketed physical, USB enx prefix,
  explicit-keys still win, unknown prefix still tunnel).

The three call-site changes (#127 application) are configuration-
level: new keys feed existing grouping code that's already tested.
The registration / smoke tests verify the new tool loads.

## Consequences

- 324 tests pass (307 pre-change + 17 new).
- Tool count goes from 153 to 154 (`get_host_floods` is the only new
  tool; the other two changes are internal).
- `WRITE_TOOLS` is unchanged — `get_host_floods` is read-only.
- `get_active_problems`, `get_correlated_events`, and
  `get_outage_clusters` now collapse host-embedded triggers into one
  row in the report; the affected hostnames remain visible in the
  hosts column. Operators reading older outputs may notice fewer
  rows for the same data — that is the intended fix for the 18-rows-
  for-25-events behaviour.
- `get_idle_relays` "Sample" column no longer lists physical NIC
  names as tunnels.
- No env-var changes.

## Not included

- Normalisation in `get_problems`, `get_alerts`, `get_alert_summary`,
  `get_event_frequency`. None of these dedup by name: the first three
  list events verbatim, the fourth keys by trigger ID. Adding the
  normalisation there would change cosmetics without changing
  behaviour and is deferred unless an operator requests it.
- A NAT-mode classifier in `get_idle_relays` (per #124's deferral).
  The smoke-test sample was 100% true positives, so the docstring
  caveat is the right level of warning right now.
- `get_host_floods` does not currently distinguish a flood whose
  triggers are dependents of one root cause from a flood of
  unrelated problems. A trigger-dependency walk would be an
  independent enhancement.
