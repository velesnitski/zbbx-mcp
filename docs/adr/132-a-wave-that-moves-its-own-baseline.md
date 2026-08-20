# ADR 132 — A wave that moves its own baseline

**Status**: Accepted (2026-08-20)
**Affected**: `tools/traffic_erosion.py` (`subnet_waves`, `_net_of`,
`detect_traffic_erosion`), `tests/test_subnet_waves.py`.
**Extends**: ADR 091, ADR 100.

## Context

ADR 091 made erosion **cohort-relative**: a host is eroding only when it
declines materially faster than its scope's median. That is what stops a
market-wide dip from being reported as N independent host failures, and it is
the reason the tool can be trusted at all.

It also guarantees a blind spot, and the blind spot grows with the size of the
event. When correlated decliners **dominate** their cohort they drag the median
down with themselves. Every member then sits at roughly the cohort slope, the
relative test finds nothing exceptional, and each is labelled *demand*. The
verdict is not merely wrong; the reasoning is circular — the baseline being
compared against is largely made of the hosts under test.

`detect_disruption_wave` does not cover this either: it requires a blast radius
spanning many `/24`s by design, so an event confined to one subnet falls
between the two tools. Neither is broken. The gap is between them.

## Decision

**Add a spatial test, computed on the cohort-blind pass.** The tool already
runs one pass without the cohort to form the median. `subnet_waves` reads
*that* pass, not the final verdicts — a detector fed cohort-relative output
would inherit the exact blind spot it exists to cover.

A wave is **≥3 hosts** in one network, each down **≥20%**, with the spread
between best and worst member **≤15 percentage points**. Demand does not
respect a netmask, so the shape is spatial rather than behavioural.

**Spread, not just count.** Three hosts in a subnet down 22%, 40% and 95% are
three stories. Without the spread rule any busy subnet eventually collects
three decliners and reports a wave.

**`/24` claims its hosts before `/16`.** The tighter network is the more
specific claim. Without the ordering the same hosts surface twice — once as a
rack, once as a range — and a reader cannot tell whether that is one event or
two.

**State the ambiguity rather than resolving it.** When a wave covers half or
more of the judged hosts, the output says so explicitly: the cohort median is
largely theirs, "tracks cohort" is circular, and a subnet event and a
scope-wide fall are **not separable from this data**. That is the honest
answer, and it is more useful than either verdict asserted confidently.

## Consequences

An event that takes out several hosts in one network is now visible even when
it is large enough to hide inside its own baseline.

The tool deliberately says nothing about cause. A shared switch, a shared
transit link and a shared configuration push all produce this signature, and
nothing in trend data separates them. What it can say is that the correlation
is spatial, which is the fact that moves an investigation from "why did demand
fall" to "what do these machines share".

Nine tests, including the dominating-cohort case and the two-waves-not-one
ordering. The spread rule was confirmed to be load-bearing by removing it.

Note for the next author: the test addresses live in RFC 1918 space rather
than RFC 5737, because every documentation range is a `/24` and a "same `/16`,
different `/24`" case cannot be built inside one. The repo-wide address guard
caught the first attempt, which is what it is for.
