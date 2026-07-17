# Architecture Decision Records

One short document per non-obvious design choice. Each ADR captures the
problem the change solved, the decision taken, the test approach, and
the consequences — enough that a reviewer reading the repo cold can
recover the *why*.

## Format

Each file is dated, numbered, and titled by the change. The body uses
fixed headings (`## Problem`, `## Decision`, `## Test approach`,
`## Consequences`, `## Not included`) so the records compare cleanly.

## Index

### Cost-import pipeline (v1.6.0 and earlier)

| # | Title | One-line summary |
|---|---|---|
| [001](001-only-if-empty-cost-import.md) | Only-if-empty cost import | Skip hosts that already have a `{$COST_MONTH}` macro |
| [002](002-compound-hostname-cost-match.md) | Compound hostname cost match | Match `host-a host-b` billing entries against parent + child Zabbix hosts |
| [003](003-fill-cost-median.md) | Fill cost median | Estimate `{$COST_MONTH}` from peer median for empty hosts |
| [004](004-cost-audit-pipeline.md) | Cost audit pipeline | Tiered probability analysis of unmatched cost entries |
| [005](005-export-cost-audit.md) | Export cost audit | XLSX export for accounting review with source-of-truth column |
| [008](008-prefix-name-match-guard.md) | Prefix-name match guard | Tighten the `/24` prefix match in cost imports |
| [009](009-reconciliation-pass-safety.md) | Reconciliation pass safety | Prefer earliest pass; zero-extras; dup-name handling |

### Infrastructure

| # | Title | One-line summary |
|---|---|---|
| [006](006-bump-python-multipart.md) | Bump python-multipart | Pin to ≥0.0.26 for CVE-2026-40347 |
| [007](007-ruff-import-sort.md) | Ruff import-sort | Fix `I001` errors across the tree |

### Outage correlation suite (v1.7.0)

| # | Title | One-line summary |
|---|---|---|
| [010](010-relay-and-outage-correlation.md) | Relay + outage correlation | `get_idle_relays`, `get_outage_clusters` with greedy time-window grouping |
| [015](015-name-normalization-host-floods-nic-regex.md) | Trigger-name normalisation, host-flood detector, NIC regex | Collapse "on \<host\>" suffixes; single-host outage tool; unused-NIC regex fallback |
| [021](021-max-age-recency-filter-on-clusters-and-floods.md) | `max_age_hours` recency filter | Drop stale "active" clusters/floods so current-incident consumers see only fresh data |
| [022](022-parent-subhost-canonical-id-in-cohesion-and-cluster-counts.md) | Parent+sub-host canonical fold | Use `build_parent_map` so one physical machine never double-counts |

### Disruption detection (v1.7.0)

| # | Title | One-line summary |
|---|---|---|
| [012](012-disruption-detection-building-blocks.md) | Disruption building blocks | `detect_loss_drift`, `get_external_ip_history`, multi-level cluster grouping |
| [013](013-disruption-detection-composites-and-sortfield-fix.md) | Disruption composites + sortfield fix | `detect_service_port_split`, `detect_regional_traffic_loss`, `detect_disruption_wave`, `get_at_risk_hosts`, `get_disruption_blast_radius`, `get_recovery_score` |
| [014](014-at-risk-skip-and-wave-defaults.md) | At-risk skip rule + wave defaults | Skip hosts with no real signal; retune wave defaults for diurnal safety |
| [018](018-service-check-stale-gate.md) | Service-check stale gate | Skip `state=1` / lastclock-frozen items before counting `lastvalue=0` as down |
| [020](020-disruption-wave-cohesion-and-peer-relative.md) | Wave cohesion + peer-relative drop | Reject globally-spread diurnal false positives; compare each host to its cohort |

### Trends, traffic, and problem analysis (v1.7.0)

| # | Title | One-line summary |
|---|---|---|
| [011](011-traffic-drops-skip-visibility-and-shutdown-headroom.md) | Traffic-drops skip visibility + shutdown headroom | Surface skipped-server counts; peer-headroom safety check on shutdown candidates |
| [019](019-age-buckets-cascade-collapse-rollup-wontfix.md) | Age buckets + cascade collapse + rollup WONTFIX | `get_problem_age_buckets`; `collapse_dependencies` on stale items; declined disruption-rollup composer |

### Token efficiency and hygiene (v1.7.0)

