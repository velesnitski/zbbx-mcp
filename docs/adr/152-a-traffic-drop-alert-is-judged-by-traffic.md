# 152. A traffic-drop alert is judged by traffic

## Status

Accepted (v1.16.74)

## Context

`triage_slack_alert` re-queries live problems so that a feed's stale claim
is never trusted (ADR 067). For one trigger class that is the wrong ground
truth. A "traffic fell sharply" trigger compares the last minutes with a
short baseline; once a node settles at a few percent of normal the
condition no longer holds and Zabbix closes the problem. The classifier saw
no active problem and returned "recovered" over a node that was still down.
Scored against hourly interface data, two of seven verdicts in one night
were "recovered" on nodes that stayed far below normal until they were
replaced the next morning, and one "problem" was a node that drops and
returns on a cycle.

## Decision

**For the traffic-drop class the verdict comes from the carrier interface.**
The class is recognised from the alert text or the problem names by a
generic pattern, replaceable per deployment with
`ZABBIX_TRAFFIC_DROP_PATTERN`. The carrier is the physical interface with
the highest seven-day average (ADR 078/109), so an idle secondary NIC is
never the one judged. The last two hours are compared with the median of
this hour of day, ±1 h, over the previous seven days — the drop detector's
bucket (ADR 097/100) at the median rather than the floor, because the
question is "is it back to normal". **"Recovered" requires at least half of
that.** Every verdict quotes both numbers; without history the verdict is
`unmeasured`, never `recovered`. Every other trigger class keeps the
problem-table verdict.

**The drop is given a shape before the verdict**, from the last day of
one-minute samples:

- `maintenance` — the drop sits on a gap in the samples (restart, redeploy);
  advice: re-check in 30 minutes.
- `periodic` — at least two earlier troughs recovered on their own and the
  current one is no longer than one and a half times the longest of them;
  advice: not a block by this evidence, check rotation and firewall rules.
  The length condition is the guard: a node that used to cycle and has now
  been down for hours is a current outage, not a cycle.
- `cliff` — under 5 % of usual within 15 minutes; `decay` — the same fall
  spread wider. Both report `still_down` with the minutes since the drop.

## Consequences

Three more API calls per host on traffic-drop alerts only: the interface
items, a week of hourly trends, a day of history. The shape logic is pure
and tested on synthetic series; the end-to-end layer checks that a real
server accepts the calls. The count shown in the feed's own one-line
summary is produced by the feed, not by this tool, and is out of scope here.
