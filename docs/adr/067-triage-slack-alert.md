# ADR 067: `triage_slack_alert` — authoritative verdict for an alert line

**Status:** Accepted
**Date:** 2026-06-25

## Problem

Dogfooding an AI Slack alert-feed by hand (resolve the host, then call
`diagnose_host` / `get_problems` / `get_active_problems`) worked but
exposed two gaps that a one-shot tool should close (tasks 164/165):

1. **The feed's state can't be trusted — in both directions.** A wall of
   "RESOLVED" sat over 100+ live problems (29 Disaster), and "REAL
   PROBLEM" lines named triggers that had already recovered. Any verdict
   must come from re-querying Zabbix, never from the feed's claim.
2. **The alert host-name is not the Zabbix host object.** Protocol/probe
   triggers embed the server in the trigger text (the probe runs on a
   prober host), domain checks live in a Web-Check group, and some names
   don't resolve at all. Resolution is a fallible first step that must
   return AMBIGUOUS rather than guess wrong — the same substring-collision
   trap a recent externalscripts audit surfaced.

## Decision

A new **read-only** tool `triage_slack_alert(text)` that orchestrates
existing primitives, plus a pure core (`alert_triage.py`) split out for
testing:

- **Parse** (`parse_alert_line`): severity, claimed-state (advisory
  only), and host candidates. `extract_host_candidates` pulls hyphen/dot
  host tokens and bare short hosts (`db14`), and drops multi-VIP suffixes
  (`bb2` after `…-a1 `) and tokens that are merely part of a hyphen
  host (`br3` in `node-eu-br3`).
- **Resolve** (task 165, `classify_match` + `_search_host`): exact
  host-name match wins; one fuzzy hit is FUZZY (catches multi-VIP and
  Web-Check domains via substring search); several is **AMBIGUOUS**; none
  is **NOT_FOUND**. Never guesses.
- **Re-query** (lesson 1): `problem.get` for the resolved hostids,
  `filter_suppressed`, and a `trigger.get selectDependencies` dep-map for
  symptom detection — the same path the problem/floods tools use.
- **Classify** (`classify_host_triage`): `real_now` / `recovered` (feed
  stale) / `symptom_of_cluster`, with a recommended action. The host's
  current problem names are listed so the operator sees ground truth.

**Read-only by design.** It does not acknowledge, suppress, rank, or run
scripts — it only reads and reports. `triage_slack_alert` is therefore
**not** in `WRITE_TOOLS`.

## Test approach

`tests/test_triage.py` unit-tests the entire pure core (24 cases): the
host-candidate extractor (the "mapping table" task 165 asked for — comma
lists, domains, VIP-suffix drop, in-token suppression, port/duration
rejection, dedup), severity/state detection, the EXACT/FUZZY/AMBIGUOUS/
NOT_FOUND match logic, and the verdict classifier. The async wiring is
config-level over already-tested helpers (`filter_suppressed`,
`collapse_dependent_problems`).

## Consequences

- **Tool count 162 → 163** (`ALL_TOOLS`, `EXPECTED_TOOLS`,
  `test_server.py`, CLAUDE.md, README updated). Tests +24 (608 → 632).
- Live verification pends an MCP-server reconnect (the running build
  predates the tool).

## Not included

- **Write actions behind a flag** (ack-with-triage-note, `rank_problem_cause`
  to collapse a flap cluster). Deferred deliberately — ship the safe
  read-only core first; the write path is a follow-up with its own
  `WRITE_TOOLS` entry and `ZABBIX_READ_ONLY` gating. **Never** auto-remediate
  (`execute_script`) — operator-only.
- **Flap detection from event history.** v1 reads the current state;
  distinguishing a true flap from a steady problem needs `event.get`
  transition counts — added when the read-only verdict proves useful.
- **Adding it to the `ops` tier bundle.** It fits incident response;
  left in `full` only for now to keep this change focused.