| # | Title | One-line summary |
|---|---|---|
| [016](016-tier-presets-and-docstring-trim.md) | Tier presets + docstring trim | `ZABBIX_TIER` cuts handshake 60–80%; trimmed verbose docstrings |
| [017](017-schema-title-strip-cost-docstring-trim-localized-header.md) | Schema-title strip + cost docstring trim + env XLSX header | Drop redundant JSON-schema `title` field; env-driven localised header |
| [023](023-country-input-normalization.md) | Country-input normalisation | `normalize_country()` accepts ISO-2 / ISO-3 / English name; inventory fallback |

### Observability and architectural hygiene (post-v1.7.0)

| # | Title | One-line summary |
|---|---|---|
| [024](024-telemetry-summary-tool-and-defensive-fastmcp-hook.md) | Telemetry summary tool + defensive FastMCP hook | `get_telemetry_summary` surfaces per-tool usage from the existing analytics log; `_iter_registered_tools` centralises private-API access with graceful fallback |
| [025](025-evidence-based-tier-recut.md) | Evidence-based tier re-cut from 16d telemetry | 12 zero-usage tools demoted from `core`; tier handshakes shrink 18–30% |
| [026](026-diagnose-host-composite-tool.md) | `diagnose_host` composite tool | One MCP call composes the per-host diagnostic chain operators ran manually for every incident |
| [027](027-zabbix-version-introspection-and-ack-action-bits.md) | Version introspection + extended ack actions | `get_zabbix_version` with feature matrix; `acknowledge_problem` gains `severity` + `unack` action bits |
| [028](028-bulk-and-subnet-diagnosis.md) | `bulk_diagnose` + `diagnose_subnet` | Fan-out `diagnose_host` across a target set (hosts / group / country) or a CIDR; one compact table per call |
| [029](029-tags-deps-and-anomaly-triggers.md) | Tag filtering, dependency surfacing, anomaly-trigger discovery | `parse_tag_filter` plumbed into 4 tools; `with_dependencies` flag on `get_triggers`; new `get_anomaly_triggers` for Zabbix 6.4 native time-series triggers |
| [030](030-cost-summary-redact-partial.md) | `redact_partial` flag on `get_cost_summary` | Opt-in filter for externally-shared cost summaries — drops partial-coverage rows, recomputes the grand total, suppresses the "M without cost" line |
| [031](031-cve-bumps-multipart-urllib3-idna.md) | CVE bumps: `python-multipart`, `urllib3`, `idna` | Lockfile-only bumps past CVE-required minimums for three transitive deps |
| [032](032-canonical-host-groups-parent-fold.md) | `canonical_host_groups` parent fold | New `data.py` helper collapses parent + sub-hosts into one canonical group per physical machine (cost=MAX, traffic=SUM, cpu=MAX). Applied to `get_cost_efficiency`, `get_cost_summary`, `get_cost_gaps` |
| [033](033-cluster-dedupe-canonical-host-name.md) | Outage-cluster dedupe by canonical host name | `_cluster_problems` counts physical machines (canonical name), not raw hostids — multi-VIP boxes no longer falsely satisfy the cluster threshold |
| [034](034-canonical-fold-service-check-tools.md) | Canonical-name fold for service-check tools | `canonical_host_name` promoted to `data.py`; new `fold_rows_by_canonical_host` helper; applied to `generate_service_brief`, `detect_regional_anomalies`, `get_service_uptime_report`, `get_service_health_matrix` |
| [035](035-eager-init-excel-fills.md) | Eager-init Excel fill constants | Drop lazy-init globals in `excel.py`; fixes `generate_full_report` crashing on `wb.save()` with `TypeError: expected Fill` (Sentry `dc717f4d`) |
| [036](036-parent-fold-inventory-and-traffic-tools.md) | Parent / sub-host fold for inventory + traffic tools | Seven more aggregators dedupe by canonical name: `get_high_cpu_servers`, `get_underloaded_servers`, `get_low_disk_servers`, `get_low_memory_servers`, `get_stale_servers`, `detect_traffic_drops`, `get_traffic_report` (SUM semantic for the last) |
| [037](037-shutdown-candidates-canonical-fold.md) | Parent / sub-host fold in `get_shutdown_candidates` | Pre-fold candidates + cohort headroom pipelines; CPU=MAX / traffic=SUM / service=WORST aggregation across canonical groups; fixes false-positive DEAD on multi-record boxes + inflated peer counts |
| [038](038-server-name-includes-version.md) | Server name carries the package version | `FastMCP("zabbix v{__version__}")` so Claude Code `/mcp` panel shows the running version; `__version__` derived from installed dist metadata |
| [039](039-bulk-diagnose-prefold.md) | Pre-fold input host list in `bulk_diagnose` / `diagnose_subnet` | `_dedupe_records_by_canonical()` collapses parent + sub-hosts to one diagnostic row per physical machine before the fan-out; result rows annotated `parent (+N sub)` |
| [040](040-traffic-drop-classifier.md) | False-positive-resistant traffic-drop classifier | New `anomaly.py` brain: recent-avg vs seasonal same-hour band, acute-vs-sustained, demand-vs-block corroboration, baseline-weighted interface pick; `detect_traffic_drops` rebuilt on it |
| [041](041-predictive-high-tier-render.md) | Render HIGH tier in `get_predictive_alerts` | Four-tier classifier wrote HIGH but the render collapsed it to INFO and omitted it from the summary — a false-negative; render the canonical severity field directly |
| [042](042-traffic-drop-corroboration.md) | CPU/connection corroboration in `detect_traffic_drops` | Bounded second pass fetches CPU + connection trends for candidates only; demand troughs (signals fall with traffic) reclassify as `low_demand`; new `metric_recent_baseline_ratio` helper with idle→used inversion |
| [043](043-idle-relays-out-vs-in-gate.md) | `get_idle_relays` out-vs-in gate | Gate on physical out/in ratio, not inbound-only — healthy NAT-mode relays (forward through physical NIC, tunnels idle by design) no longer flagged as forwarding failures |
| [044](044-maintenance-suppress-filter.md) | Maintenance-suppress filtering | `filter_suppressed` helper + `include_suppressed=False` on `get_active_problems` / `get_problems` / `get_host_floods` / `get_outage_clusters` — planned-downtime problems no longer counted as incidents |
| [045](045-service-brief-country-fold.md) | `generate_service_brief` per-country fold | Per-country ok/partial/down counters fold sub-hosts to canonical (traffic SUM, service worst-wins) — multi-VIP boxes count once; new `_classify_country_group` helper |
| [046](046-diagnose-subhost-problems.md) | Diagnosis queries the whole canonical group | `diagnose_host` / `bulk_diagnose` query problems across every VIP hostid, not just the parent — a sub-host problem no longer hides as `healthy` |
| [047](047-regional-anomalies-classifier.md) | `detect_regional_anomalies` on the classifier | Per-host judgment via `classify_drop` fed a recent-days vs baseline-days average (`recent_baseline_from_daily`) — daily grain is diurnal-safe, killing the spot-reading false positives |
| [048](048-trigger-dependency-collapse.md) | Trigger dependency collapse | `get_active_problems` drops symptom problems whose trigger depends on another firing trigger (root-cause-only); new `collapse_dependent_problems` helper, `collapse_dependent=True` |
| [049](049-diagnose-group-wide-facts.md) | Diagnosis reads agent/traffic group-wide | `diagnose_host` / `bulk_diagnose` read items across every VIP — traffic sums across the box, agent uses the freshest ping; closes the "traffic lives on the VIPs" gap |
| [050](050-floods-dependency-collapse.md) | Dependency collapse in `get_host_floods` | Completes ADR 048's ticket — collapse symptom problems before the per-host count so a cascade (root + declared symptoms) doesn't falsely trip a flood |
| [051](051-regional-acute-mode.md) | Acute mode for `detect_regional_anomalies` | Opt-in `acute=True` sums each country's hourly traffic and judges it against its same-hour seasonal band (`aggregate_hourly_by_country`) — catches immediate regional blocks the daily grain dilutes |
| [052](052-complete-suppress-coverage.md) | Complete maintenance-suppress coverage | Wires `filter_suppressed` (ADR 044) into the last three problem-consuming tools — `diagnose_host`/`bulk_diagnose`/`diagnose_subnet`, `get_recent_changes`, `send_slack_report` — each gains `include_suppressed=False`; planned downtime no longer reads as live problems |
| [053](053-loss-drift-degraded-baseline.md) | Suppress false RTT drift vs degraded baseline | `compute_loss_drift` skips the RTT-drift branch when baseline loss ≥ 20% (`_BASELINE_LOSS_MAX`) — an outage baseline has unreliable RTT, so a recovered host (47% loss → 0%) no longer reads as `rtt-up`; mirrors zabbix-reports `_classify_loss_drift` |
| [054](054-starlette-cve-2026-48710.md) | Bump starlette to clear CVE-2026-48710 | Transitive `starlette 1.0.0 → 1.2.1` via `uv lock --upgrade-package` — clears the Dependabot-flagged Host-header request-smuggling CVE (CVSS 6.5); lockfile-only, 561 tests green |
| [055](055-zabbix-7-api-compat.md) | Zabbix 7.2+ API compatibility | Instance upgraded 6.4 → 7.4.9. Client now sends `Authorization: Bearer` (the 7.2-removed `auth` body property is gone) and translates `host.get`/`trigger.get` `selectGroups`↔`selectHostGroups` / `groups`↔`hostgroups` — one client boundary, no call-site churn; spans 6.2–7.x. +5 wire-format tests |
| [056](056-fix-get-proxies.md) | Fix `get_proxies` (never called a real method) | The tool called non-existent `relay.get`/`relayid` (scrub artifact) — errored on every invocation. Rewritten on `proxy.get` with 7.0 `name`/`operating_mode`, plus `version`/`compatibility` skew flags (⚠ outdated / ✗ unsupported) |
| [057](057-token-expiry-warning.md) | Token-expiry warning in `check_connection` | `token.get` inventory checked on every connection check — enabled tokens expiring within 30 days are listed soonest-first (`summarize_token_expiry`); silent degradation when the token API is unavailable. Catches the all-tools-die-at-once failure weeks early |
| [058](058-why-unclassified-audit.md) | Why-unclassified breakdown in `get_product_audit` | Auditing `product="Unknown"` now appends each unmapped group name with its Unknown-host count (`unmapped_group_counts`) — the exact `ZABBIX_PRODUCT_MAP` entries to add, prioritised by impact; skip-mappings respected |
| [059](059-native-problem-snooze.md) | Native problem snooze (suppress write path) | `acknowledge_problem`/`bulk_acknowledge` gain `suppress_hours` (-1 = until resolve) and `unsuppress` via ack bits 32/64 + `suppress_until` — activates the ADR 052 read path: snoozed noise drops out of every suppress-aware view, in Zabbix itself and all 7 tools here |
| [060](060-rank-problem-cause.md) | `rank_problem_cause` — durable cluster correlation | New write tool: mark events as symptoms of a cause (`event.acknowledge` bit 256 + `cause_eventid`; `unrank` via 128) — the `get_outage_clusters` follow-up that collapses an incident at the source, so the Zabbix UI and every consumer see 1 incident instead of N. Tool count 161 → 162 |
| [061](061-mcp-version-label.md) | Version in the `/mcp` dialog (`--version` + label sync) | `/mcp` labels by config key, not `serverInfo.name`, so the version was invisible. Adds a `--version` flag and `scripts/sync-mcp-label.py` (matches by command/args fragment, asks the wired invocation, renames the key to `zabbix v<version>`) — parity with slk-mcp ADR 024. +18 tests |
| [062](062-sync-label-all-containers.md) | `sync-mcp-label` updates every container | Fix: `any(rename_in(c) for c in …)` over a generator short-circuited, re-keying only the first `mcpServers` block and leaving the rest stale. Extracted `sync_config` mapping over a list so all containers are visited. +2 tests |
| [063](063-readme-accuracy-sync.md) | README accuracy sync | The README's hand-maintained counts had drifted and disagreed with each other (badge 161 / tier-table 156 / prose 154 vs the real 162). Synced to computed `ALL_TOOLS` / tier sizes, refreshed the `serverInfo`/`--version`/Zabbix-version examples; flagged auto-counting as the fix for the root cause |
| [064](064-python-multipart-cve-2026-53539.md) | Bump python-multipart to clear CVE-2026-53539 | Transitive `python-multipart 0.0.29 → 0.0.32` via `uv lock --upgrade-package` — clears the Dependabot-flagged High-severity quadratic-querystring CPU DoS (fixed in 0.0.30); lockfile-only, 608 tests green |
| [065](065-pyjwt-cve-2026-48526.md) | Bump PyJWT to clear CVE-2026-48526 | Transitive `pyjwt[crypto] 2.12.1 → 2.13.0` via `uv lock --upgrade-package` — clears the Dependabot-flagged High JWT algorithm-confusion (public JWK accepted as HMAC secret → forged HS256; fixed in 2.13.0); lockfile-only, 608 tests green |
| [066](066-dep-cves-2026-06-23.md) | Clear four Dependabot CVEs (batched) | Transitive `cryptography 46.0.7 → 49.0.0` (bundled-OpenSSL, High), `starlette 1.2.1 → 1.3.1` (urlencoded-body DoS + url.hostname poisoning), `pydantic-settings 2.13.1 → 2.14.2` (secrets_dir symlink traversal) — one `uv lock` re-resolve; lockfile-only, 608 tests green |
| [067](067-triage-slack-alert.md) | `triage_slack_alert` — authoritative alert verdict | New read-only tool: parse an AI/Slack alert line, resolve its host (EXACT/FUZZY/AMBIGUOUS/NOT_FOUND, never guesses), re-query live Zabbix (feed state never trusted), classify real_now/recovered/symptom. Pure core `alert_triage.py`; tool count 162 → 163, +24 tests |
| [068](068-triage-problem-get-selecthosts-fix.md) | `triage_slack_alert` live fix — `problem.get` rejects `selectHosts` | First live call crashed with -32602 (`problem.get` has no `selectHosts`, unlike event/trigger.get). Map problem→host via the `trigger.get` already fetched for dep-collapse (now `selectHosts:["hostid"]`), no extra round-trips. Added `TestTriageWireContract` (the wire path v1.16.0's pure-core tests never exercised); 633 → 636 |
| [069](069-diagnose-active-problem-age.md) | `diagnose_host` false `healthy` — age-filtered active problems | The verbose cutoff dropped any problem whose *start* clock was older than `problem_hours`, so a host with unresolved Disasters from days ago read `healthy`/0. Fix: `_keep_active_or_recent` never ages out unresolved problems (windows only recently-resolved via `r_eventid`); shared by all three diagnose tools. Verdict change; +8 tests, 633 → 641 |
| [070](070-recent-changes-selecthosts-fix.md) | `get_recent_changes` — same `selectHosts` bug as ADR 068 | Second live -32602: its `problem.get` carried `selectHosts` too. Same fix (map problem→host via `trigger.get`); full-repo sweep of all 30+ `selectHosts` sites confirms this was the last `problem.get` carrier. Wire-contract test; 641 → 644 |
| [071](071-problem-detail-rank-snooze.md) | `get_problem_detail` surfaces rank + snooze | Closes task 162: `suppress_until` requested and rendered (`_format_snooze_status` — maintenance window / until-resolve / remaining time / lapsed), and non-zero `cause_eventid` renders "symptom of cause event N". Read paths for ADR 059/060; +10 tests, 644 → 654 |
| [072](072-architecture-guards.md) | Architecture guards | The quarter's two recurring failure classes become tests: AST contract-guard (deny-listed params like `selectHosts` on `problem.get`, the twice-shipped -32602) + doc-count guard (README badge/headline/tier table + CLAUDE.md pinned to computed registry). Shared `tests/wiretest.py` replaces 3 copy-pasted scaffolds; 3 stale CLAUDE.md rows fixed. Tests+docs only; 654 → 659 |
| [073](073-runtime-self-awareness.md) | Runtime self-awareness | `check_connection` warns when the running build lags the source tree (the recurring "why isn't the fix live" class — process imports `__version__` once, reconnect loads it); `get_telemetry_summary` gains a Σ-tokens footer so token-effectiveness is one call. +10 tests, 659 → 669 |
| [074](074-file-length-budgets.md) | File-length budgets | Length isn't the variable — structure is. The 4,104-line / 67-class `test_analytics.py` sink split into 9 domain files (AST-mechanical, collect-count invariant 669 → 669); `TestFileLengthGuard` caps src ≤ 1100 / tests ≤ 1000 with zero grandfathers. Structured big tool modules deliberately kept |
| [075](075-time-honest-uptime-and-retention.md) | Time-honest uptime + retention honesty | `get_service_uptime_report` counted only observed trend rows → a 1-sample dead host read 100% and chronically-dead hosts vanished. Shared pure `uptime.py`: denominator = first-seen→now (missing hour = down), traffic gate rescues deprecated-check false-downs; coverage note + m-o-m retention guard; SLA dashboard relabelled point-in-time. +14 tests, 671 → 685 |
| [076](076-path-confinement.md) | Filesystem confinement for caller paths | Validated the repo against advisory GHSA-99mq-fjjc-6v9j (CWE-22/CWE-73 path traversal). Shared confinement in `utils.py`: `realpath` + `commonpath` (sibling-prefix-safe) against an allowlist of roots (`ZBBX_FILE_ROOTS`-extendable), symlink-resolved reads with a size cap, filename-basename guard. Every caller `file_path`/`output_dir` routed through it; fixed the `safe_output_path` `startswith` bug. +19 tests, 685 → 704 |
| [077](077-select-field-contract.md) | `selectAcknowledges` illegal field + select-field guard | `get_problem_detail` was dead on *every* problem: `selectAcknowledges` asked for `alias` (a pre-5.4 *user* field that never existed on an acknowledge object) → -32602 on the whole call; the renderer would have printed `?` for the author regardless. ADR 072's guard checked param *names*, not field *values*, so the class was invisible. Fixed the fields, resolved the author via `user.get` (best-effort, falls back to `user <id>`), and added a select-field guard over `ALLOWED_SELECT_FIELDS`. +6 tests, 704 → 710 |
| [078](078-diagnose-traffic-window-and-carrier.md) | diagnose traffic: collapsed baseline window + carrier dilution | `diagnose_host` printed "No traffic items" for hosts moving tens of Mbps. Three defects: the baseline window collapsed for any `traffic_hours >= 24` (making the `traffic_lost` verdict **unreachable** — a dead host read `healthy`); traffic was a flat mean across every NIC, so a busy carrier beside an idle one read half (bond0 ~60 + idle eno4 → 30.1 Mbps); and `diagnose` vs `fetch_traffic_map` used two disagreeing definitions of "traffic item". Pure `_traffic_windows` + `_carrier_traffic_mbps` + one shared `is_physical_traffic_in_key`. +11 tests, 710 → 721 |
| [079](079-fleet-data-guard.md) | No deployment magnitudes in public docs | The repo is public; the systems it is operated against are not. The pre-push scan is a *string* deny-list, so numeric magnitudes and ISO country codes are invisible to it by construction. New guard over the docs: `fleet of <n>`, observed host/server/cluster counts, 3+ digit counts, subnet spreads, and regional footprints (validated against the repo's own ISO-3166 dataset). Configured thresholds and caps keep passing. +3 tests, 721 → 724 |
| [080](080-test-host-exclusion.md) | Test/staging hosts must not pollute fleet verdicts | Non-production boxes landed in every fleet-wide verdict — padding analysed counts and adding phantom failures to protocol sweeps. Classifying by host *group* does not work: the test boxes sit in **production** groups while the test groups go unused. One token-bounded pattern (`ZABBIX_TEST_NAME_RE`) applied to the host name **and** every group name, union of both; `partition_test_hosts` splits rather than filters so the excluded hosts are always named. +23 tests, 724 → 747 |
| [081](081-per-hour-traffic-gate.md) | Per-hour traffic gate + test-pattern gaps | The ADR 075 traffic gate was window-wide — a host that served a week then hard-died read ~100% instead of ~50% (task 172). `traffic_hours_from_trends` builds per-hour sets from physical-NIC trends; a silent check-hour is rescued only if THAT hour had traffic. Also closes two ADR 080 pattern gaps (dot separators, `test2`) that split determinism with the sibling pipeline. +9 tests, 750 → 759 |
| [082](082-mcp-cve-2026-52869.md) | Bump `mcp` for CVE-2026-52869 (HTTP transport principal check) | High-severity SDK flaw: HTTP transports (SSE / streamable-HTTP) served session requests without verifying the authenticated principal (`mcp <= 1.27.1`, fixed 1.27.2). We expose those transports via `--transport`, and our own `<1.26.0` cap was *blocking* the patch — an over-tight pin turned security liability. Raised to `>=1.27.2,<1.28.0`; verified the FastMCP private-API surface we depend on survives the jump. Lockfile + constraint |

## Writing a new ADR

1. Pick the next number (`ls docs/adr/*.md | wc -l` + 1).
2. Use a hyphenated kebab-case title that matches the commit headline.
3. Copy an existing ADR's heading structure.
4. Land it in the same commit as the code change so reviewers see both.
5. Add a row to this index in the right section.
