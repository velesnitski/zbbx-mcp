# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [1.16.6] - 2026-07-03

### Added — runtime self-awareness (stale-build warning + token accounting)
ADR 073. Two things the server knew and never said: (1) after a release
bump the running process silently serves the old build until the MCP
client reconnects — `check_connection` now compares its in-memory
`__version__` against the source tree's `pyproject.toml` and warns
"Running build vX, but the source tree is vY — reconnect /mcp"
(suppressed for wheel installs / unknown versions, so no false
positives); (2) `get_telemetry_summary` now ends with
`Σ responses: N chars ≈ M tokens (~K tokens/call)`, making
token-effectiveness a one-call answer instead of manual math. +10 tests
via the shared wiretest scaffolding; 659 → 669.

## [1.16.5] - 2026-07-03

### Added — architecture guards (tests + docs only, no runtime change)
ADR 072. An architecture review found the design sound but two recurring
failure classes unguarded: (1) invalid Zabbix API params reaching the
wire — `problem.get`+`selectHosts` shipped twice (ADR 068/070), each
crashing a tool live with -32602; (2) hand-maintained doc counts
drifting (ADR 063: three different totals in one README). New
`tests/test_guards.py`: an AST contract-guard scanning every
`client.call(...)` dict literal against a deny-map, and a doc-count
guard pinning the README badge/headline/tier table and CLAUDE.md header
to the computed registry. The thrice-copy-pasted wire-test scaffolding
is extracted to `tests/wiretest.py` (behaviour-identical refactor);
three factually stale CLAUDE.md module rows fixed and the new-tool
checklist extended. 654 → 659 tests.

## [1.16.4] - 2026-07-03

### Added — `get_problem_detail` surfaces symptom rank and snooze state
ADR 071 (task 162). The ADR 059/060 write paths (snooze, cause/symptom
ranking) had no read path — deferred "once snooze/rank see real use",
which a live feed validation has now demonstrated. `get_problem_detail`
requests `suppress_until` and renders a `Suppression:` line (maintenance
window / snoozed-until-resolve / remaining time / lapsed) via the new
pure helper `_format_snooze_status`, and renders `Rank: symptom of cause
event N` when `cause_eventid` is non-zero (arrives free via
`output: "extend"`; absent on pre-6.4 servers → simply not rendered).
+10 tests (6 pure + 4 wire-contract); 644 → 654.

## [1.16.3] - 2026-07-03

### Fixed — `get_recent_changes` crashed on every call (same `selectHosts` class as v1.16.1)
ADR 070. Found live during a feed-vs-Zabbix cross-validation: the tool's
`problem.get` carried `selectHosts`, which `problem.get` rejects
(`-32602`) — and its host column read a field `problem.get` never
returns. Same fix as ADR 068: drop `selectHosts`, add `objectid`, map
problem → host via one scoped `trigger.get`; the resolved-events
`event.get` branch (which supports `selectHosts`) is untouched. A
full-repo sweep of all 30+ `selectHosts` call sites confirms this was
the **last** `problem.get` carrier. +3 wire-contract tests
(`TestRecentChangesWireContract`); 641 → 644.

## [1.16.2] - 2026-06-25

### Fixed — diagnose_host false `healthy` for long-running outages
ADR 069 (task 166). `_collect_diagnosis_inner` dropped any problem whose
*start* `clock` was older than `problem_hours` (24h default) — including
ones still **unresolved** — so a host with eight active Disasters, the
oldest ~3 days old, read `healthy` / 0 problems (found dogfooding against
`triage_slack_alert` + `get_active_problems` on the same host, same
instant). A days-long unresolved problem is more severe, not less. Fix:
new pure helper `_keep_active_or_recent` never ages out unresolved
problems, windowing only recently-resolved ones (distinguished by the now-
requested `r_eventid`); shared by `diagnose_host` / `bulk_diagnose` /
`diagnose_subnet`. `problem_hours` now bounds the recently-resolved set
(docstrings updated). Verdict change. +8 tests (incl. a wire-level
72h-old-Disaster regression); 633 → 641.

## [1.16.1] - 2026-06-25

### Fixed — `triage_slack_alert` crashed on every live call (`selectHosts`)
ADR 068. The tool's ground-truth step called `problem.get` with
`selectHosts`, which Zabbix 7.x rejects (`-32602: unexpected parameter
selectHosts` — only `event.get`/`trigger.get` support it). Every real
invocation failed; v1.16.0's 25 tests missed it because they only covered
the pure core, never the `client.call` wire path. Fix: drop `selectHosts`
from `problem.get` and map problem → host through the `trigger.get` call
already made for dependency collapse (now `selectHosts: ["hostid"]`), no
extra round-trips. Added `TestTriageWireContract` (recording fake client)
so the wire contract is covered. 633 → 636 tests.

## [1.16.0] - 2026-06-25

### Added — `triage_slack_alert` (new tool, 162 → 163)
ADR 067 (tasks 164/165). A read-only tool that turns one AI/Slack alert
line into an authoritative Zabbix verdict, born from dogfooding the
feed-to-MCP loop by hand. It parses the line, **resolves the named host**
to its Zabbix object (EXACT / FUZZY / AMBIGUOUS / NOT_FOUND — never
guesses, since alert names embed protocol/probe prefixes and domains live
in a Web-Check group), then **re-queries live problems** (the feed's
state is never trusted — it lags Zabbix in both directions) and
classifies per host: `real_now` / `recovered` / `symptom_of_cluster`,
with the host's current problems listed and a recommended action. Does
not acknowledge, suppress, rank, or remediate — not in `WRITE_TOOLS`.
Pure core extracted to `alert_triage.py`; +24 tests (`test_triage.py`).
608 → 632.

## [1.15.5] - 2026-06-23

### Security — clear four Dependabot CVEs (cryptography, starlette, pydantic-settings)
ADR 066. Four alerts landed at once, all transitive via `mcp`, cleared in
one `uv lock` re-resolve (lockfile-only):
- **cryptography 46.0.7 → 49.0.0** — GHSA-537c-gmf6-5ccf (High): vulnerable
  OpenSSL statically bundled in the project's PyPI wheels (fixed 48.0.1).
- **starlette 1.2.1 → 1.3.1** — CVE-2026-54283 (High): oversized urlencoded
  body → DoS (fixed 1.3.1); CVE-2026-54282 (Low): unvalidated path poisons
  `request.url.hostname` (fixed 1.3.0).
- **pydantic-settings 2.13.1 → 2.14.2** — GHSA-4xgf-cpjx-pc3j (Moderate):
  `NestedSecretsSettingsSource` follows symlinks out of `secrets_dir` (fixed
  2.14.2).
Reachability as before: starlette only under SSE/streamable-http;
pydantic-settings only with the `secrets_dir` loader (zbbx-mcp uses env
vars); none in the default stdio setup. No source change. 608 tests green.

## [1.15.4] - 2026-06-23

### Security — clear CVE-2026-48526 (PyJWT)
ADR 065. Dependabot flagged the transitive `pyjwt[crypto] == 2.12.1` pin
against CVE-2026-48526 (High) — a JWT algorithm-confusion flaw: a verifier
supporting both asymmetric and HMAC algorithms fails to reject a JSON Web
Key used as the HMAC secret, so a forged HS256 token signed with the
issuer's *public* JWK passes verification (affected `< 2.13.0`, fixed in
`2.13.0`). Re-resolved via `uv lock --upgrade-package pyjwt`, moving it
`2.12.1 → 2.13.0` (transitive via `mcp`'s OAuth support; no direct
dependency added). Only reachable under the SSE / streamable-http
transports' OAuth path — the default stdio deployment never verifies JWTs
— and High-complexity besides, but cleared regardless. Lockfile-only; no
source change. 608 tests green.

## [1.15.3] - 2026-06-18

### Security — clear CVE-2026-53539 (python-multipart)
ADR 064. Dependabot flagged the transitive `python-multipart == 0.0.29`
pin against CVE-2026-53539 (High) — a CPU denial-of-service: its
`QuerystringParser` locates form-field boundaries with an O(B²) scan
(whole-buffer search for `&`, then re-scan for `;`), so a body of
semicolons pins a CPU (affected `< 0.0.30`, fixed in `0.0.30`; the line
also covers the sibling CVE-2026-53538). Re-resolved via
`uv lock --upgrade-package python-multipart`, moving it `0.0.29 → 0.0.32`
(transitive via `mcp`; no direct dependency added). Only reachable under
the SSE / streamable-http transports — stdio never parses form bodies —
but cleared regardless. Lockfile-only; no source change. 608 tests green.

