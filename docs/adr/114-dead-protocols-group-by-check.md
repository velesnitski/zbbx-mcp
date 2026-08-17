# ADR 114 — A dead protocol is a fleet finding, not N host findings

**Status**: Accepted (2026-08-17)
**Affected**: `src/zbbx_mcp/tools/dead_protocols.py` (new),
`tools/__init__.py`, `tools/tiers.py` (`ops`), `tests/test_dead_protocols.py`.
**Extends**: ADR 103, 110, 111. Ports the reports-side dead-protocol detector.
**Closes**: task 178(c).

## Context

A user report of "connects, but no internet" was investigated with the existing
tools. Every one of them said the host was fine: agent reachable, traffic
symmetric and normal for the hour, all protocol checks green, no problems.

The actual finding took a scope test to reach. One protocol check read **0 on
every host queried across a whole product fleet** — several countries, several
providers — and had done so for a full day. **Not one alert had fired**, and
nothing in the fleet could have raised one, because availability is defined as
*any protocol answers* and the other protocols were healthy.

The rule is right: one stuck check must not condemn a serving machine. Its cost
is that a protocol can die completely and leave no mark. That blind spot needs
its own surface.

## Decision

A new tool, `detect_dead_protocols`. Two choices come from the incident rather
than from the reports-side original.

**Discovery walks every `*check*` item, not the configured service keys.** The
check that was down is not one of the three `ZABBIX_SERVICE*_CHECK_KEY` values.
A detector scoped to configured keys would have reported the fleet perfectly
healthy — which is exactly what everything else did. Firewall-table assertions
(`nft.*`) and non-uint items are excluded: they are not reachability, and a 0/1
verdict on a string-valued check is meaningless.

**Results group by CHECK, with a denominator.** As per-host rows the live case
is hundreds of lines nobody reads. As one row — *dead on N of N judged hosts* —
it is a platform outage with a single owner. The **dead/judged ratio is the
discriminator** between "this box is broken" and "this protocol is down
everywhere", and it is the question an operator actually has. The denominator
counts hosts where the check could be *judged*, not hosts where it failed;
counting only failures would make every key read 100% dead and destroy the one
number that matters. A key dead on 1 of 1 host is not a fleet claim, so
`_MIN_FLEET_JUDGED` guards the ratio against tiny samples.

Verdict order is deliberate: **too young** before everything (a verdict from
two samples is an artefact), then **host dark** (a machine entirely down is the
SLA's finding — listing each of its protocols here would bury the real ones),
then **died** vs **never up**. Those two are kept apart because they need
different actions: died is a regression with a timestamp to match against a
deploy, never-up is a provisioning gap — what an item looks like right after
recreation.

Hosts that could not be judged are named, not silently dropped (ADR 110), and
`value_max` is used rather than `value_avg` because a uint hour collapses to
its minimum under Zabbix integer division and cannot tell "answered once" from
"never answered" (ADR 0048).

## Consequences

- The failure mode that produced a day-long silent outage now has a surface,
  and it reads as one line rather than as noise.
- Added to the `ops` tier — this is an incident-response question.
- The tool deliberately does not alert or change any verdict. It reports what
  the availability definition cannot express; changing that definition is a
  separate decision with its own re-baseline (open, task 185).

## Verification

988 tests pass (+18). The load-bearing ones: a single pass anywhere in the
window still counts alive (the tool must not contradict the availability rule,
only see behind it); the denominator comes from judged hosts and not from
failures; a fully dark host is deferred rather than reported; and the wire test
pins key-pattern discovery plus `value_max`.
