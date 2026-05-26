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

## Writing a new ADR

1. Pick the next number (`ls docs/adr/*.md | wc -l` + 1).
2. Use a hyphenated kebab-case title that matches the commit headline.
3. Copy an existing ADR's heading structure.
4. Land it in the same commit as the code change so reviewers see both.
5. Add a row to this index in the right section.