## [1.15.2] - 2026-06-18

### Docs — README accuracy sync
ADR 063. The README's hand-maintained counts had drifted and even
disagreed with each other (tool badge 161, tier-table `full` 156, prose
154 — real total 162). Synced everything to **computed** values
(`ALL_TOOLS` / `resolve_tier_disabled`): tool count 162; tiers core 27 /
ops 57 / finance 49 / reports 65 / full 162; added `get_problem_age_buckets`
and `rank_problem_cause` to the Problems row; refreshed the `initialize`
example to `serverInfo.name = "zabbix v1.15.1"`; added the `--version`
flag to the CLI table; requirements → Zabbix 6.2+ (tested on 7.4).
Docs-only; no code change.

## [1.15.1] - 2026-06-16

### Fixed — label sync now updates every container
ADR 062. `scripts/sync-mcp-label.py` re-keyed only the first `mcpServers`
container: `any(rename_in(c) for c in …)` over a generator short-circuits
once the first container changes, so with one zabbix entry per project
the rest stayed plain `zabbix`. Caught on first real use (2 containers,
1 renamed). Extracted `sync_config` that maps `rename_in` over a list
before reducing, so all containers are visited; verified live. +2 tests
(606 → 608).

## [1.15.0] - 2026-06-16

### Added — version visible in the `/mcp` dialog
ADR 061. ADR 038 put the version in `serverInfo.name`, but Claude Code's
`/mcp` dialog labels servers by their config *key*, not the reported
name — so the running version was invisible exactly where operators
check it (this fleet ran v1.13.0 while v1.14.0 was on `main`). Two parts,
reusing the slk-mcp ADR 024 pattern: (1) a `--version` flag
(`uv run zbbx-mcp --version`); (2) `scripts/sync-mcp-label.py`, which
finds the entry by command/args fragment, asks the wired invocation its
version (pyproject fallback), and renames the config key to
`zabbix v<version>` — idempotent, atomic, `.bak` backup, across all
`mcpServers` containers. Run after a release bump, then reconnect `/mcp`.
+18 tests (`test_sync_label.py`); 588 → 606.

## [1.14.0] - 2026-06-12

### Added — `rank_problem_cause` (new tool, 161 → 162)
ADR 060. `get_outage_clusters` finds correlated incidents but the
knowledge died inside the MCP response — Zabbix and every other consumer
still saw N independent problems. The new write tool marks events as
**symptoms of a cause** using Zabbix 6.4+ native event ranking
(`event.acknowledge` bit 256 + `cause_eventid`; `unrank=True` ranks back
via bit 128), so the correlation is written into Zabbix itself: the UI
nests the symptoms, and one incident replaces the cluster everywhere.
Registered in `WRITE_TOOLS` (disabled under `ZABBIX_READ_ONLY`). New pure
helper `_build_rank_action`; +3 tests (585 → 588).

## [1.13.4] - 2026-06-12

### Added — native problem snooze (the suppress write path)
ADR 059. ADR 044→052 made all seven problem-consuming tools *read*
suppression correctly, but nothing could *create* one short of a
maintenance window. `acknowledge_problem` and `bulk_acknowledge` now take
`suppress_hours` (N hours; `-1` = until the problem resolves) and
`unsuppress`, mapped to `event.acknowledge` bits 32/64 +
`suppress_until`. Because suppression is recorded in Zabbix itself, a
snoozed problem disappears from the Zabbix UI's default views, pauses
suppression-aware escalations, and drops out of every suppress-aware tool
here — then returns automatically when the timer lapses.
`include_suppressed=True` remains the audit lens. New pure helper
`_suppress_until_from_hours`; +7 tests (578 → 585).

## [1.13.3] - 2026-06-12

### Added — why-unclassified breakdown in `get_product_audit`
ADR 058. ~21% of the fleet classifies as Unknown/Unknown because host
groups carry names `ZABBIX_PRODUCT_MAP` doesn't map — but nothing ever
said *which* names. Auditing `product="Unknown"` now appends a "Why
unclassified" table: every unmapped group name with its Unknown-host
count, sorted by impact — literally the map entries to add. Explicit
skip-mappings are respected; group-less hosts counted under `(no
groups)`. New pure helper `classify.unmapped_group_counts`; additive
output only, no extra API calls. +4 tests (574 → 578).

## [1.13.2] - 2026-06-12

### Added — token-expiry early warning
ADR 057. `check_connection` now also inventories API tokens via
`token.get` and warns when any enabled token expires within 30 days
(soonest-first, with "EXPIRED Nd ago" for lapsed ones). An expired token
kills every authenticated tool at once — the same failure shape the 7.2
upgrade just demonstrated — and this catches it weeks ahead from the tool
an operator naturally runs first. Degrades silently when `token.get` is
unavailable or denied. New pure helper `summarize_token_expiry`; +4 tests
(570 → 574).

## [1.13.1] - 2026-06-12

### Fixed — `get_proxies` never called a real API method
ADR 056. The tool called `relay.get` with a `relayid` output field —
neither exists in any Zabbix version (an over-eager find/replace
artifact), so the tool errored on every invocation since it was written.
Rewritten against the real `proxy.get` with the Zabbix 7.0 proxy object
(`name`, `operating_mode`), and now also surfaces `version` +
`compatibility` — proxies running outdated (⚠) or unsupported (✗)
versions relative to the server are flagged, which is exactly the check
an operator wants after a server upgrade. +4 pure-helper tests
(`TestFormatProxyCompat`); 566 → 570.

## [1.13.0] - 2026-06-12

### Fixed — Zabbix 7.2+ API compatibility
ADR 055. The monitored instance was upgraded 6.4 → 7.4.9, which broke the
server on two backward-incompatible JSON-RPC changes from 7.2: (1) the
`auth` request-body property was removed — every authenticated call failed
with `unexpected parameter "auth"` (only `apiinfo.version` kept working);
(2) `host.get`/`trigger.get` dropped `selectGroups` (returned `groups` →
`hostgroups`), which the tool layer uses in ~76/~82 places for host-group
classification. Both are fixed at the client boundary: authentication now
uses the `Authorization: Bearer` header, and the client transparently
translates `selectGroups` ↔ `selectHostGroups` and aliases `hostgroups`
back to `groups`. No call-site or tool-signature changes; the client now
spans Zabbix 6.2–7.x. Other 7.0/7.2/7.4 removals were checked and are
unused here. +5 wire-format tests (`test_client.py`); 561 → 566.

## [1.12.7] - 2026-06-09

### Security — clear CVE-2026-48710 (starlette)
ADR 054. GitHub Dependabot flagged the transitive `starlette == 1.0.0`
pin against CVE-2026-48710 (CVSS 6.5, moderate) — an HTTP request-
smuggling flaw where the `Host` header was used to reconstruct
`request.url` without validation, allowing security middleware to be
bypassed (affected 0.8.3–1.0.0, fixed 1.0.1). Re-resolved via
`uv lock --upgrade-package starlette`, moving starlette `1.0.0 → 1.2.1`
(transitive via `mcp` / `sse-starlette`; no direct dependency added).
Lockfile-only — no source or API change. 561 tests green on the new
Starlette.

## [1.12.6] - 2026-06-05

### Fixed — false RTT drift against a degraded baseline
ADR 053. `compute_loss_drift` (behind `detect_loss_drift`) flagged `rtt-up`
when a host's recent RTT climbed above its 14-day baseline — but a baseline
measured during an outage (heavy packet loss) has an unreliable RTT, so a
host that has since *recovered* (e.g. baseline 47% loss / 76 ms → recent
0.09% loss / 142 ms) read as drift when it was actually returning to
normal. The RTT-drift branch is now skipped when baseline loss ≥ 20%
(`_BASELINE_LOSS_MAX`); loss-based detection is unaffected. Mirrors
zabbix-reports `_classify_loss_drift`. Pure-helper change, no API surface.
Tests: +1 (560 → 561).

