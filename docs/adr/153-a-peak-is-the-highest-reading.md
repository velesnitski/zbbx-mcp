# 153. A peak is the highest reading

## Status

Accepted (v1.16.75)

## Context

Two defects surfaced in one ordinary capacity question about a single
server over thirty days.

**Outbound traffic was printed in bits per second under an "Mbps" header.**
ADR 147 moved the trend batch to prefix discovery for both directions and
added the outbound metric, but the unit conversion, the trend-direction
comparison and the daily breakdown still tested for the inbound metric name
only. A server moving 25 Mbps out showed 24,813,822.

**The peak analysis answered a different question than it was asked.** For
a single host it printed the last 48 raw hours whatever the period, and its
summary called the largest hourly *average* the peak. Over thirty days with
one short burst near the port ceiling it reported "Peak: 72". It also found
interfaces through the exact key list ADR 147 retired elsewhere, so a host
whose interface key is quoted or off the list had no traffic at all, and it
read any period not ending in `d` as seven days without saying so.

## Decision

- The trend batch treats both traffic directions alike for conversion,
  trend direction and the daily breakdown.
- The peak analysis builds one hour-of-day profile for a fleet slice and
  for a single host alike, names the host, and states the highest single
  reading (the hourly maximum, not an average) with the hour it occurred.
  When the trend history covers less than nine tenths of the requested
  window it says how much it covers. Periods are `Nd` or `Nw`; anything
  else is refused with the accepted forms. Interfaces are found by prefix
  through the shared physical-interface helper.

## Consequences

The single-host view no longer lists raw hours; the profile and the
highest reading answer "when is it busy" and "how high did it go", which is
what the tool is for. Tests pin the units of the outbound rows, the true
peak over a long quiet window, and the coverage line.
