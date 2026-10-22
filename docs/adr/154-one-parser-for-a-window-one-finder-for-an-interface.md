# 154. One parser for a window, one finder for an interface

## Status

Accepted (v1.16.76)

## Context

Two patterns this project has already fixed once kept surviving in tools
the fix did not reach.

**Time windows.** ADR 134 established that input which was not understood
is not input that was not given, and `utils.parse_time` exists for it.
`get_events` still called `int()` on its window, so an ISO datetime became a
traceback. `get_audit_log` kept a private parser that returned 0 for any
format it did not know — including a datetime with seconds — and 0 meant
"no window": a question about five hours came back as the newest rows of
the whole log, with nothing to say the window had been dropped.

**Interfaces.** ADR 147 moved the trend tools to prefix discovery through
the one physical-interface definition, because the stock agent template
quotes the interface name. `get_traffic_report` and
`detect_traffic_anomalies` still used the exact list of unquoted keys, so a
host on that template had no traffic in either.

`get_latency_estimate` ranks servers by great-circle distance from a
country; it measures nothing, while its name and first docstring line read
as a measurement.

## Decision

- `get_events` and `get_audit_log` parse their windows with
  `utils.parse_time` and refuse an unreadable value before calling the API,
  naming the accepted forms.
- `get_traffic_report` and `detect_traffic_anomalies` find interfaces with
  `fetch.physical_traffic_items`.
- `get_latency_estimate` keeps its name for compatibility; its description
  now opens by saying it is a distance ranking, not measured latency, and
  points to the tool that measures reachability.

## Consequences

A window that cannot be read is now a message rather than a silently wider
answer. Hosts on either agent template carry traffic in every traffic tool.
