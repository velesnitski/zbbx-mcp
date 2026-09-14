# 141. A reading is a value with an age

## Status

Accepted (v1.16.66)

## Context

The same defect has now been removed three times, once per surface.

Zabbix keeps an item's `lastvalue` forever. Read on its own it is
indistinguishable from a live measurement. A dead agent's final idle reading
of 0 was printed as "CPU 100% now"; a never-collected total-memory item was
printed as "0 GB"; a day-old session count was ranked as the lowest real
value. ADR 136 fixed the CPU verdict, ADR 137 the connection counts and
traffic totals, ADR 140 the trend rows, the CPU map and the infrastructure
workbook. Each fix was correct and each was local: it taught one reader to
look at `lastclock` before believing `lastvalue`.

v1.16.65 added `live_value(item, now)` so a reader had somewhere to go. It
did not stop a reader from going elsewhere. A survey after that release
found fifty-three direct reads of `item["lastvalue"]` across twenty-five
modules: the dashboard export, the two Excel reports, the Slack digest, all
five load and capacity listings, the predictive regressions, the idle-relay
detector, the role analysis, the agent-reachability check, the SLA
dashboard's service map. Most of them fetched the value without its clock,
so even a reader who wanted to check the age could not. The three earlier
ADRs had each patched the site in front of them; the population they came
from was untouched, and the next patch would have been the fourth.

## Decision

**A last value is a `Reading`, not a number.** `reading.read_item(item,
now)` is the one function that turns a Zabbix item into a value. It returns
a frozen `Reading(value, age_s, state)` whose `value` is a float only in
state `live` and `None` in every other state — `never` (the `lastclock = 0`
sentinel), `stale` (older than `LIVE_VALUE_MAX_AGE_S`, thirty minutes),
`unparsable`, and `missing_clock` for a caller that did not request the
clock, which fails closed. `text(unit)` renders a live reading as `12.3 %`
and anything else as `n/a (not reporting)`, the wording ADR 140 pinned.
`fetch.live_value` is now a view over it for callers that only need the
number; `fetch.live_items`, the `data.build_value_map` and `build_max_map`
helpers, `cpu_pct_from_items`, `connections_from_items`,
`carrier_traffic`, and the traffic map that several tools use to say "it
has traffic, so it is up" all read through it. The type lives in its own pure module
because `data.py` and `fetch.py` import each other; `fetch` re-exports it so
the boundary has one name.

**A guard test makes the boundary a property of the tree, not a habit.**
`TestReadingBoundaryGuard` scans every module outside `reading.py` and
`fetch.py` for `["lastvalue"]` and `.get("lastvalue")` and fails on any hit
that is not on an explicit allow-list keyed by module and code fragment, each
with the reason it stays. An entry that stops matching fails too, so the list
cannot outlive the code it excuses. Six entries remain, of two kinds: generic
listings that print every value type beside its own timestamp (the item
search now carries an *Updated* column so that statement is true of it), and
checks that run on their own schedule rather than an agent's — certificate,
registration and https checks, web scenarios, the monthly cost snapshot —
where the thirty-minute window is the wrong instrument. A per-class window
for those is future work; until then they are named, not hidden.

**Choosing which history to analyse is a ranking, not a reading.** Five
tools each carried a private `_lv()` that sorted a host's interfaces by raw
last value to pick the carrier NIC before fetching its trends. That sort is
legitimate — a dead host's carrier NIC is still its carrier NIC, and ranking
by a live value of `None` would hand the analysis an idle interface's history
(ADR 135) — but it must not leak the number. `fetch.rank_by_last_value` is
the one sort key; it returns items, never a value.

**Why a type at the boundary beats a fourth patch.** A patch fixes the site
that failed and teaches nothing to the site that has not failed yet. A type
changes what a site *can* do: with `Reading` the honest path is the short
one, the dishonest path is a lint failure, and a new tool written next month
inherits the rule without having read the ADR. The three earlier fixes were
not wrong; they were addressed to the wrong population.

## Consequences

Every "current" surface now agrees. A host whose agent is silent for more
than thirty minutes has no CPU, load, memory, connection or traffic figure
in the dashboard export, either Excel report, the Slack digest, the load and
capacity listings, the SLA service map, the idle-relay and role analyses, or
the predictive alerts; it reads as not reporting, and the rollups that used
to average it in no longer do. That is louder than before, and correct.

`get_agent_unreachable` uses the shared window: a ping older than thirty
minutes is `STALE`, where it used to tolerate an hour. `diagnose_host`
renders a silent agent as "silent for N minutes" rather than as its last
value with an age beside it. `carrier_traffic` leaves a stale host uncovered
— named in the total's disclosure — instead of summing the rate it once had.

Test fixtures that carried a fixed clock from a past release now carry a
clock relative to the run, because a fixed clock ages out of the window; a
fixture that means "never collected" still says `0`.

The window is still one constant. The scheduled-check sites on the allow-list
are the argument for a second one; adding it is a change to `read_item`, in
one place, and the guard will show exactly which sites it should reach.
