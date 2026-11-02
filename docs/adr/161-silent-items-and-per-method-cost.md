# 161. Silent items in trend summaries; per-method API cost

## Status

Accepted (v1.16.81)

## Context

ADR 141 made a trend row's `current` reading `None` when its item has stopped
reporting, so that a dead agent's last value is no longer presented as live.
Four readers still treated it as a number: the per-country sum and the
blocked-host listing in `get_geo_traffic_trends` / `detect_regional_anomalies`,
the traffic-drop check in `get_health_assessment`, and the blocking-risk
check in `generate_service_brief`. Each raised `TypeError` on the first such
host and failed the whole fleet report. Until ADR 160 the month-scale reads
failed earlier with HTTP 500, which hid this.

The type checker did not see it: these rows arrive through a lazily
re-exported fetch helper, typed `Any` at the call site.

ADR 160 added per-tool API call counts and time. Measured live, the heavy
tools spend 8–13 s on 11–30 calls with the summed API time about twice the
wall time — already parallel — which leaves the question of *which* method
dominates unanswered.

## Decision

- A silent item contributes nothing to a summed `current`, is skipped by the
  drop comparisons, and reads `N/A` in a listing. The average, which comes
  from trends rather than the last value, is unaffected.
- Each analytics line also carries `api_top`: the three methods with the most
  time, as `method:calls/seconds`. Method names only, never parameters.

## Test approach

Each of the four readers gets a wire test with one item whose `lastclock` is
the never-collected sentinel; each test fails with the live `TypeError`
without the fix. The service-brief test reaches its comparison only for a
busy host in a country that carries traffic, so its fixture has one live and
one silent host in the same country. `api_top` is tested for ranking, for the
empty case and through a tool call that mixes methods under `gather`.

## Consequences

The four reports answer with silent hosts present. The next performance
change is chosen from `api_top` across real calls.
