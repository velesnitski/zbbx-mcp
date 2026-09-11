# 140. A last value is current only while the item reports

## Status

Accepted (v1.16.65)

## Context

Two defects of the same family surfaced in one week of real use, both in
places earlier ADRs had not reached.

**A dead agent's last reading was printed as "now".** Zabbix keeps an item's
`lastvalue` forever. The trend tools, the CPU map behind several inventory
tools, and the infrastructure workbook all read it without `lastclock`. A host
whose agent had been silent for a day showed "CPU 100% now" (its idle item
had stopped at 0) and "0 GB" of memory (a total-memory item that had never
reported). Both were rendered as measurements, and the workbook's decommission
logic sorted on them. ADR 136 removed this defect from the CPU verdict and
ADR 137 from connection counts; every other "current" surface still had it.

**Product and tier filters were still substring matches in most modules.**
ADR 137 made the filters exact where it found them. Twenty-seven more sites,
across thirteen modules, kept `tier.lower() in label.lower()`, so a filter
for one tier also admitted every tier whose label contains the word. The
batch trend tool, asked for one country's free tier, returned more than
twice the set the caller meant, with nothing in the output saying so. Every
figure built on such a call was a figure about a different fleet.

## Decision

**A `lastvalue` is a current reading only if the item reported recently.**
`live_value(item, now)` returns the value when `lastclock` is within
`LIVE_VALUE_MAX_AGE_S` (30 minutes) and `None` otherwise: for the
never-collected sentinel, for a stale clock, for an unparsable value, and
for a missing `lastclock` — a caller that did not ask for the clock gets
"not reporting", never a number. The CPU map, the trend rows' `current` and
the workbook's four live columns all go through it. `TrendRow.current` is
now optional and renders as `n/a (not reporting)`; the period's average,
peak and minimum stay, because history is not what went stale.

**Label filters are exact everywhere.** Every remaining substring site uses
`label_matches`, and a guard test pins the tree so the pattern cannot return.
The one substring left alone is the hide-products exclusion, which is an
operator's deny-list and widens on purpose.

**`compare_servers` reports minimum and trend** alongside average, peak and
now, so the named-set fallback carries the same columns as the batch tool.

## Consequences

A host whose agent is down now shows no "current" value anywhere. That is
the correct reading and it is louder than before: the tools used to fill the
gap with the last number they had.

The 30-minute window is a constant, not a setting. Agent items on this
deployment refresh every one to five minutes; a value older than half an
hour is not a reading the caller can act on, and making the threshold
configurable would let a deployment quietly restore the defect.

Callers that filtered by a partial label — `tier="Free"` expecting every
free-family tier — now get the exact tier only. That is the change: a caller
who wants the family names each member, and the count they get is the count
they asked for.
