# 150. Coverage that asserts behaviour

## Status

Accepted (v1.16.72)

## Context

After ADR 144 the suite covered 67 % of the code with branches counted.
Thirty-six tool modules sat below 60 %, several below 20 %, and the CI floor
was 15 %, a number that could not fail. Line coverage alone also says nothing
about whether a covered line is checked: a test that calls a function and
asserts nothing covers it.

## Decision

Five layers, each answering a different question.

**Wire tests for every tool module.** Each tool is driven through the
recording client with a small neutral fleet, an empty fleet and a degraded
case, and the test asserts the text returned, the exact API parameters sent,
any file written and, for write tools, the payload and the rollback journal.

**A survival sweep over every registered tool.** Every tool is called with
neutral arguments against an empty instance and against one whose every call
fails; it must return a message, never a traceback. The module list is read
from the registration itself, so a new tool is swept without a test change.
Sockets, name resolution and the user's home are blocked inside the sweep.

**Property tests on the pure core.** Budget fitting never exceeds its
budget and names exactly what it left out; a reading has a value if and only
if it is live; label matching, interface-key recognition, time and retention
parsing hold over generated inputs. Generated host names and addresses are
confined to the fixture conventions of ADR 127.

**Mutation testing on the core.** `scripts/mutation.sh` mutates the
reading, budget, data, formatter, catalogue-map and classifier modules and
runs the behavioural tests against each mutant. Surviving mutants in the
reading, budget and catalogue modules were killed by new tests or recorded as
equivalent with the reason; the scores are a weekly figure, not a gate.

**End-to-end integration against a real server.** A pinned, throwaway
Zabbix stack is seeded with a synthetic fleet and the server is driven over
JSON-RPC as a subprocess. This is the only layer that catches a wrong API
parameter name. It runs nightly and on demand in CI and skips locally when no
stack is configured. It uses the image's public default administrator login
against a container bound to the loopback interface, and no secret.

**The floor is raised to 85 % with branch coverage**, so the figure can now
fail a build.

## Consequences

Line and branch coverage moved from 67 % to 93 %; the suite grew from about
1,700 to about 2,600 tests. Writing the tests found real defects, fixed with a
failing test first: a group filter that compared ids against a fetch that
carried names and so matched nothing; a region default that silently dropped
every country not listed in a named region; a value map that discarded a live
reading of exactly zero, so idle hosts vanished from traffic views; sub-host
problems named after the parent; and a CSV export that wrote to a fixed
folder without the path confinement every other writer uses.

Write-path weaknesses that need a decision rather than a one-line fix are
recorded as findings, one as a strict expected failure so it flags when
fixed: template link and unlink semantics, bulk macro writes without a
rollback entry, and journal ordering around failed deletes.
