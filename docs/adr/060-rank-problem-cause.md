# ADR 060: `rank_problem_cause` — write cluster correlation into Zabbix

**Status:** Accepted
**Date:** 2026-06-12

## Problem

`get_outage_clusters` detects correlated incidents — N hosts down on one
subnet/provider — and `get_active_problems` collapses cascades for its
own rendering. But that correlation knowledge lives and dies inside an
MCP response: the Zabbix UI, escalations, and every non-MCP consumer
still see N independent problems. A recent live snapshot rendered "10
cluster incidents collapsed from 87 individual problems" — purely
cosmetically.

Zabbix 6.4+ has first-class **cause/symptom event ranking**
(`event.acknowledge` bit 256 + `cause_eventid`; bit 128 ranks back to
cause), which nests symptoms under their cause everywhere. The capability
was even listed in this server's own `get_zabbix_version` feature matrix
— detected, never used.

## Decision

New write tool `rank_problem_cause(symptom_event_ids, cause_event_id,
unrank=False, message="")` in `problems.py`:

- rank: action 256 on the symptom ids with `cause_eventid` (Zabbix
  requires it for that bit); optional message adds bit 4.
- `unrank=True`: action 128 ranks the listed events back to independent
  causes (no `cause_eventid` — passing one is rejected).
- Input validation (ids present; cause required unless unranking;
  mutually exclusive) happens before any API call.

The intended workflow is the cluster follow-up: `get_outage_clusters`
names the correlated events → one `rank_problem_cause` call collapses
them **at the source**, so the Zabbix UI nests them and every consumer
sees one incident. Bitmask assembly lives in the pure helper
`_build_rank_action`.

Registered as a write tool (`WRITE_TOOLS`), so `ZABBIX_READ_ONLY`
disables it. No rollback snapshot — consistent with the other
`event.acknowledge` tools (rank changes are themselves reversible via
`unrank`).

## Test approach

Three pure-helper tests (`TestBuildRankAction`): symptom (256), unrank
(128), message combos (260/132). Registration/count covered by the
updated `EXPECTED_TOOLS` + `test_server.py` assertions like every tool.

## Consequences

- **Tool count 161 → 162** (`ALL_TOOLS`, `EXPECTED_TOOLS`,
  `test_server.py`, CLAUDE.md updated). Tests +3 (585 → 588).
- Cluster correlation becomes durable and visible outside the MCP.
- Minor version bump (1.14.0) for the new tool.

## Not included

- **Auto-ranking from `get_outage_clusters`.** Deciding what is one
  incident is operator judgement; the read tool stays read-only and the
  write is its own explicit, auditable call.
- **Surfacing cause/symptom state in problem listings** (`problem.get`
  returns `cause_eventid`). Worth adding to `get_problem_detail` when
  ranking sees real use; until then it would be dead columns.
- **Replacing the trigger-dependency collapse (ADR 048/050).** That
  works on *declared* dependencies with zero operator action; ranking is
  manual. They compose — revisit unification if ranked events become the
  norm.