## [1.12.5] - 2026-06-04

### Added — complete maintenance-suppress coverage
ADR 052. The suppress filter from ADR 044 (`filter_suppressed`) was wired
into four problem-surfacing tools but three others that also call
`problem.get` were left out — so a host inside a maintenance window read
its planned downtime as live problems. This closes the gap: the
diagnosis path (`diagnose_host` / `bulk_diagnose` / `diagnose_subnet` via
`_collect_diagnosis_inner`), `get_recent_changes`, and
`send_slack_report` now drop maintenance-suppressed problems by default.
Each gains an `include_suppressed: bool = False` flag to restore full
visibility. No-op today (no maintenance windows configured); structural —
suppressed problems now drop out of all seven problem-consuming tools
uniformly. Tests: +3 (`TestDiagnoseSuppressThreading`).

## [1.12.4] - 2026-06-04

### Added — acute mode for `detect_regional_anomalies`
ADR 047 put the regional detector on the classifier at a daily grain —
diurnal-safe, but it can't catch an *immediate* regional block (one that
started in the last few hours is diluted in today's daily average). New
opt-in `acute=True` mode adds the deeper treatment: it sums each
country's hourly traffic into a country-aggregate series and judges it
against the country's **same-hour-of-day seasonal band** via
`classify_drop`, flagging acute / sustained regional blocks immediately.

Default stays `acute=False` (the daily roll-up), so existing behaviour
and volume are unchanged. The acute path fetches one main interface per
host (bounded, like `detect_traffic_drops`). New pure helper
`anomaly.aggregate_hourly_by_country`. See ADR 051.

### Tooling
- 552 tests → 557 (+5 for `aggregate_hourly_by_country`).

## [1.12.3] - 2026-06-04

### Added — dependency collapse in `get_host_floods`
Completes ADR 048 (the ticket named both tools). `get_host_floods` now
collapses symptom problems whose trigger depends on another firing
trigger **before** the per-host count, reusing
`collapse_dependent_problems`. This is the right interaction with the
flood threshold: a host with 5 problems that are 1 root + 4 declared
symptoms now counts as 1 real problem and no longer falsely trips a
flood. New `collapse_dependent: bool = True` arg; no-op where no trigger
dependencies are configured. See ADR 050.

## [1.12.2] - 2026-06-04

### Fixed — diagnosis read agent/traffic from the parent only (missed VIP traffic)
ADR 046 merged sub-host *problems* onto the rep, but `diagnose_host` /
`bulk_diagnose` still read agent-ping and traffic items from the
representative record alone. On a multi-VIP box, traffic lives on the
sub-host VIP interfaces — so the diagnosis reported "No traffic items"
and could not assess `traffic_lost` on exactly the boxes most likely to
be multi-VIP. (Observed live: a parent host whose VIPs carried the load
diagnosed with no traffic data.)

Now both paths fetch items across **every** hostid in the canonical
group: traffic sums across the box's VIP interfaces, and agent
reachability uses the freshest `agent.ping` across the group (a stale
sub-host record can't override the parent's live agent — new
`_freshest_agent_ping` helper). `bulk_diagnose` fetches group-wide items
in its existing batch and maps them back per box, so no extra round-trip
per host. Closes the recurring "traffic lives on the VIPs" gap noted in
ADR 036/039/046. See ADR 049.

### Tooling
- 548 tests → 552 (+4 for `_freshest_agent_ping`).

## [1.12.1] - 2026-06-04

### Added — trigger dependency collapse (root-cause-only) in `get_active_problems`
Zabbix lets a trigger declare it depends on another — when a service
check depends on "agent unreachable", an agent-down event fires both,
and the dependent one is symptomatic noise. `get_active_problems` now
collapses those: it fetches `trigger.get` with `selectDependencies` for
the firing triggers and drops any problem whose trigger depends on
another currently-firing trigger, leaving the root cause. New
`collapse_dependent: bool = True` arg; the header notes how many
symptoms were collapsed.

New pure helper `data.collapse_dependent_problems(problems, dep_map,
collapse)`. No-op where no trigger dependencies are configured (the
monitored instance currently has none), so zero behaviour change today —
pure noise reduction for environments that wire dependencies. See
ADR 048.

### Tooling
- 542 tests → 548 (+6 for `collapse_dependent_problems`).

## [1.12.0] - 2026-06-04

### Changed — `detect_regional_anomalies` on the false-positive-resistant classifier
The regional detector judged each host by `(avg − current) / avg` — the
same instantaneous-spot-reading-vs-average comparison that produced the
diurnal false positives `detect_traffic_drops` was rebuilt to eliminate
(ADR 040). On a normal nightly trough it flagged "N countries affected"
that were fine.

Now each host is judged by `anomaly.classify_drop`, fed a recent-**days**
average vs a baseline-days average via the new
`recent_baseline_from_daily` helper. Daily aggregates are inherently
diurnal-safe (a full day's mean can't show a nightly trough), and the
classifier's floor + threshold + host-down rule-out (via service status)
apply. The per-country roll-up (≥ `country_threshold` % of a country's
hosts affected) and the `min_avg_mbps` micro-market gate are unchanged.

The grain is daily, not hourly: this detector has no hourly series for a
same-hour seasonal floor, so `seasonal_floor_value` is None here (the
daily aggregation provides the diurnal safety instead). See ADR 047.

### Tooling
- 536 tests → 542 (+6 for `recent_baseline_from_daily`).

## [1.11.2] - 2026-06-04

### Fixed — diagnosis missed sub-host (VIP) problems
`diagnose_host` / `bulk_diagnose` queried `problem.get` for the
representative (parent) hostid only. On a multi-VIP physical machine a
problem firing on a sub-host VIP was invisible to the verdict, so a box
with a real per-VIP problem could read `healthy` — a false-negative,
the dangerous direction.

Now the diagnosis queries problems across **every** hostid in the
canonical group:
- `_collect_diagnosis_inner` gains `group_hostids` (defaults to the rep
  alone, so single hosts are unchanged);
- `_dedupe_records_by_canonical` attaches `_group_hostids` to each rep,
  threaded through `_run_bulk_diagnosis`;
- `diagnose_host` fetches the canonical group's VIPs and passes their
  hostids.

The verdict's open-problem count now reflects the whole box. See ADR 046.

### Tooling
- 535 tests → 536 (+2 for `_group_hostids`, −1 reshaped).

## [1.11.1] - 2026-06-04

### Fixed — `generate_service_brief` per-country counters double-counted VIPs
The per-country ok/partial/down/total tallies iterated raw Zabbix hosts,
so a multi-VIP physical machine counted once per VIP — inflating the
marketing-facing service-quality numbers (ADR 034/036 left these
internal counters for later). Now folds sub-hosts to canonical groups:
one physical machine = one count, traffic SUMs across the box's VIPs,
and service checks merge across them worst-wins (a single failing VIP
check pulls the box below "ok"). New pure helper
`_classify_country_group(group_mbps, merged_checks)`. See ADR 045.

### Tooling
- 529 tests → 535 (+6 for `_classify_country_group`).

## [1.11.0] - 2026-06-04

### Added — maintenance-suppress filtering (`include_suppressed`)
Zabbix marks a problem `suppressed` when its host is inside an active
maintenance window — planned downtime, not an incident. The problem-
surfacing tools counted them anyway, so the moment ops configures a
maintenance window every report would flag planned downtime as an
outage. (Latent today — no windows configured — hence shipped as
insurance before it bites.)

New pure helper `data.filter_suppressed(problems, include_suppressed)`
drops `suppressed == "1"` rows unless asked to keep them (client-side
and version-agnostic, since the `problem.get` `suppressed` param
semantics shifted across Zabbix versions). Wired into the four incident-
surfacing tools, each gaining `include_suppressed: bool = False`:
`get_active_problems`, `get_problems`, `get_host_floods`,
`get_outage_clusters`. Each now requests the `suppressed` field and
applies the filter. Default excludes — zero behaviour change while no
maintenance windows exist. See ADR 044.

### Tooling
- 524 tests → 529 (+5 for `filter_suppressed`).

## [1.10.4] - 2026-06-04

