# 137. Exact filters, honest counts, and a total that names its gaps

## Status

Accepted (v1.16.62)

## Context

Three defects surfaced from one question — "how much traffic does this slice
of the fleet carry, per user" — and they share a shape with ADR 130, 133 and
136: a value the code did not have, rendered as a value it did.

**A filter that widened itself.** Every product and tier filter in `tools/`
was written as `wanted.lower() in actual.lower()`. Tier labels nest — a base
tier's name is a prefix of its variants — so asking for the base tier returned
the base *and* every variant, and nothing in the output said the scope had
grown. The same function compared `group` and `country` exactly. Ten call
sites across eight files disagreed with each other about what a filter is. A
filter that broadens itself is worse than one that errors: the caller reads a
number about the wrong set and has no way to know.

**A count that was never taken, printed as zero.** `get_traffic_report` fetched
connection items with `output: ["hostid", "lastvalue"]` and read
`float(lastvalue)`. Zabbix marks an item that has never produced a value with
`lastclock = 0`; its `lastvalue` is a placeholder. Without the clock the code
could not tell, and a host moving hundreds of megabits printed "0 connections"
— physically impossible, and therefore never a measurement. The tool already
rendered a *missing* item as `–`; it could not tell a dead item from a live
zero. And `detect_traffic_anomalies`, in the same file, still carried
`host_conns.get(hid, 0)` — the exact line ADR 130 removed from its sibling.

**A total that did not exist.** Every traffic surface is per-host and sorted,
and the response budget truncates the list. Asked for a group's total, a reader
was left summing a table that had been cut off and extrapolating the tail. That
is not a measurement, and it is the wrong shape for the question: a total is
one number and should cost one call.

## Decision

**`label_matches(actual, wanted)` in `data.py`: exact, case-insensitive, empty
means no filter.** All ten sites use it. A caller who wants a family of tiers
asks for each, or filters by group; the tool no longer guesses a wider set on
their behalf.

**`connections_from_items(items)` in `fetch.py` honours the sentinel.** Items
with `lastclock <= 0` are left out of the map, so the caller reads `None` and
renders unmeasured — the same as no item at all. Both connection fetches now
request `lastclock`; a caller that omits it gets every item treated as
never-collected, which fails closed rather than open. `detect_traffic_anomalies`
reads `None` for absence and guards every comparison, so an unknown count can
neither crash the tool nor trigger "active connections but low traffic".

**`get_traffic_totals`** in its own module, `tools/traffic_totals.py`. One call:
the sum of each host's carrier NIC (busiest physical inbound interface, ADR
078/105) across the slice, with coverage in the sentence — *N of M hosts*. A
host with no readable traffic item is **counted as uncovered and named**, never
folded in as zero; never-collected items are counted separately and ignored.
The total is stated as a floor for the slice when anything is uncovered.

## Consequences

Exact matching is a behaviour change for any caller who relied on the
substring: `tier="Free"` now returns the base tier alone. That is the documented
meaning of the parameter, and the tool descriptions say so. No test relied on
the substring behaviour; the fixtures that changed were ones that predated
`lastclock` being requested, and now carry a live clock so each keeps its
original meaning.

`get_traffic_totals` adds one `host.get` and one `item.get` — it does not read
trends, so it reports the instantaneous `lastvalue`, not an average. For a
period average, `get_trends_batch` remains the tool; this one answers "right
now, how much, over how many".

The pure cores — `label_matches`, `connections_from_items`, `carrier_traffic`,
`summarise` — are tested without a client. Wire tests pin that `lastclock` is
actually requested, because the parser fails closed without it and a silent
"all unmeasured" would be the next bug in this family.
