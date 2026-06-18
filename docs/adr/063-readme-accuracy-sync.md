# ADR 063: README accuracy sync to the v1.15.x state

**Status:** Accepted
**Date:** 2026-06-18

## Problem

The README's hand-maintained counts had drifted across several releases
and were even self-inconsistent: the badge said **161** tools, the tier
table's `full` row said **156**, and the prose said a **154**-tool
catalog — three different numbers for the same total, none of them the
real **162**. The per-tier sizes were all stale too (core 25, ops 52,
finance 47, reports 63 vs the real 27 / 57 / 49 / 65), the `initialize`
example still showed `serverInfo.name = "zabbix"` (it carries the version
since ADR 038), the CLI table omitted `--version` (ADR 061), and the
requirements still claimed "Zabbix 6.0+, tested on 6.4" though the client
now spans 6.2–7.x and runs against 7.4 (ADR 055).

## Decision

Sync the README to reality, with the numbers **computed**, not guessed —
the tier sizes were read from `len(ALL_TOOLS)` and
`resolve_tier_disabled(tier, ALL_TOOLS)` rather than hand-counted:

- tool badge/headline → 162; dropped the brittle "across N modules" phrase;
- tier table → core 27 / ops 57 / finance 49 / reports 65 / full 162, and
  the prose catalog size → 162;
- added `get_problem_age_buckets` and `rank_problem_cause` (ADR 060) to
  the Problems row;
- `initialize` example → `serverInfo.name = "zabbix v1.15.1"`;
- added the `--version` flag to the CLI-options table;
- requirements → "Zabbix 6.2+ … tested on 7.4".

Docs-only; no code or test change.

## Test approach

None — documentation only. The tier figures were derived from the live
`ALL_TOOLS` / `resolve_tier_disabled` so they match what the server
actually registers; the full suite (608) is unaffected.

## Consequences

- The README's counts match the code again, and the `full` total agrees
  with the badge, the headline, and `test_server.py`'s 162 assertion.
- Tool count unchanged (162); tests unchanged (608).

## Not included

- **Automating the counts.** The drift root cause is that README numbers
  are maintained by hand. A small `make readme-counts` (or a test that
  asserts the badge equals `len(ALL_TOOLS)`) would prevent recurrence;
  deferred — flagged here so the next drift becomes an automation task,
  not another manual patch.