### Fixed — `get_idle_relays` flagged healthy NAT-mode relays
The idle-relay check looked at `net.if.in` only and flagged "physical
NIC busy + tunnel interfaces at 0 bps" as a forwarding failure. That is
the normal signature of a NAT-mode relay — it forwards through the
physical NIC with its tunnel interfaces idle by design — so the tool
returned healthy relays as failures (busiest first, since sorted by
throughput). The docstring hedged this but nothing gated on it.

Fix: also fetch `net.if.out` and gate on the physical out/in ratio —
flag only when the physical NIC receives (>= min) but sends < 10% of
that (traffic arriving, not relayed) with all tunnels at 0. Healthy
forwarders (out ~= in) are excluded. `_split_iface_metrics` now buckets
both directions; `_find_idle_relays` returns in+out kbps; output shows
both, and an empty result returns a "no forwarding failures" note.
Mirrors the same fix in the report consumer. See ADR 043.

### Tooling
- 523 tests → 524 (+1: a balanced-throughput relay is not flagged).

## [1.10.3] - 2026-06-01

### Added — CPU/connection corroboration in `detect_traffic_drops`
ADR 040 shipped the classifier *accepting* `cpu_ratio` / `conn_ratio`
but the tool passed only `agent_reachable`, so a coordinated regional
*demand* trough (traffic down, but users/CPU down with it) still
classified as `blocked` — it had no signal to tell a block (host still
serving, connections/CPU hold up while bytes collapse) from low demand
(everything falls together).

