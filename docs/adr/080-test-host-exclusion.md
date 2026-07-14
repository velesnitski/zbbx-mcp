# ADR 080: Test/staging hosts must not pollute fleet verdicts

**Status:** Accepted
**Date:** 2026-07-14

## Problem

Non-production boxes are monitored alongside production ones, and nothing in
the tooling told them apart. A test box therefore landed in every fleet-wide
verdict it was scoped into: it padded "analysed N servers" counts, contributed
phantom service-check failures to protocol sweeps, and dragged on uptime and
SLA aggregates. Live consequence: a fleet-wide protocol sweep reported a
handful of failures, one of which was a test box — a material distortion of a
small numerator.

The obvious fix — "classify by host group" — **does not work here**, and the
reason matters:

- The instance *has* test host groups, but they are effectively unused.
- The test boxes are instead full members of **production** groups.

So a group-membership check would miss precisely the hosts that cause the
damage. Conversely, a name check alone is not sufficient either: a box may be
correctly grouped but blandly named.

A naive `"test" in name` substring is also wrong — it silently swallows
`latest`, `contest`, `fastest`, `attestation`.

## Decision

**`is_test_host(host)`** in `classify.py` (pure): apply one token-bounded
pattern to the **host name** *and* to **every group name**, and take the union.
Both signals are checked because neither is reliable alone.

- Default pattern `(?:^|[-_\s])test(?:[-_\s]|$)` — token-bounded, so
  lookalike words cannot trip it. The separator class includes **whitespace**
  because group names contain spaces where host names use dashes.
- Overridable via **`ZABBIX_TEST_NAME_RE`** (per the repo rule against
  hardcoded identifiers); an invalid regex falls back to the default rather
  than crashing the server.

**`partition_test_hosts(hosts)`** in `data.py` returns `(production, test)` —
a *split*, not a filter, so the caller can report what it dropped.

**`excluded_test_note(test_hosts)`** renders a footer naming them. Silence is
not an option: an invisible skip is the exact class of bug ADR 011 exists to
kill. (The helper is named `excluded_test_note`, not `test_excluded_note`,
because pytest collects any imported callable prefixed `test_` as a test case
— a production helper must not carry that prefix.)

Wired into the tools where a test box demonstrably changes a conclusion:
`search_items` and `detect_traffic_drops`, each gaining `include_test: bool =
False`. `search_items` additionally now requests `selectGroups`, without which
half the signal is unavailable.

## Test approach

`tests/test_test_hosts.py` (+23): token-bounded matching on leading, middle,
trailing and underscore-separated names; the lookalike words are all rejected;
group-only detection including a space-separated group name; production groups
never match; the real-world case (test-named box inside a production group);
env override and invalid-regex fallback; partition split; the note names what
was dropped and truncates long lists. 724 → 747.

## Consequences

- Fleet sweeps, traffic-drop analysis and item searches exclude non-production
  boxes by default, and *say so* — the excluded hosts are named, never dropped
  in silence.
- Operators who want them back pass `include_test=true`.
- Tool count unchanged (163).

## Not included

- **The remaining aggregate tools** (uptime/SLA, at-risk, bulk diagnosis).
  The core is shared and pure, so wiring them is mechanical; this ADR ships the
  two with a demonstrated distortion rather than a sprawling diff.
- **Fixing the estate.** The right long-term answer is to put the boxes in the
  test groups (or tag them) rather than leave them in production groups. The
  group half of the rule starts working the day that happens; until then the
  name half carries it.
- **Genuinely ambiguous names.** A host whose name contains both a test and a
  production marker cannot be resolved by regex. The design makes such a host
  *visible* in the excluded footer and overridable by env, which is the safe
  failure mode, but the call is a human's.
