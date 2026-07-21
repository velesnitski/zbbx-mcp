# ADR 090: `detect_check_flaps` — minute-level flap matrix with a noise/real split

**Status:** Accepted
**Date:** 2026-07-21

## Problem

A minute-level flap audit across a small host sample (written up in the
private reports repo, which ships the cheap hourly proxy) proved three facts
the trigger layer cannot see:

1. **Same-minute dips on geographically distant hosts are prober/egress
   noise.** The monitoring path blinked, not the fleet. Any per-host score
   that does not subtract these first punishes healthy hosts.
2. **A TEST-class check flaps several times more than the production check on
   the same host.** Script noise; weight ~0, but worth tracking so it can be
   cleaned up.
3. **Chronically degraded hosts fire zero triggers.** A production check
   failing in *most* hours, one-two minutes at a time, never trips a
   consecutive-fail trigger — the truly sick end of the fleet is invisible to
   alerting. Trend-grain uptime (hourly averages) also smooths these dips
   away, which is why the existing uptime tools can't see them either.

## Decision

New read-only tool **`detect_check_flaps`** (`tools/check_flaps.py`,
tool count 163 → 164): pulls raw **minute** history for the configured
service-check items over a bounded scope (`hosts`/`group`/`country`; hard caps
`max_hosts` ≤ 12, window ≤ 7d — raw history is heavy and short-lived) and
classifies every flap-minute in strict priority order via the pure
`classify_flap_minutes`:

1. **fleet noise** — prod-check dips the same minute on hosts spanning ≥2
   distinct countries (≥3 hosts as the unknown-country fallback) → discarded
   for every host. TEST-check dips never create fleet noise.
2. **host event** — ≥2 distinct prod checks on one host, same minute → real.
3. **test noise** — TEST-class check (token-bounded, bracket-aware name
   match; `latest`/`attestation` never match) dipping alone → tracked, weight 0.
4. **prod flap** — residual single-service dips; with (2) forms the honest
   per-host `rate_per_day`.

Output: ranked matrix (rate/day, host events, prod flaps, test noise, fleet
noise, problem events), a fleet-wide noise summary, and **rate-based trigger
candidates** — hosts whose rate is chronic (`chronic_min_per_day`, default 10)
with **zero** problem events in the window, i.e. exactly the hosts alerting
misses. Problem events come from `event.get` (source 0/object 0/value 1) over
the same window. Scoped sweeps drop test *hosts* (ADR 080 semantics:
explicitly named hosts are always analyzed); rows are capped by
`max_results`; a truncation past `max_hosts` is stated, never silent (ADR 011).

The operator follow-up — a rate-based Zabbix trigger
(`count(0-samples)/1h > N`) so chronic flappers alert — is deliberately left
to the operator (`create_trigger` can do it once a threshold is picked); the
tool's job is to make the candidates visible.

## Test approach

`tests/test_check_flaps.py` (+14): each audit fact as an invariant —
distant-pair dips are noise / same-country pairs are **not** (a shared-DC
event is real), the ≥3-host unknown-country fallback, two-service host
events, TEST-only dips tracked at weight 0 and unable to fabricate fleet
noise, a chronic 30-dips/3d host ranks at 10/day; TEST-name matching with
bracketed forms and the lookalike words; wire contract (history requested
with the item's own `value_type`, scope required, no-keys message).
783 → 797.

## Consequences

- The chronic-but-silent class is now enumerable, with prober noise
  subtracted rather than blamed on hosts. Tool count 164.
- Bounded by design: this is a scoped diagnostic, not a fleet sweep — the
  hourly proxy in the reports repo covers the always-on wide view.

## Not included

- **Auto-creating the rate-based trigger.** Threshold choice is an operator
  decision; the tool names the candidates.
- **Latency-degradation flaps** (check slow but passing) — different data
  (item latency), different tool.