Now a bounded second pass corroborates: for the handful of candidates
that pass the seasonal gate (not the whole fleet), it fetches CPU and
connection trends, computes recent/baseline ratios, and re-classifies.
Candidates whose connections/CPU fell with traffic flip to `low_demand`
and drop out of the block list. Connections are the strong signal (they
track users directly); CPU is a weak fallback (fixed OS/overhead floor
that doesn't scale with traffic). Cost stays bounded — corroboration
trends are fetched only for candidates, never fleet-wide.

New pure helper `anomaly.metric_recent_baseline_ratio(records,
recent_start, invert_pct=...)` computes the recent/baseline ratio, with
`invert_pct` converting an idle-percentage metric (`cpu.util[,idle]`)
to its used complement before the ratio. See ADR 042.

### Tooling
- 517 tests → 523 (+6 for `metric_recent_baseline_ratio`, pinning the
  idle→used inversion).

## [1.10.2] - 2026-06-01

### Fixed — `get_predictive_alerts` rendered HIGH tier as INFO
The four-tier severity classifier (CRITICAL / HIGH / WARNING / INFO)
wrote the correct tier into each alert, but the markdown render layer
still assumed the legacy three tiers: the table-cell mapping collapsed
anything not CRITICAL/WARNING to INFO (so every HIGH alert showed as
the lowest tier), and the summary counted only CRITICAL and WARNING
(so HIGH was omitted entirely). Net effect was a false-*negative* — a
near-term risk one step below the top displayed as most-benign and was
missing from the call-to-action summary. Fix renders the canonical
`severity` field directly and adds a HIGH summary line. Presentation
only; classifier unchanged. See ADR 041.

### Tooling
- Lockfile `uv.lock` synced to the current version.

## [1.10.1] - 2026-05-29

### Fixed — `detect_traffic_drops` 500 on fleet-wide runs
v1.10.0 fetched trends for *every* traffic interface; a host has one
real uplink plus many idle `svc`/`tun`/`ppp` interfaces, so a
fleet-wide `trend.get` (hundreds of hosts × dozens of interfaces ×
7 days) overran the Zabbix API and returned HTTP 500. Region- or
group-scoped runs worked; the unfiltered run failed.

Fix: shortlist the top `_IFACE_CANDIDATES` (3) interfaces per host
**by current value** before the trend fetch, bounding it to ~3
items/host (same order as pre-1.10.0). An always-idle interface
never makes the shortlist, so the dead-interface false positive is
still avoided; baseline-weighted selection (P4) then runs among the
shortlist. Classifier logic unchanged.

## [1.10.0] - 2026-05-29

### Changed — `detect_traffic_drops` rebuilt to suppress false positives
The old detector compared an instantaneous spot reading against the
N-day average, so any normal diurnal trough read as an 80–96% "drop."
Replaced with a layered classifier (new `zbbx_mcp.anomaly` module) that
distinguishes real blocking — **including immediate/acute blocking
detected on the current bucket** — from diurnal troughs and demand shifts.

New `anomaly.py` pure helpers (24 unit tests):
- `classify_drop(...)` → `DropVerdict(state, confidence, drop_pct, reasons)`
  with states `healthy` / `low_demand` / `blocked_acute` /
  `blocked_sustained` / `artifact` / `unknown`.
- `seasonal_floor(hourly, hour_of_day)` — same-hour-of-day percentile
  band, so a normal nightly trough isn't a "drop" and a genuine drop is
  flagged immediately (below-band-now == anomalous-now).
- `pick_traffic_interface(interfaces)` — selects the highest-*baseline*
  interface (not highest-current), so an idle tunnel reading near zero
  can't fabricate a drop on a box whose primary uplink is flowing.
- `percentile(values, pct)` — nearest-rank, for small seasonal buckets.

`detect_traffic_drops` now:
- compares a recent-window **average** (`recent_hours`, default 6) to the
  baseline, never an instantaneous `lastvalue`;
- judges against the seasonal band (`seasonal=True` by default);
- escalates acute → sustained on persistence (does not gate detection);
- fetches `agent.ping` to rule out host-down (corroboration);
- selects the interface by baseline;
- raised `min_baseline_mbps` default 1.0 → 5.0 (denominator floor);
- output now reports per-row state + confidence + reason, and separates
  "low-demand not blocked" from real blocks.

### Behaviour / compat
- Output format changed: columns are now
  `Server | Provider | State | Conf | Recent → Baseline | Drop | Why`.
- New params `recent_hours` and `seasonal`; existing params unchanged.
- See ADR 040.

### Tooling
- 493 tests → 517 (+24 in `test_anomaly.py`).

## [1.9.6] - 2026-05-28

### Fixed — Pre-fold input list in `bulk_diagnose` / `diagnose_subnet`
- Both tools shared `_run_bulk_diagnosis`, which ran
  `_collect_diagnosis_inner` once per resolved Zabbix record.
  Multi-record physical machines therefore surfaced as N
  near-identical rows in the output table — same problem as
  ADRs 032–037 but on the *input* side rather than the per-host
  aggregator side.
- Fix: new pure helper `_dedupe_records_by_canonical()` collapses
  the input list to one record per canonical (physical) machine
  before the fan-out. Representative selection prefers the parent
  (host name with no space); falls back to the first sub-host
  when the parent isn't in the resolved set. Returns a parallel
  `sub_counts` map so each kept record knows how many sub-host
  records were collapsed into it.
- Rendering: each result row's `host` field is annotated
  `parent (+N sub)` when the canonical group covered more than
  one Zabbix record. Standalone hosts pass through unchanged.
- The table header still reports the *original* (pre-dedup)
  count for the "M of N host(s)" line, so operators can see at a
  glance when the fold compressed many records.
- See ADR 039.

### Tooling
- 488 tests → 493 (+5 new pure-helper tests for
  `_dedupe_records_by_canonical`: pass-through, full parent +
  sub fold, sub-host-only set falls back to first, mixed
  standalone + groups, empty input).

## [1.9.5] - 2026-05-28

### Changed — Server name now carries the package version
- `FastMCP(...)` is constructed with `f"zabbix v{__version__}"`
  instead of the bare `"zabbix"`. The string lands in the MCP
  `initialize` response under `serverInfo.name`, and Claude Code's
  `/mcp` UI renders that field next to the connection status.
  After a server restart the panel reads `zabbix v1.9.5  ✓ connected`
  instead of just `zabbix  ✓ connected`.
- `zbbx_mcp.__version__` now resolves at import time via
  `importlib.metadata.version("zbbx-mcp")` instead of the
  hard-coded stale `"1.6.0"` string — auto-syncs with
  `pyproject.toml`. Falls back to `0.0.0+unknown` when the dist
  isn't installed (editable / source-tree usage).
- Existing MCP clients that compare `serverInfo.name` to a literal
  `"zabbix"` will need to switch to `startswith("zabbix")` (the
  `test_initialize` smoke was updated the same way).
- See ADR 038.

## [1.9.4] - 2026-05-27

### Fixed — Parent / sub-host fold in `get_shutdown_candidates`
- `get_shutdown_candidates` now pre-folds sub-hosts into canonical
  groups before classification. The previous per-Zabbix-host loop
  could surface one multi-record physical machine as N separate
  DEAD / ZOMBIE / BROKEN / IDLE candidates, **and** count its
  sub-hosts as N peers in the cohort headroom math — inflating
  both the candidate count and the apparent peer capacity.
- Aggregation rules (mirroring ADR 032 conventions):
  - `cpu_avg` = **MAX** across the group (worst-case CPU)
  - `traffic_avg` = **SUM** across the group (each VIP has its
    own interface)
  - `service` = **WORST** across the group (DOWN > PARTIAL > OK)
- The peer-headroom cohorts are also built from canonical groups
  so capacity reflects physical machines. Cohort traffic peak +
  avg also SUM across sub-hosts.
- Display: candidate rows annotate `parent (+N sub)` when the
  group has sub-hosts.
- See ADR 037.

### Tooling
- 482 tests → 488 (+6 new metric-aggregation sanity tests:
  CPU=MAX, traffic=SUM, service=WORST; the all-idle and
  busy-sub-host-rescues-parent bug cases).

## [1.9.3] - 2026-05-27

### Fixed — Parent / sub-host fold in inventory + traffic tools
- Seven more per-host aggregators now collapse sub-host records to
  one canonical row each. Same bug shape ADRs 032 / 033 / 034
  addressed for the cost, outage-cluster, and service-check
  surfaces.
- Tools refactored (each with the worst-wins sort that fits its
  semantic):
  - `get_high_cpu_servers` — highest CPU per canonical wins.
  - `get_underloaded_servers` — lowest CPU per canonical wins.
  - `get_low_disk_servers` — highest disk% per canonical wins.
    Now fetches hostnames for **all** flagged hosts (not just top
    N) so the fold runs before the truncate.
  - `get_low_memory_servers` — lowest free memory per canonical
    wins. Same upfront-fetch change.
  - `get_stale_servers` — oldest last-data per canonical wins.
  - `detect_traffic_drops` — biggest drop % per canonical wins
    (via `fold_rows_by_canonical_host`).
  - `get_traffic_report` — different semantics: traffic and
    connections **SUM** across sub-hosts (each VIP has its own
    interface and session counter); `bw_per_client` is recomputed
    from the summed totals.
- See ADR 036.

### Tooling
- 479 tests → 482 (+3 new pattern-sanity tests for the inline
  fold loops: tuple worst-wins, hostid indirection with host_map
  lookup, traffic-report SUM fold).

## [1.9.2] - 2026-05-27

### Fixed — `generate_full_report` crash on save (Sentry dc717f4d)
- `excel.py` used a lazy-init pattern: the module-level fill
  constants (`HEADER_FILL`, `RED_FILL`, …) were `None` at import
  time and only rebound inside `_init_openpyxl()`. Consumers doing
  `from zbbx_mcp.excel import RED_FILL` at *their* module level
  captured the `None` binding — the later rebind never propagated.
- `full_report.py` was the one consumer using that import shape;
  the others import openpyxl lazily inside functions and so always
  saw a freshly-constructed fill.
- Symptom: every `generate_full_report` call raised
  `TypeError: expected <class 'openpyxl.styles.fills.Fill'>` from
  `wb.save()` because the cell `.fill` descriptor received `None`.
- Fix: removed `_init_openpyxl()`; module-level fills are now
  constructed eagerly (openpyxl is a hard dependency anyway, so
  the lazy-import saving was illusory). The other style-using
  tools are unaffected.
- See ADR 035.

### Tooling
- 476 tests → 479 (+3 new regression tests for the Fill
  descriptor: module-level fills are PatternFill instances,
  a workbook using each fill saves cleanly,
  `full_report`'s module-level imports resolve to PatternFill).

## [1.9.1] - 2026-05-26

### Fixed — Parent / sub-host fold in service-check tools
- Four tools that count "failing servers" from service-check items
  were summing one row per Zabbix host. Multi-record physical
  machines therefore inflated the count, the same shape that
  ADR 032 fixed for cost tools and ADR 033 fixed for outage
  clusters.
- New shared helpers in `data.py`:
  - `canonical_host_name(name)` — promoted from `correlation.py`
    to be the single primitive used by every per-host fold.
  - `fold_rows_by_canonical_host(rows, name_key, sort_key)` —
    dedupes a row list by canonical name, keeps first / sorted-
    first occurrence, annotates `sub_count`.
- Tools refactored to use canonical fold at the main count site:
  - `generate_service_brief` — per-check failing-server lists
    collapse sub-hosts; "Servers Failing" totals reflect physical
    machines.
  - `detect_regional_anomalies` — anomaly table sorted worst
    severity first, then folded to canonical (worst sub-host
    wins).
  - `get_service_uptime_report` — per-host rows sorted by
    primary-check uptime ascending, then folded (lowest uptime
    sub-host wins).
  - `get_service_health_matrix` — per-country counts now iterate
    canonical groups; a group is "up" for a check only when every
    sub-host is up (or any sub-host is traffic-validated).
- See ADR 034.

### Tooling
- 471 tests → 476 (+5 new for `fold_rows_by_canonical_host`:
  pass-through, sub-host collapse with first-occurrence kept,
  sort-key picks worst, mixed standalone/sub, alternate name key).

## [1.9.0] - 2026-05-26

### Fixed — Outage-cluster dedupe by canonical host name
- `get_outage_clusters` previously counted Zabbix sub-hosts of one
  physical machine as separate "distinct hosts" when checking the
  `min_hosts` threshold. A multi-VIP box throwing one problem on
  each VIP could therefore satisfy a 3-host cluster gate while
  actually being a single machine misbehaving — exactly the
  false-positive shape ADR 032 fixed for cost tools.
- Fix: new pure helper `_canonical_host_name()` in `correlation.py`
  strips the `" <suffix>"` tail. `_cluster_problems()` now uses
  canonical names in the `uniq_hosts` set and the `hosts` output
  field, so the threshold check and the displayed cluster size
  both reflect physical machines.
- `get_host_floods` already canonicalised via `build_parent_map`;
  this brings outage clusters to the same standard. See ADR 033.

### Tooling
- 471 tests (+6 new for `_cluster_problems` canonical fold:
  parent + sub-hosts below threshold, distinct hosts still cluster,
  mixed parents/subs counted correctly, sub-hosts only also fold,
  canonical-name helper pass-through and strip).

## [1.8.9] - 2026-05-26

### Fixed — Parent / sub-host double-count in cost tools
- New shared helper `canonical_host_groups()` in `data.py` collapses
  parent + sub-host Zabbix records into one canonical group per
  physical machine. Aggregation rules:
  - **cost = MAX** across the group (sub-host `{$COST_MONTH}` macros
    typically duplicate the parent's bill — summing inflated spend).
  - **traffic = SUM** across the group (each VIP has its own
    interface).
  - **cpu = MAX** across the group (worst-case across VIPs).
- Three cost tools now iterate canonical groups instead of raw
  hosts:
  - `get_cost_efficiency` — the "Waste" list, by-country, and
    by-provider tables no longer multiply per-VIP. Waste rows
    annotate sub-host count: `parent (+N sub)`.
  - `get_cost_summary` — server counts in by-product and by-provider
    tables now reflect physical machines.
  - `get_cost_gaps` — "M without cost" counts physical machines, not
    individual sub-host records.
- See ADR 032.

### Deferred (queued for v1.9.0)
- `get_shutdown_candidates` — two-pipeline (candidates + cohorts)
  plus three metrics (cpu/traffic/service); fold takes a separate
  pass.
- `bulk_diagnose` / `diagnose_subnet` — sub-host rows currently
  dilute the table.
- `detect_traffic_drops` / `detect_traffic_anomalies` /
  `get_traffic_report` — drop counts inflate by sub-host count.
- `get_high_cpu_servers` / `get_underloaded_servers` /
  `get_low_disk_servers` / `get_low_memory_servers` /
  `get_stale_servers` — current inheritance pattern is correct but
  rows still over-count sub-hosts.

### Tooling
- 465 tests (+9 new for `canonical_host_groups`: standalone, parent
  + sub-fold, cost=MAX, traffic=SUM, cpu=MAX, cost=None when
  unpriced, mixed standalone/sub, orphan sub-host, malformed values
  ignored).

## [1.8.8] - 2026-05-26

### Security
- **Bumped three transitive dependencies past CVE-required minimums**
  via `uv lock --upgrade-package`:
  - `python-multipart` 0.0.26 → 0.0.29 (CVE-2026-42561, High)
  - `urllib3` 2.6.3 → 2.7.0 (CVE-2026-44432, CVE-2026-44431, High)
  - `idna` 3.11 → 3.16 (CVE-2026-45409, Moderate)
- Lockfile-only change; no source edits, no API change. See
  ADR 031.

## [1.8.7] - 2026-05-26

### Added — `redact_partial` flag on `get_cost_summary`
- New optional `redact_partial: bool = False` arg. When True, drops
  per-product and per-provider rows where some servers in the group
  have no `{$COST_MONTH}` macro set, recomputes the grand total from
  the kept rows, suppresses the "Servers with cost / Without" line,
  and appends a footer marking the output as filtered. Intended for
  externally-shared artifacts (board decks, partner readouts) where
  partial-coverage metadata is a finding about process maturity
  rather than the metric the audience wants. Default is unchanged:
  internal callers see the full breakdown.
- Renderer extracted into a pure helper `_render_cost_summary` for
  testability. See ADR 030.

### Tooling
- 456 tests (+8 new for `_render_cost_summary` covering: default
  preserves full output, redact drops partial product/provider rows,
  recomputes grand total, suppresses the "Without" line, appends
  footer, handles all-partial edge case, defensive keep-on-missing
  for keys absent from the totals map).

## [1.8.6] - 2026-05-21

### Fixed
- **`bulk_diagnose(country=...)` returned a small random sample.** The
  Python-side country filter ran *after* the Zabbix API's
  `limit: max_hosts + 1` truncation, so the country filter narrowed
  an already-truncated sample rather than the full fleet. Fix: when
  `country` is set, skip the API `limit` and request `selectInventory`
  so `resolve_country()` sees both hostname and inventory signals;
  then truncate to `max_hosts` at the end. The `hosts=` / `group=`
  paths are unaffected (their filters apply server-side already).

## [1.8.5] - 2026-05-21

### Added — Tag-based filtering across detection tools
- New shared module `zbbx_mcp.tag_filter` exposing
  `parse_tag_filter(spec) -> list[dict]`. Operators pass tags as
  `"key:value,key2:value2"` (AND-combined); bare key means
  "exists" check. Parser tolerates whitespace, empty pairs,
  trailing commas.
- `search_hosts`, `get_problems`, `get_active_problems`, and
  `get_triggers` all gain a new optional `tags: str = ""` arg that
  pipes the parsed filter into the Zabbix `host.get` / `problem.get` /
  `trigger.get` payload. Tools without tag plumbing yet can be
  extended the same way (one-line import + payload merge).

### Added — Dependency surfacing in `get_triggers`
- New optional `with_dependencies: bool = False` arg surfaces each
  trigger's `selectDependencies` list. Lets operators spot
  dependent triggers that are masked by a parent firing. Zero
  behaviour change when deps are not configured.

### Added — Native anomaly-trigger surfacing (Zabbix 6.4)
- **`get_anomaly_triggers(only_active=True)`** — lists triggers
  whose expression references Zabbix 6.4's built-in time-series
  functions (`anomalystl`, `baselinewma`, `baselinedev`,
  `trendstl`, `forecast`). Complements the MCP's client-side
  detectors (`detect_loss_drift`, `detect_disruption_wave`) by
  exposing what server-side anomaly alerting is already configured.
  Lands in the `ops` tier. See ADR 029.

### Tooling
- 161 tools across 55 modules.
- 447 tests (+8 new for `parse_tag_filter`).

## [1.8.4] - 2026-05-21

### Added — Bulk diagnostic composition
- **`bulk_diagnose(hosts="", group="", country="")`** — runs the
  `diagnose_host` pipeline across a target set and returns a compact
  table (one row per host: verdict, mode, primary signal, action).
  Supports three filter axes that compose: explicit host list,
  host-group name, or country (ISO-2 / ISO-3 / English name).
  Bounded concurrency (semaphore=10), capped at 50 hosts per call.
  Output rows are sorted by verdict severity. Lands in the `ops`
  tier.
- **`diagnose_subnet(subnet)`** — follow-on to `get_outage_clusters`:
  when a cluster row reports "5 hosts on 1.2.3.0/24", paste that
  CIDR in here to get a verdict for each host. Accepts /24, /16, or
  dotted prefix forms. Internally resolves to a host list and reuses
  the bulk pipeline. Lands in the `ops` tier.

### Changed — Internal refactor
- `diagnose.py` factored into a shared async data-gather helper
  (`_collect_diagnosis_inner`) and a shared bulk runner
  (`_run_bulk_diagnosis`). Both new tools and the existing
  `diagnose_host` share these helpers. No behaviour change for
  `diagnose_host`; the rotation-history step is now skipped on
  bulk calls (set `rotation_days=0`) to keep fan-out responsive.

### Tooling
- 160 tools across 55 modules.
- 439 tests (+18 new for `_verdict_primary_signal`,
  `_render_bulk_table`, `_ip_matches_subnet` pure helpers).

## [1.8.3] - 2026-05-21

### Added — Zabbix-version introspection
- **`get_zabbix_version`** — wraps `apiinfo.version` and surfaces a
  feature-availability matrix derived from the parsed version.
  Operators (and the LLM client) can see at a glance which optional
  APIs the connected server supports: API token API (5.4+),
  unacknowledge / severity-change actions (6.0+), suppress /
  unsuppress (5.2+), cause/symptom rank actions (6.4+), connector
  API / proxy groups / HA cluster (7.0+). Lands in the `core` tier.
  See ADR 027.

### Changed — Enhanced acknowledge actions
- **`acknowledge_problem`** and **`bulk_acknowledge`** now accept
  two new optional params:
  - `severity: int = -1` — change the problem severity (0-5) in the
    same call. Maps to Zabbix `event.acknowledge` action bit 8.
  - `unack: bool = False` — unacknowledge instead of acknowledge.
    Maps to action bit 16 (mutually exclusive with the ack bit).
  Existing callers are unaffected; the new params default to no-op.
  The action-bitmask computation is now a pure helper
  (`_build_ack_action`) with 8 dedicated unit tests.

### Tooling
- 158 tools across 55 modules.
- 421 tests (16 new for pure-helpers: `_build_ack_action` +
  `_parse_zabbix_version` + `_feature_matrix`).

## [1.8.2] - 2026-05-21

### Added — Composite diagnostic
- **`diagnose_host(host)`** — one MCP call composes host.get +
  item.get + trend.get + problem.get + auditlog.get into a unified
  per-host report with verdict + recommended action. Auto-detects
  server-mode hosts (with agent / traffic items) vs domain-mode
  hosts (HTTPS-check only). Replaces the multi-tool chain operators
  ran by hand for every "is this host healthy?" question. Lands in
  the `core` tier. See ADR 026.

### Changed — Tier re-cut (evidence-based)
- 16 days of `get_telemetry_summary` data drove a data-driven re-cut
  of the tier composition (ADR 025). 12 tools in the original
  `core` tier had zero calls in the window:
  - 9 demoted to `full`-only: `get_templates`, `get_graphs`,
    `get_maintenance`, `get_services`, `get_global_macros`,
    `get_users`, `get_proxies`, `get_maps`, `get_map_detail`.
  - 3 demoted to thematic tiers: `acknowledge_problem` and
    `get_alerts` → `ops`; `get_sla` → `reports`.
- Handshake reductions (compact mode on):
  - `core`     5k → 4k tokens (-20%)
  - `ops`      11k → 9k       (-18%)
  - `finance`  10k → 7k       (-30%)
  - `reports`  13k → 10k      (-23%)
  - `full`     unchanged at 25k

### Tooling
- 157 tools across 55 modules.
- 405 tests (12 new for `diagnose_host` pure helpers).

## [1.8.1] - 2026-05-05

### Changed — Public-repo hygiene
- **`REGION_MAP` and `CAPITAL_COORDS` expanded to full ISO 3166-1
  coverage.** Both tables previously held curated subsets (65 and 49
  countries respectively); the inclusion list was a soft hint at
  market footprint. Now every ISO 3166-1 country is present in both
  tables (200 entries each). `get_latency_estimate` works for any
  source country, not just the previously-curated subset.
  - REGION_MAP grouping unchanged in spirit (NA, LATAM, EMEA, APAC,
    CIS); Central Asia / Caucasus countries appear in two regions
    where the geography genuinely overlaps.
  - CAPITAL_COORDS adds capital lat / lon for every country not
    previously listed.
- **Hostname-pattern placeholder cleanup.** A comment in
  `costs_import.py` used a `prem-*` example matching a banned
  hostname-pattern (the operator's actual fleet prefix); replaced
  with generic `parent-a a-b`.

No tool added or removed; tool count and behaviour unchanged. 393
tests pass; ruff + mypy + sensitive scan all clean.

## [1.8.0] - 2026-05-05

### Added — Self-introspection
- `get_telemetry_summary`: reads the analytics log written by the existing
  `logged()` decorator and reports per-tool call counts, error rate,
  average + max latency, and average response size. Args: `hours`
  (lookback window, 0 = all-time), `top`, `log_path`. Pure helper
  `_summarise_records` covered by unit tests; handles both epoch and
  ISO 8601 timestamps. Lands in the `core` tier so introspection
  works in every session. See ADR 024.

### Changed — Code organisation (no behaviour change)
- `data.py` split: country-specific reference data
  (`REGION_MAP`, `CAPITAL_COORDS`, `_COUNTRY_NAMES` table,
  `extract_country` / `normalize_country` / `resolve_country` /
  `countries_for_region`) extracted to new module
  `src/zbbx_mcp/country.py`. `data.py` re-exports the public symbols
  for back-compat — every existing `from zbbx_mcp.data import ...`
  callsite keeps working. `data.py` shrank from 659 to 334 lines.
- `costs.py` split: the 2173-line / 14-tool monolith broken into
  four cohesive modules. `costs_common.py` (shared helpers + tags),
  `costs_import.py` (6 ingestion tools), `costs_audit.py` (5 audit
  and reconciliation tools), `costs_summary.py` (3 read-only summary
  tools). Tool count and names unchanged.
- Output formatters `_format_value` and `_format_age` promoted to
  public `format_value` and `format_age` in `formatters.py`.
  Analytics helpers `_subnet24`, `_parse_ip_changes`,
  `_compute_loss_drift`, `_split_baseline_recent` had their
  underscore prefixes dropped (they are imported across tool
  modules and were never module-private in practice).

### Changed — Robustness
- `server.py` gained a single shared `_iter_registered_tools` helper
  with graceful fallback if FastMCP renames its private
  `_tool_manager._tools` attributes. Both `_compact_descriptions`
  and the tool-wrapping loop now degrade with a logged warning
  instead of raising `AttributeError` at startup.

### Added — CI gates
- `mypy` typecheck job runs `mypy src/zbbx_mcp` on every push / PR.
  `tools/` excluded for now (~180 accumulated type smells); core
  modules (`data.py`, `fetch.py`, `formatters.py`, `classify.py`,
  `config.py`, `client.py`, `server.py`, `logging.py`,
  `rollback.py`, `resolver.py`, `utils.py`, `excel.py`,
  `country.py`) are clean.
- `pytest --cov=zbbx_mcp --cov-fail-under=15` runs in the test job;
  prevents silent coverage regression below the current floor.

### Added — Documentation
- `docs/adr/README.md`: index of all 24 ADRs grouped by theme
  (cost-import pipeline, infrastructure, outage correlation,
  disruption detection, trends / traffic / problems, token
  efficiency and hygiene, observability and architectural hygiene).
- `CONTRIBUTING.md`: new "Sensitive content" section with the
  public-repo hygiene rules and the reproducible pre-commit scan
  command. The new CI gates (ruff, mypy, coverage) listed in the
  code-style section.

### Tooling
- 156 tools across 54 modules.
- 393 tests.
- ADRs 010 through 024.

## [1.7.0] - 2026-05-05

### Added — Outage correlation (ADR 010, 015, 022)
- `get_idle_relays`: relay hosts whose mgmt NIC has traffic but tunnel
  interfaces report zero. Exclusion-based detection plus a physical-NIC
  regex fallback so unused secondary adapters don't bucket as tunnels.
- `get_outage_clusters`: greedy time-window grouping of active problems.
  Supports `subnet24` / `subnet16` / `provider` / `hostgroup` / `auto`.
- `get_host_floods`: single-host outage detector — N simultaneous
  problems on one machine. Sub-host (parent + " " + suffix) merges.

### Added — Disruption detection (ADR 012, 013, 014, 020)
- `detect_loss_drift`: ping-loss / RTT drift vs 14d baseline.
  Env-driven (`ZABBIX_PING_LOSS_KEY`, `ZABBIX_PING_RTT_KEY`).
- `detect_service_port_split`: service-port traffic dropped while
  management is healthy. Env-driven (`ZABBIX_SERVICE_BPS_KEY`).
- `detect_regional_traffic_loss`: regional-bucket traffic collapse vs
  flat peers. Env-driven JSON map (`ZABBIX_REGIONAL_TRAFFIC_KEYS`).
- `detect_disruption_wave`: many hosts × many /24s in the same hour.
  Diurnal-safe defaults, country-cohesion guard, and peer-relative
  drop pre-filter (host vs same-cohort peers) to suppress diurnal
  false positives.

### Added — Risk and impact (ADR 013, 014)
- `get_at_risk_hosts`: composite score over peer rotations + ping/RTT
  drift + IP age. Skips hosts with no peer churn AND no drift signal.
- `get_disruption_blast_radius`: cohort connection-count delta
  pre/post a host drop. Reuses `KEY_CONNECTIONS`.

### Added — External IP history (ADR 012, 013, 019)
- `get_external_ip_history`: per-host IP rotation timeline with
  recovery scoring against a 24h pre/post traffic comparison.
- `get_recovery_score`: fleet-level recovery KPI aggregator.

### Added — Trigger / problem analysis (ADR 011, 019)
- `get_trigger_timeline`: OK ↔ PROBLEM transitions for a trigger.
- `bulk_acknowledge`: acknowledge many events at once.
- `get_problem_age_buckets`: per-severity histogram (<1d / 1-3d /
  3-7d / 7d+) — fills the visibility gap on the actionable
  1–7d band.
- `get_stale_items` cascade-aware mode (`collapse_dependencies`) —
  folds downstream stale dependents into stale master via
  `master_itemid` walk.

### Added — Token efficiency (ADR 016, 017)
- `ZABBIX_TIER` env var bundles for focused sessions: `core` (~5k
  tokens), `ops` (~11k), `finance` (~10k), `reports` (~13k), or
  `full` (default, ~25k). Cuts 60–80% off the tools/list handshake
  for typical sessions.
- Schema `title` field strip + cost-tool docstring trim — knocked
  ~5k tokens off the full-tier handshake.

### Added — Country normalization (ADR 023)
- `normalize_country()` and `resolve_country()` in `data.py`.
  `search_hosts`, `search_hosts_by_location`, `get_server_clusters`
  now accept ISO-2, ISO-3, or English country name. Result header
  surfaces the resolved code so the caller sees that the input was
  understood. Hosts without a country segment in their hostname fall
  back to Zabbix host inventory.

### Changed — Accuracy and noise reduction (ADR 014, 015, 018, 020, 021, 022)
- `get_active_problems`, `get_correlated_events`, and
  `get_outage_clusters` collapse host-embedded triggers (`Foo on
  host-a` / `Foo on host-b`) under the same dedup key via a new
  `normalize_problem_name` helper. Affected hostnames remain
  visible in the affected-hosts column.
- `detect_disruption_wave` defaults retuned for diurnal safety
  (window 6h → 12h, recent 1h → 2h, drop 30% → 50%) plus a new
  `min_baseline_mbps=5.0` floor.
- Service-check tools (`fetch_service_status`, `generate_service_brief`,
  `get_health_assessment`, `detect_regional_anomalies`,
  `get_service_uptime_report`, `get_service_health_matrix`) now skip
  unsupported / stale-lastclock items instead of reading their
  lingering 0 as service-down.
- `get_outage_clusters` and `get_host_floods` gain a `max_age_hours`
  recency filter (default 0 = unlimited preserves existing
  behaviour). Both surface the cluster / flood age in the output via
  a shared `_format_age` helper.
- `detect_disruption_wave` and `get_outage_clusters` use canonical
  hostid (`build_parent_map`) so a parent + sub-host pair counts
  once in cohesion / unique-host calculations.
- `detect_traffic_drops` skip-breakdown footer surfaces what was
  dropped (no-history / no-baseline-window / below-floor).
- `get_shutdown_candidates` peer-headroom safety check
  (SAFE / RISKY / SOLO).
- `get_outage_clusters` `problem.get` switched to
  `sortfield="eventid"` (Zabbix 6.4 rejects sortfield=clock).
- `get_idle_relays` NAT-mode caveat softened — observed false-
  positive rate is low.

### Fixed
- `get_item_history` accepts ISO date, ISO datetime, relative
  ("24h", "7d"), and epoch int.
- `get_problems` time-window filters: `time_from`, `time_till`,
  `include_resolved`, `event_eventid` (problem timeline).
- `search_hosts` markdown table preserved at scale.
- `get_at_risk_hosts` skips hosts that score on age alone (no peer
  churn, no drift) — was returning every host at the same floor
  score.
- `import_from_xlsx` localised header is now env-driven
  (`ZABBIX_BILLING_IP_HEADER`); no non-ASCII literal in source.

### Tooling
- 155 tools across 49 modules.
- 386 tests (pure-helper coverage on every new analytic).
- ADRs 010 through 023 documenting design decisions.

## [1.6.0] - 2026-03-30

### Added
- **112 tools** across 35 modules
- 4 analysis tools: `analyze_server_roles`, `correlate_logs`, `audit_host_ips`, `classify_external_ips`
- `detect_regional_anomalies`: detect unusual patterns within a geographic region
- `find_host_dashboard`: quick host-to-dashboard lookup
- `generate_product_map`: auto-create starter product config from Zabbix groups
- `get_product_audit`: categorize servers by product as active/dead/idle with cluster awareness
- `get_audit_log`: Zabbix audit log for host creation dates and change history
- `get_host_availability`, `get_recent_changes`: host uptime and config change tracking
- `get_service_health_matrix`, `get_service_uptime_report`: service-level monitoring
- `get_error_rate`: error rate analysis per host/group
- `get_incident_report`: incident reporting with timeline
- `get_traffic_drop_timeline`: traffic drop analysis over time
- `get_unknown_providers`: group unclassified server IPs by /16 prefix
- `identify_providers`: auto-detect unknown hosting providers via reverse DNS
- Exclusion-based tunnel detection (replaces hardcoded checks)
- Tag-based NIC discovery for traffic instead of hardcoded interface key list
- `HIDE_PRODUCTS` env var to exclude products from CEO report fleet composition
- Sentry error capture and logging integration
- GitHub Copilot and Codex CLI setup guides in README
- MCP resources support

### Changed
- Service check keys fully configurable via env vars (no hardcoded values)
- Lazy-load openpyxl (~15MB RAM savings on startup)
- Tool descriptions trimmed: 6010 → 5332 chars compacted (−11%)
- Virtual interface blacklist replaced with physical NIC whitelist
- CEO report uses service fleet counts (excludes infra/monitoring from KPIs)
- Cluster-aware product audit: detects secondaries of active primaries
- Version bump to 1.6.0

### Fixed
- Trend sanity: change < −30% with "stable" now correctly shows "dropping"
- Trend sanity: change > +30% with "stable" now correctly shows "rising"
- Traffic unit: removed incorrect ×8 multiplier (values already in bits/sec)
- CEO report change %: uses trend-vs-trend comparison (same data source)
- CEO report avg: uses TrendRow.avg (proper mean) instead of broken daily running average
- Dead server count: requires actual traffic monitoring data (was counting hosts without items)
- TrendRow.daily: proper sum/count mean replaces broken (old+new)/2 running average
- `ZABBIX_TRAFFIC_UNIT` env var: set to `bytes` for deployments where net.if.in returns bytes/sec
- All traffic conversions use configurable divisor (bits: /1M, bytes: /8M)
- Dependabot: bumped pytest>=9.0.3, pytest-asyncio>=1 (CVE fix)
- Domain CSV export: 19 fields including SSL expiry days, issuer, response time, HSTS, IPv6
- Provider "Unknown": hosts without IP skipped from distribution
- UK → GB country code normalization
- Service DOWN: don't mark as broken if server has real traffic (>2 Mbps)
- Off-by-one in trend sanity boundary values

### Security
- All service check keys configurable via environment variables
- 93 hosting providers (368 CIDR ranges) in classification database
- Comprehensive code and history audit for data hygiene
- Test assertions use generic examples only

## [1.5.0] - 2026-03-29

### Added
- **100 tools** — configurable service keys, product filtering
- `generate_ceo_report`: full executive HTML report with all analytics sections
  - Executive Summary with auto-generated alerts
  - Traffic by Country with trend badges and bar charts
  - Service Uptime by Country (SLA dashboard)
  - Capacity Planning (Mbps/server density)
  - Risk Assessment (provider concentration, redundancy)
  - Fleet Composition (product breakdown cards)
  - Shutdown Candidates (dead/broken/idle with server table)
  - Provider Distribution (stacked bar chart + concentration risk)
  - Expansion Opportunities (LATAM/APAC/EMEA tables)
  - Strategic Recommendations (immediate/short/medium-term actions)
  - Country Deep Dives (auto-detected + manual via `deep_dive_country` param)
  - Traffic Redistribution Analysis (where traffic goes when servers go down)
  - Status Legend (severity labels explained)
- `get_peak_analysis`: peak vs off-peak traffic by hour-of-day
- `get_executive_dashboard`: single-call KPI summary
- `get_month_over_month`: period comparison on traffic/CPU/countries
- `get_fleet_risk_score`: composite risk per country
- `get_sla_dashboard`: uptime % by product/country
- `get_report_snapshot`: save KPIs as JSON
- `get_expansion_report`: regional coverage gap analysis
- `get_regional_density_map`: server density by country with datacenter info
- `get_latency_estimate`: nearest server by geographic distance (haversine)
- `get_event_frequency`: flapping detection
- `get_correlated_events`: find same problem on multiple hosts
- `get_server_clusters`: detect host clusters from naming patterns
- `search_hosts_by_location`: compound query with country/group/product/traffic filter
- `resolve_datacenter(ip)`: IP-to-datacenter-city via CIDR ranges
- Region filters on geo/traffic tools (LATAM/APAC/EMEA/NA/CIS/ALL)
- Region mapping (`REGION_MAP`, `CAPITAL_COORDS`) in data.py
- Shared fetch helpers: `fetch_enabled_hosts`, `fetch_traffic_map`, `fetch_cpu_map`, `group_by_country`, `host_ip`
- Pre-compiled regex patterns in response compression
- Host fetch caching (60s TTL)
- Batched trend fetch (200/chunk) to avoid Zabbix 500 on large fleets
- CLAUDE.md, REVIEW.md, AGENTS.md for AI agent context
- `py.typed` marker (PEP 561), `__all__` on all core modules
- CI lint job (ruff), ruff config in pyproject.toml
- Multi-stage Dockerfile with non-root user and healthcheck

## [1.0.0] - 2026-03-24

### Added
- Initial release with comprehensive Zabbix MCP server
- Multi-instance support, read-only mode, HTTPS enforcement
- Async HTTP/2 client with connection pooling
- Rollback system with pre-mutation snapshots
- Excel and HTML report generation
- Traffic anomaly detection and regional-loss monitoring
- Cost management via host macros
- Slack integration
- Structured JSON logging with Sentry support
- Docker support with docker-compose
- GitHub Actions CI (Python 3.10–3.13)
