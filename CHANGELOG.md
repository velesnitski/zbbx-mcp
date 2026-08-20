# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [1.16.57] - 2026-08-20

### Fixed — dashboards built from graph widgets resolved to no hosts
ADR 131. Widget references are typed; both dashboard readers decoded `2`/`3`/`4`
(group/host/item) and ignored the rest. A classic **Graph** widget names a
*graphid*, and the graph belongs to its items' hosts — one hop through
`graph.get` that nothing followed. So a dashboard built entirely from graph
widgets resolved to zero hosts, `get_dashboard_detail` printed a widget count
with no host section, and `find_host_dashboard` answered "not found on any
dashboard" for hosts whose graphs sit on one.

Both tools now share one decoder (`collect_widget_refs`), follow the graph hop,
and name the match ("via graph '<name>'"). The page list also names the graph
each widget draws instead of repeating "[Graph]".

### Added — undecodable widget references are counted, not silently dropped
ADR 131. Some widgets name hosts by *pattern* rather than id and cannot be
resolved. Those are counted by widget type: the detail view warns their hosts
are absent from its list, and a "not found" answer carries the caveat that the
host could be on one of them. A widget with no fields is not counted — nothing
to read is not the same as failing to read something.

## [1.16.56] - 2026-08-20

### Fixed — `get_traffic_report` reported unmeasured session counts as zero
ADR 130. Connections were read with `host_conns.get(hid, 0)`, so a host carrying
no item for the configured connections key rendered `0` — identical to a host
genuinely reporting no sessions. On a fleet where that key is not deployed every
row read `0` connections with `–` bandwidth-per-client: a plausible and entirely
fabricated finding, since it claims servers carry traffic with nobody connected.

Unmeasured is now `None` and renders as `–`, with a footnote stating the count
is unknown rather than zero and how many rows it affects. It propagates through
the canonical fold (a box stays unmeasured only while no sub-host reported) and
sorts last rather than lowest, instead of competing with genuinely idle hosts.
A real zero still reads zero.

The environment variable was set, so the usual configured/not-configured check
passed. Configuration being present says nothing about whether the configured
item exists on the hosts — the same gap as ADR 128.

## [1.16.55] - 2026-08-20

### Fixed — the host-inventory country fallback never ran
ADR 129. ADR 099 added a fallback that resolves a host's country from Zabbix
inventory when the name carries no code. Five call sites requested
`country_code` and `country_name` via `selectInventory`; neither is a Zabbix
host-inventory field. The schema's country field is `site_country`, alongside
`site_city` and `location`.

An unknown field is not rejected — `host.get` accepts the request and returns
nothing for it (the silent-degradation behaviour of ADR 088) — so the inventory
dict never carried those keys and every branch of the fallback saw an empty
string. The feature was unreachable from the day it shipped, and invisible,
because a field that does not exist and a host with no inventory produce the
same empty result.

Requests and parsing now share `INVENTORY_COUNTRY_FIELDS`
(`site_country`, `site_city`, `location`), defined next to `resolve_country`
which consumes it. Values are free text, so each candidate goes through
`normalize_country` and yields `""` rather than a wrong code when it is not a
country; `"<city>, <cc>"` gets one validated attempt at the trailing token.

Whether the fallback now *fires* depends on whether this deployment populates
`site_country` — an unpopulated field yields `""` exactly as before, and the
existing inventory-gap note still names hosts whose country cannot be resolved.

### Added — a guard tying requested fields to the schema and to the parser
ADR 129. `HOST_INVENTORY_FIELDS` carries Zabbix's documented inventory column
set. Tests assert every requested field exists in it, that no dead field name
survives anywhere in `src`, that every `selectInventory` site uses the shared
constant rather than an inline list, and that every inventory field
`resolve_country` reads is one some caller actually requests.

The previous tests passed throughout by supplying `{"country_code": "NL"}` —
a dict Zabbix cannot produce. They proved the parser handled a shape that
never arrives. Those fixtures now use real field names.

## [1.16.54] - 2026-08-20

### Fixed — `generate_service_brief` reported an unmonitored check as healthy
ADR 128. The per-protocol table was built from `blocked_by_check`, which is
populated only from checks that FAILED. A key carried by no host produced no
rows — indistinguishable downstream from a key carried by many hosts of which
none failed — and rendered "all healthy". The section was also gated on there
being failures at all, so when nothing was failing the table vanished entirely,
taking the disclosure with it exactly when a reader concludes the fleet is fine.
Carriers are now counted separately from failures: a key nobody carries is
reported as such and labelled *not measured, not healthy*, while a carried and
passing key still reads healthy with its carrier count.

This is the opposite symptom from ADR 126 with the same root cause. Where
absence entered a numerator it read as failure; where it entered a filter it
read as health. The second is the more dangerous direction, because a false
DOWN gets investigated and a false green does not.

### Changed — health rollups disclose partial coverage
ADR 128. `get_sla_dashboard` consults one configured key and silently dropped
every host without it; the skip is correct (absent evidence must not become a
down vote) but the output read as a whole-fleet SLA. It now counts and names
those hosts. `get_service_uptime_report` names the configured key it does not
read — it reads primary and secondary, never tertiary.

Audited and unchanged: `get_service_uptime_report` has no numerator/denominator
asymmetry (it builds rows only from items that exist, then folds), and
`get_fleet_risk_score` consults no service check key at all.

## [1.16.53] - 2026-08-20

### Fixed — the health matrix read a missing check as a failing one
ADR 126. `get_service_health_matrix` folds sub-hosts into canonical groups and
scored each group per protocol with `all(...)` over every sub-host, while
scoring the denominator with `any(...)` over the same group — two different
sets. A sibling carrying no item of that key is absent from the value map, so
`None != 1` sank the whole group to DOWN while the other half still counted it
as measured. The halves of a pair are routinely provisioned differently here,
so this was the ordinary case: a country whose every protocol answered on both
halves of every pair reported `DOWN (0/3)`, while the wide walk in
`detect_dead_protocols` found every check alive. Groups are now judged only on
the sub-hosts that carry evidence for that protocol; nothing carrying the key
reads `N/A` rather than zero. Worst-wins is unchanged over what is measured.

The tool had **no behavioural test** — only its name in the registration list.
Five now pin it, and the first was confirmed to reproduce the exact live cell
against the old scoring before being accepted.

### Changed — the matrix discloses that its columns are configured keys
ADR 126. Three columns implied the fleet has three protocols; they are the
three configured check keys. A protocol served under any other key is absent
from the table, and absent is not down. The output now says so, counts its
`N/A` cells, and points at the tool that walks every check item. Widening the
column set to discovered checks is deferred — it changes what the columns mean.

## [1.16.52] - 2026-08-20

### Fixed — `detect_traffic_shaping` crashed on every invocation
ADR 125. The both-direction combiner added in 1.16.51 ranked verdicts by
position in a hand-typed tuple listing six of the module's eight. Any host
whose verdict was one of the two omitted raised `ValueError: tuple.index(x):
x not in tuple`, and one of them — `no_baseline` — is produced for any host
without trend history before the comparison window, so the tool failed on
every call from the moment it shipped. Ranking is now a dict keyed by the
verdict constants and read with a default: an unranked verdict sorts last and
still reaches the caller intact, because ordering is a presentation question
and no presentation question justifies failing a report. The `pinned` test
was switched off retyped literals to the same constants.

Thirteen tests covered the combiner and all stayed green, because every one
built its fixtures from the six values the tuple happened to list. Coverage is
now asserted against ground truth: a guard AST-walks the module for every
`ShapingVerdict(...)` construction and asserts its verdict is ranked, and a
second drives every verdict against every other plus `None`. Both were
confirmed to fail against the shipped ranking before being accepted.

### Changed — uncertainty outranks benign when combining directions
ADR 125. `no_baseline` and `insufficient` now rank above `normal` and `idle`,
and below every real finding. A host that reads normal one way and unjudgeable
the other has not been shown to be normal; headlining it `normal` renders
absent evidence as evidence of absence (ADR 107) at the pair level. A
direction that is provably shaped is still the headline.

## [1.16.51] - 2026-08-18

### Changed — provider table is generated from routing data, not hand-maintained
ADR 120. `detect_provider` resolved addresses against a table maintained by hand
in the source of `classify.py`. It could not be complete — there is no registry
of every hosting provider and allocations move between them — and a wrong entry
did not fail loudly: it attributed an address to the wrong provider,
confidently, in output someone acts on.

Two entries were in fact wrong. `18.0.0.0/8` was attributed to AWS, but that
range is announced by AS3 (MIT); AWS holds only part of the /8. `34.128.0.0` was
attributed to AWS and is Google Cloud. Both had been answering questions for as
long as the table existed.

So the table is now generated. `scripts/gen_provider_cidrs.py` derives
`src/zbbx_mcp/data/provider_cidrs.json` from the public prefix-to-AS dataset at
iptoasn.com, which maps every routed IPv4 prefix to the AS announcing it. The
rule is mechanical: drop unrouted space, aggregate each AS's prefixes, rank by
routed address count, keep the top N and each one's largest few blocks.
Aggregation preserves the exact address set and truncation only drops coverage,
so every failure mode is "resolves to Other" rather than "resolves to the wrong
name".

The table is substantially broader, and a substantially larger share of routed
IPv4 space now resolves to a name. Every provider and every range the previous
table carried is retained; probes drawn from it resolve identically apart from
the corrections above. Display names are pinned so
reports keep saying Vultr and Linode, and regeneration unions with the file on
disk so a narrower cutoff cannot silently drop an operator.

Alongside it, ZABBIX_PROVIDER_CIDRS — a JSON object of {"Provider": ["cidr",
...]} given as a file path or inline — is consulted BEFORE the generated table,
most-specific-first. Unparseable configuration disables the override entirely
rather than half-applying it: a partial merge would resolve some addresses
against operator data and others silently against the built-in, with nothing
saying which. A missing or corrupt data file degrades to an empty table with a
warning rather than taking the server down.

classify.py gets shorter; the data ships in the wheel as JSON.
1048 -> 1052 tests.


## [1.16.50] - 2026-08-18

### Added — repo-wide address guard
The fixture rule from 1.16.49 applied only to `tests/`, which left the same
mistake possible everywhere else. The guard now covers every tracked file.

Three things may be a routable address: non-global space (private, loopback,
link-local, multicast, reserved, RFC 5737), a network address DECLARED by the
allocation tables, or one of three documentation placeholders used in
docstrings to show CIDR syntax. Provider detection is a feature here, so the
tables have to be exempt — but the exemption is deliberately exact: the address
must BE a declared network address, not merely sit inside a known range.
Otherwise "somewhere in a provider block" would be a loophole wide enough for
any individual machine. That exactness is pinned by its own not-vacuous test.

1035 -> 1037 tests.

## [1.16.49] - 2026-08-17

### Added — a mutation guard, and a fixture-data guard
ADR 119. Two questions the suite could not answer about itself.

Does it pin anything? A green run proves the tests execute, not that they hold
behaviour down. Three defects in a single day were all of that shape: a
threshold that let the real case through, a ported rule that scored a fully dead
protocol as perfect, and a name that silently shadowed a dict.

Off-the-shelf mutation testing re-runs the whole suite per mutant — hours for a
thousand tests, which cannot share a CI job. So the question is narrowed rather
than the rigour: mutate only the pure functions where the decisions live, and
check each mutant against oracles stated in the guard itself. No subprocess, no
suite re-run, ~0.7s for 51 mutants across three modules. Operators are semantic
— comparison flips, and/or swaps, constant doubling, boolean negation — and a
mutant that passes every oracle is reported with its exact function, line and
mutation. Site counts are pinned per target rather than floored: a drop means a
branch was deleted and the guard silently got easier, a rise means new decisions
arrived with no oracle.

It found a gap on its first run, in its own oracles: two mutants of the score
clamp survived, and killing them required a PARTIAL uptime case, because with
full-uptime protocols a doubled scale saturates back to 100 and every other
assertion still holds.

Is the fixture data synthetic? An address from some real network makes a test
depend on where it runs and dates the moment that network is renumbered, and
the existing docs guard covered only the docs — tests/ was never in scope. Now
two layers. Structural and always on: fixture addresses must come from the
private, special-purpose or RFC 5737 documentation ranges, because "is this
address real?" invites an argument and "is it from a documentation range?" does
not. The four conventional dummies present were migrated to 192.0.2.x /
198.51.100.x, and the magnitude patterns now cover tests/ as well.

Configurable per deployment: identifiers specific to whoever runs this server
cannot be enumerated in the package itself, so they come from
ZBBX_SENSITIVE_STRINGS (file path or inline list) and are enforced whenever it
is set; unset, the test skips LOUDLY rather than passing silently, since a guard
that quietly does nothing reads green. When it fires it names the offending
files but never the term.

Both guards exempt their own files, which must carry deliberately invalid
samples to prove they are not vacuous. 1024 -> 1035 tests, 1 skipped by design.

## [1.16.48] - 2026-08-17

### Added — peer-relative detection for a host capped from birth
ADR 118, closing task 188 with one correction to its premise.

A node was reported as under-performing and every tool said it was fine:
diagnose_host healthy at 99% of its own baseline, no drop, no erosion, shaping
normal. All four were correct and all four were useless for the same reason —
they are before/after comparisons, and this host had no "before". It ramped from
idle straight into a ceiling on the hour it entered service and never exceeded
it. A host capped from birth never falls, so nothing looking for a fall can see
it.

Against peers sharing its country, product, provider and interface: hourly peaks
bounded at ~37 Mbps for five days while three identical siblings peaked at
363-408, and during busy hours its hourly MINIMUM sat at 34-36 — demand pressing
against a boundary, not demand failing to arrive. Average-to-peak 0.82 against
0.37-0.41 for the siblings: the curve was flattened, not scaled.

Correction to the task: it assumed such hosts are visible as `capped` and
proposed refining that verdict's wording. Measured against the real series,
ceiling_hit_rate returns 54% against a 60% gate, so the verdict is `normal` and
no refinement of `capped` would ever have fired. The gate was the problem, not
the wording. The task's rejection of widening the tolerance stands and is
respected — ADR 108's modal point-mass already absorbs cap wobble and a wider
band would erode the negative controls.

New: a cohort pass costing no extra fetch. swing_ratio = (p95-p10)/p95 over
active hours; a host serving real demand swings with the daily curve, a host held
at a limit cannot, whatever the morphology of the limit. classify_peer_cap
requires BOTH a swing materially below the country x product cohort median AND a
ceiling materially below the cohort's peak level. That second condition is the
saturation guard: a host flat at or above peer level is running at capacity,
which is healthy utilisation, not a limit. Declines rather than guesses with
fewer than three peers, or when the whole cohort is flat.

Renders on both exits — a peer-capped host is normally `normal` by the ceiling
test, so gating the section on the existing flagged list would have hidden
exactly the case it exists for.

The verdict states what was measured — flat and below peers — and stops.
Whether the limit is provider policy, a tc rule or a misprovisioned plan is not
visible in throughput. 1017 -> 1024 tests.

## [1.16.47] - 2026-08-17

### Fixed/Added — uptime coverage disclosure and a shadow per-protocol score
ADR 117, closing tasks 178(a) and 185, and recording a decision on 178(b).

Coverage was disclosed from the wrong extremum. coverage_note() computes it from
the earliest sample ANYWHERE in scope, so one long-lived host makes the note
report the most optimistic possible coverage for everyone, and a host whose own
window is a day says nothing. Same fleet-extremum mistake ADR 113 found in the
health matrix — there max(window), here min(clock), both picking the single most
flattering host. Hosts under 48h are now named, with the reason: trends belong to
the item, so recreating a host's checks restarts its history.

The shadow per-protocol score is added alongside the existing figures, changing
no verdict, sort order or threshold. "Up if any protocol answers" pins nearly
every host at exactly 100%, so it can neither rank hosts nor show a protocol
dying behind healthy siblings.

Its specification said deployed = "answered at least once in the window". That
rule is wrong for this purpose and the first synthetic case tried exposed it: a
host with three protocols and one dead for the ENTIRE window scores 100%,
because the dead protocol is dropped from the mean instead of counted as zero —
the exact case the score exists to surface. Deployed now means the protocol
produced measured hours (total > 0); a protocol the host does not run has no
item and no trends and is excluded, so nobody is punished for a protocol they
were never meant to run, while a measured protocol that never answered scores
zero.

Carry-forward (178(b)) is deliberately NOT ported. On the reporting side it
exists to stop a rebuilt host inflating a traffic-weighted fleet mean by about
half the error budget. This tool has no such number — per-host rows, and a
per-country MEDIAN, which is exactly the statistic a single short-window outlier
cannot move. Implementing it would substitute an unmeasured value into a number
that does not need protecting and take a cross-repo dependency on the sibling
pipeline's snapshot to do it. Closed by decision rather than left to be
re-raised; if this tool ever grows a weighted aggregate the argument changes.

1007 -> 1017 tests.

## [1.16.46] - 2026-08-17

### Fixed — diagnose_host now judges traffic against the same shape as the acute detector
ADR 116, closing task 186 and completing ADR 113. That ADR disclosed a bias
without removing it: the baseline is the 24h immediately preceding the recent
window, which sits in a different part of the daily cycle, so a healthy host
reads 50-70% "of baseline" outside peak hours and that reads like a fault.

The caveat helped, but the underlying problem stood — diagnose_host and
detect_traffic_drops gave different answers about the same host, and the one an
operator reaches for first in an incident was the one without a seasonal
comparison. A caveat asking the reader to go run another tool is not a fix.

diagnose_host now computes the same-hour-of-day floor that detect_traffic_drops
judges against, via the existing seasonal_floor helper over a 7-day window, and
states a verdict: at or above the floor, "WITHIN the normal band for this time
of day, so the ratio above is diurnal, not a fault"; below it, "N% BELOW the
normal band ... anomalous, not diurnal".

The cost, which the task asked to measure before porting: one extra trend.get
over 7 days. Nothing for a single host; for bulk_diagnose it multiplies by a
fan-out capped at 50 hosts. So it is a parameter defaulted OFF, and only the
single-host path opts in — bulk keeps its cheap ratio and the caveat that goes
with it. Both halves are pinned by test, because a default flipped by accident
would quietly multiply the cost of every fan-out.

A failed or empty seasonal fetch leaves the floor unset and falls back to the
caveat, naming which case applies: it enriches a report and must never break one.

Also rewritten: the ADR 113 assertions grepped the module source for its own
wording, and a lint pass rewrapping a string literal failed the test while the
behaviour was correct — Python's adjacent-literal concatenation meant no
whitespace normalisation could fix it either. They now run against rendered
output through the real renderer, which is what they were always trying to check.

1002 -> 1007 tests.

## [1.16.45] - 2026-08-17

### Added — template-fallback classification (task 187)
ADR 115, mirroring the reporting side's ADR 0080. A host can sit only in a mixed
host group whose members belong to several products. The group then classifies
it as infrastructure and it disappears from every product-scoped view —
availability, unit economics, per-country facts — while carrying production
traffic and a monthly bill. Mapping the group to any one product is wrong by
construction, because the group really does hold more than one family.

What separates them is the template: the deploy's own statement of what the
machine runs. When a host's groups classify to a non-serving product
(infrastructure/monitoring/unknown) and one of its templates is in an
operator-supplied allow-list, the implied product group is prepended to the
host's groups inside fetch_enabled_hosts, so every downstream classify_host()
call site sees it without a signature change. The original group is kept — the
evidence survives.

Groups always win when they answer. The template is consulted only for a host
the groups declined to classify and can never override a real product group;
that rule is what makes this safe and it is pinned by test.

Configuration rather than a constant: the reporting side can hardcode its
allow-list because it serves exactly one deployment, but a hardcoded
template-to-product table here would be wrong for every deployment but one. ZABBIX_TEMPLATE_PRODUCT_MAP holds it, accepting JSON or
`tpl:group,tpl2:group2`. Unset — the default — disables the feature completely
including the extra selectParentTemplates on the host fetch, so an unconfigured
deployment pays nothing. Malformed input disables rather than half-applies.

Worth knowing: without a product map the fallback is inert, because
classify_host then answers with each host's own first group name and nothing is
ever non-serving. That is correct, and pinned by test — it looked like a bug the
first time the tests hit it.

988 -> 1002 tests.

## [1.16.44] - 2026-08-17

### Added — detect_dead_protocols: the blind spot behind "UP if any protocol answers"
ADR 114, closing task 178(c). A user report of "connects, but no internet" was
investigated with the existing tools and every one of them said the host was
fine: agent reachable, traffic symmetric and normal for the hour, all protocol
checks green, no problems.

The actual finding took a scope test. One protocol check read 0 on every host
queried across a whole product fleet — several countries, several providers —
and had done so for a full day. Not one alert had fired, and none could have,
because availability is "any protocol answers" and the others were healthy.

Two choices come from that incident rather than from the reports-side original.

Discovery walks every `*check*` item instead of the three configured
ZABBIX_SERVICE*_CHECK_KEY values, because the check that was down is not one of
them — a detector scoped to configured keys would have reported the fleet
perfectly healthy, which is exactly what everything else did. nft.* firewall
assertions and non-uint items are excluded: not reachability, and a 0/1 verdict
on a string-valued check is meaningless.

Results group by CHECK with a denominator. As per-host rows the live case is
hundreds of lines nobody reads; as one row — dead on N of N judged hosts — it is
a platform outage with a single owner, and the ratio is what separates "this box
is broken" from "this protocol is down everywhere". The denominator counts hosts
where the check could be JUDGED, not where it failed; counting failures alone
would make every key read 100% dead and destroy the only number that matters.

Verdict order is deliberate: too young first (a verdict from two samples is an
artefact), then host dark (a machine entirely down is the SLA's finding), then
died vs never up — kept apart because they need different actions: died is a
regression with a timestamp to match to a deploy, never-up is a provisioning
gap. Un-judged hosts are named, not dropped. value_max not value_avg (ADR 0048).

The tool deliberately alerts nothing and changes no verdict; changing the
availability definition is a separate decision with its own re-baseline.
168 -> 169 tools, 970 -> 988 tests.

## [1.16.43] - 2026-08-15

### Fixed — the health matrix printed "no data" as ALL DEGRADED
ADR 113. A live incident was investigated with these tools, and both misled the
investigation before any real cause was examined.

get_service_health_matrix's `_status()` returns "N/A" when nothing was checked.
The recommendation then built its working-protocol list by testing `"OK" in s or
"PARTIAL" in s` — "N/A" contains neither, so the list came back empty and the
row printed ALL DEGRADED, the most alarming state on the board. In the live run
roughly a third of the country rows said ALL DEGRADED purely for having no data, and a
country whose service was confirmed working was among them.

A weaker form sat one branch further down: two protocols fine and the third
unmeasured rendered as "Proto 1 / Proto 2 only", asserting the third is broken
when nobody looked.

Now: no measurements at all reads "NOT MEASURED — no check data"; all measured
ones fine reads "OK where measured (N/3), M unmeasured"; and ALL DEGRADED
requires evidence — at least one protocol checked and every checked one failing.

### Fixed — diagnose_host's traffic ratio read like a verdict
Its baseline is the 24h immediately PRECEDING the recent window (ADR 078), so an
off-peak window is compared against a baseline holding the previous evening's
peak, and a healthy host reads 50-70% "of baseline" on a weekend morning. Live,
it reported 68% and 54% on hosts that detect_traffic_drops — seasonal,
same-hour-of-day, across the whole scope — cleared completely. Two tools in the same
server disagreed about the same host, and the one reached for first during an
incident was the wrong one.

The ratio now carries its own caveat below 85%: the baseline is not the same
hour of day, this is not an anomaly verdict, and detect_traffic_drops is what
gives one. Also: diagnose_host reads across the whole canonical group because
multi-VIP traffic lives on sub-host interfaces (ADR 049) — correct, but a
sub-host with no traffic items of its own showed its parent's numbers with
nothing saying so. It now discloses when figures span several records.

Not fixed here: a true seasonal band in diagnose_host needs a 7-day trend fetch
instead of 24h and would change the cost of every bulk diagnosis. Recorded as a
follow-up rather than done blind; the disclosure removes the harm meanwhile.
964 -> 970 tests.

## [1.16.42] - 2026-08-14

### Added — get_api_tokens: the surface a token audit had to go around
ADR 112. A Zabbix token audit had to bypass this server entirely and call
token.get by hand, because no tool covered API tokens. What it found is what a
routine tool would have surfaced without anyone going looking: expired temporary
tokens still present, several with NO expiry at all (one service token unused
for over a year), and a token named `test` on a personal account.

Every other object here is watched because it might break. A token is different:
the finding is its ABSENCE of use — a key nobody has touched in a year is not
idle capacity, it is an unrevoked credential.

The trap the pure core exists to avoid: Zabbix encodes "never" as 0 in both
lastaccess and expires_at, and 0 is also a valid timestamp meaning 1970. Treating
it arithmetically makes a never-used token read as ~19,000 days idle — or, once
sorted, as the MOST recently used one — and makes a token that never expires read
as long expired. Both are separate states, pinned by test, because either
confusion inverts the signal the tool exists for.

Ranked worst-first: expired-but-present, permanent-and-never-used,
permanent-and-stale, never used, permanent, stale. A disabled token ranks below
every live one regardless of its other flags — it cannot be used, and ranking it
on those flags would push real keys off the top of the list.

token.get is Super-admin-only unless a role grants it, so a denied call returns
"this is a permissions answer, NOT 'there are no tokens'" (ADR 103) — an empty
token list is otherwise the most reassuring possible output for the least
trustworthy possible reason. Owner lookup fails softer: a denied user.get still
lists tokens by id rather than blanking the audit.

Read-only by design; delete_api_token was offered by the task and left out,
because revoking a credential is not a step to take from a summary table.
167 -> 168 tools, 951 -> 964 tests.

## [1.16.41] - 2026-08-14

### Fixed — the un-judged disclosure asserted a cause it had not checked
ADR 111. ADR 107 named the hosts the shaping detector could not judge and
explained why it mattered: "Recreating a host's items destroys its trend
history". The first time that line was read against real data it was wrong —
twenty hosts on one dashboard came back un-judged with ~50h of history each and
none had been rebuilt; they had been provisioned two days earlier.

Both readings fit the same observation exactly: a short history means the items
were rebuilt, OR the host is new. The disclosure picked one and stated it as
fact, which is the defect the ADR it belongs to exists to remove — one level up,
in the prose instead of the data.

The audit log records host creation (resourcetype=4, action=0), so one
auditlog.get for the <=5 named hosts answers it outright: "50h of history — host
added 46h ago, so it is simply new" / "3h of history but host added 720h ago —
its items were rebuilt" / "age unknown (no Add record in audit retention)".

Three states, not two. Audit retention is finite so an old host legitimately has
no Add record, and that is "cannot tell" — it must not collapse into either
answer, which is pinned by test because collapsing it is how the original
wording went wrong. A 6h slack absorbs the boundary (trend flush timing).
host_added_hours is best-effort: a failure degrades every host to unknown rather
than breaking the disclosure it exists to enrich, and no call is made when there
is nothing to explain.

945 -> 951 tests.

## [1.16.40] - 2026-08-14

### Fixed — detect_traffic_drops counted the hosts it could not examine, never named them
ADR 110. A host collapsed ~98% (70-177 Mbps peaks to 1-20, sustained over a day)
and the tool returned "No blocks detected". Run live against its group: 25
analyzed, 17 healthy/diurnal, 8 skipped — and the collapsed host was in neither
the findings nor any named list.

Two reasons, both ending in silence. It was invisible to discovery, because its
items come from the stock Linux template and this tool searched by item name
until ADR 109. And separately, the hosts it DOES skip for absence of data
(no_history, no_baseline_window — the shape a host has right after its items are
recreated) collapse into "8 skipped for insufficient/low baseline". Two of the
three hosts in that incident landed there.

The second reason is the one that matters, because it survives the first being
fixed: a count reads as reassurance and names nobody, so a host that could not
be looked at is indistinguishable from one checked and found fine.

Hosts skipped for absence of data are now named, bounded to five plus a count,
with "Absent from this result means unmeasured, not healthy". Only the two
no-data reasons are collected — below_floor and healthy are real verdicts
reached by looking, and sweeping them in would bury the signal in the noise this
fixes. The note is appended to BOTH exits: a run that found two blocks and could
not examine a third has to say so, or the disclosure only shows up when nothing
else does.

Third tool to get this treatment after get_host (ADR 103) and
detect_traffic_shaping (ADR 107). Each was found in production rather than by a
test, because the broken version looks exactly like the healthy one.
943 -> 945 tests.

## [1.16.39] - 2026-08-14

### Fixed — eight copies of "find this host's traffic items", one of them blind
ADR 109. ADR 105 fixed traffic discovery in one tool and listed five other call
sites still searching by item NAME — blind to Zabbix's stock "Linux by Zabbix
agent" template, whose items are named "Interface enp3s0: Bits received" rather
than "Incoming network traffic on enp3s0". The plan was to fix them one at a
time.

Writing the guard that would stop a sixth copy appearing found THREE MORE the
list did not know about: analysis.py, geo_health.py, and traffic_shaping.py —
the tool ADR 105 had just fixed. Those were already key-based so they were not
blind, but that is eight independent copies of one rule, which is why a fix to
any one of them reaches only that one. Same drift ADR 078 fixed for the
physical-NIC predicate, one level up.

fetch.physical_traffic_items is now the single definition and all eight sites
call it. Discovery is by key, never by name. key_ is always requested
regardless of the caller's output list, because the filter needs it and a
caller that forgot would silently get everything back unfiltered.
is_physical_traffic_out_key was added so the outbound half of the fetch.py
fallback goes through the same rule rather than a seventh variant. A guard
fails the build if traffic is searched by name again, or if a raw
`*net.if.in[*` search reappears outside the helper.

Consolidation weakened an existing guard, and it said so: TestSearchWildcardGuard
scans INLINE LITERALS for the ADR 094 mistake, and building the helper's term as
an f-string made the last traffic search invisible to it — its own vacuity check
caught the drop. Rather than lower a threshold quietly, the terms are module-level
constants, the floor moved 4 -> 3 with the reason recorded inline, and the lost
assertion is made directly in the new guard.

Three sites gained a physical-interface filter they never had (geo_traffic,
dashboard_report, and the fetch.py fallback matched by name and kept
docker0/tun* alongside real NICs), so their numbers get more accurate — worth
watching on the first run. 935 -> 943 tests.

## [1.16.38] - 2026-08-14

### Fixed — a burst-tolerant policer reported as "not a cap"
ADR 108. An independent adversarial validation of ADR 104 ran controls the
original tests did not, and found one that failed: a token bucket.

A token bucket permits an occasional burst above its cap. With ~8% overshoot
every ~11 hours the burst becomes p95, the real cap then sits below the +/-2%
band around p95, and almost no hour counts as touching the ceiling — hit rate
~7%, verdict `dropped`: "peaks fell 62% ... but still spread ... demand or
reachability, not a cap". Reproduced before fixing (48h sine clipped at 120
Mbps, every 11th hour x1.08, against a 350-peak baseline).

That is the worst failure available to this tool. Burst tolerance is normal
policer configuration, so the shape most likely to BE a rate limit was the one
the detector denied, confidently, in the verdict wording.

The ceiling is now the modal point mass — the value where the most active hours
cluster — found by scanning every observed value as a candidate. p95 no longer
defines the ceiling; it still sets the active threshold, where an outlier is
harmless. Scanning every value is exact at these sizes, so there is no bin width
to tune. Ties prefer the higher value, so a cap reads as the cap and not as a
trough beneath it. The modal rule is MORE outlier-robust than p95: a lone freak
minute forms a cluster of one and always loses (pinned by test).

The constraint that shapes the design: a real sine spends more time near its
extremes, so a naive "most common value" rule could read a healthy daily peak
plateau as a cap. The uncapped-diurnal control must still read `normal`, and
does.

`ceiling_mbps` is now the cap rather than the burst, so `drop_pct` measured
against it is right too. Documented rather than fixed: a RATCHETING cap stepped
down more than once inside the recent window reads `normal` until it settles —
about one run of latency, self-resolving. 929 -> 935 tests.

## [1.16.37] - 2026-08-14

### Fixed — a host whose trend history was destroyed read as "normal"
ADR 107. Chasing the ADR 105 field report turned up two more hosts running at a
fraction of a sibling's throughput, each with freshly re-created items, so no
trend reached back past the recent window.

`classify_shaping` set `base_ceiling = None` for a missing baseline, which makes
the drop test unevaluable — and then fell through to `return NORMAL`, "peaks
spread normally". Verified against the previous commit: `classify_shaping(
varying_series, [])` returns `normal`. That is health asserted from no evidence.
`normal` is a COMPARATIVE claim, "against what came before, nothing changed",
and there was no before; a host whose history had just been destroyed rendered
identically to one that had been fine all along.

New verdict `no_baseline` covers that state. `capped` deliberately still fires
without a baseline: a ceiling is an observation about the recent window alone
and needs no past, so only the comparative half is withheld — withhold the
claims that need history, keep the ones that do not.

The tool also only COUNTED what it could not judge ("2 insufficient") in a
header line, which is the line a reader skips and never names a host. Hosts that
could not be judged are now named with how far back their history actually
reaches — `node-x (3h of history)` — plus why it matters: recreating a host's
items destroys its trend history, so a host that just lost its past reads the
same as one that never had a problem.

Same failure the reporting side hit twice (zabbix-reports ADR 0069, then 0073):
trends belong to the ITEM, so an item rebuild resets the measurement window and
every comparative detector inherits the blind spot. 925 -> 929 tests.

## [1.16.36] - 2026-08-14

### Fixed — an entire Zabbix template was invisible to every traffic tool
ADR 105. Reported from the field: three hosts with an obvious traffic anomaly,
and `detect_traffic_shaping` returned "No host is pinned against a throughput
ceiling". The data was not ambiguous — one ran 70-177 Mbps peaks until a cliff,
then 1-20 Mbps, a ~98% collapse. The tool had never looked at those hosts, and
its output gave no hint of that.

Two independent causes, both turning "not measured" into "nothing to report".
First, `is_physical_traffic_in_key` did `split("[",1)[1].rstrip("]")`, which
strips the bracket but not the quotes: Zabbix's stock "Linux by Zabbix agent"
template emits `net.if.in["enp3s0"]`, so the interface token was `"enp3s0"`
with the quote and failed `startswith(("eth","eno","enp",...))`. Every host on
the stock template was invisible to every traffic tool built on ADR 078's
shared predicate. Second, discovery searched item NAME ("Incoming network
traffic"), which the stock template spells "Interface enp3s0: Bits received".

The predicate now unquotes the token — one fix at ADR 078's single definition
restores every key-based consumer, and the virtual-interface exclusions are
pinned by test so the widening cannot smuggle in `docker0`/`tun0`/`veth0`.
`detect_traffic_shaping` discovers by key (`*net.if.in[*`), never by name:
item names are template cosmetics, the key is the contract. And a host in scope
that could not be examined is now NAMED — "absent from this table means
unmeasured, not healthy" — which is what would have caught both bugs on day
one, since the result looked clean.

Still name-based and still blind to the stock template: `tools/traffic.py:558`,
`tools/geo_traffic.py:59`, `tools/dashboard_report.py:119`,
`tools/traffic_erosion.py:307`, `fetch.py:533`. Listed in ADR 105 rather than
changed blind — four have their own scoping tests and a wrong move there
silently changes what the fleet reports.

### Added — cost falls back to an item when `usermacro.get` is revoked
ADR 106. ADR 103 made a denied macro read visible; it still left the reader
without the number. The monthly figure turns out to be published twice — as
`{$COST_MONTH}` and as the `Cost_macros_present` item — and reading an item
needs only host-group read, not `usermacro.get`, which a role can revoke
independently. Verified equal to the macro on two hosts (16.00/16 and
32.49/32.49) before being trusted.

When the macro yields no cost, `get_host` reads the item and renders
`**Cost/month:** 16 _(via item `Cost_macros_present`)_`. The source is always
carried: the item is a polled snapshot and can lag a macro edit, so passing it
off as the macro would make the fallback the next confident wrong answer. The
ADR 103 "Not shown" disclosure stays even when the fallback succeeds, so the
role problem does not get papered over by a working number. Only runs on the
miss path; an unparseable value is skipped, not guessed.

914 -> 925 tests.

## [1.16.35] - 2026-08-14

### Added — detect_traffic_shaping: separate a rate limit from lost demand
ADR 104. `detect_traffic_drops` (ADR 040) and `detect_traffic_erosion`
(ADR 091) both answer "is it lower". Neither answers "why", and that is the
question with a cost attached: **shaped** means open a provider ticket,
**demand** means do nothing. In an average the two are indistinguishable.

Shaping's fingerprint is not in the level, it is in the shape of the peaks. A
policer truncates the top of the distribution — every hour that wants more than
the cap reports exactly the cap, so the busy hours pile up on one value, while
ordinary demand reaches its maximum once and spreads out below it. The tool
therefore reads hourly `value_max` and never `value_avg`: a steady load has a
stable average and still-moving peaks, so averaging erases the only signal that
separates the two. A wire test asserts `value_max` is requested and `value_avg`
is not, because that regression would be silent.

The metric is the ceiling **hit rate** — of the hours where the host was
actually pushing (peak >= 50% of the p95 ceiling), what share sit within 2% of
it. Verdicts: `shaped` (ceiling fell >=25% AND hit rate >=60%), `capped` (hit
rate high, no drop), `dropped` (fell but peaks still spread — that is
`detect_traffic_drops`' story), `normal`, `idle`, `insufficient`. Both halves
are required for `shaped`, and with no usable baseline a host reads `capped`,
never `shaped` — the tool does not claim a change it could not observe.

Recorded in the ADR because it looks right and is not: the first metric measured
the SPREAD of the top quartile of hours. Selecting the largest values compresses
any distribution, so a perfectly healthy series scored as flat as a shaped one —
it fired on the first synthetic control tried. The healthy-varying and diurnal
controls are kept as tests for that reason.

`capped` cannot separate a hard rate limit from genuinely constant demand;
throughput does not carry that information, so the verdict is worded as an
observation and listed apart from `shaped`. Added to the `ops` tier (59 -> 60).
166 -> 167 tools, 896 -> 914 tests.

## [1.16.34] - 2026-08-13

### Fixed — a swallowed enrichment failure looked exactly like an absent value (ADR 103)
Reported from the field: `get_host` returned no cost for hosts that have one,
on a non-Super-admin token. The Zabbix rule is simple enough — host cost lives in
the `{$COST_MONTH}` user macro, so reading it needs `usermacro.get` IN ADDITION
to host-group read access; a token that lists hosts perfectly can still return
nothing for macros, because those are separate permissions.

The defect was ours. ADR 099 wrapped each enrichment block in `try/except … pass`
so a failure could never break the identity lookup — right intent, but `pass`
made a permission error and "this host has no cost macro" produce byte-identical
output. Three different situations (not set / may not read / API method revoked)
rendered the same, and a reader will assume the mundane one.

Enrichment stays best-effort but is no longer silent: failures record into
`ctx["_unavailable"]` and render under a **Not shown** heading stating they are
missing because a call FAILED, not because the value is absent — with where to
look (host-group read permission, role API-method list). Applied to cost/bandwidth
macros, current traffic and service-check state. README's token section now
documents the three gates (host-group read, role API methods, Secret macro type)
and that Super admin bypasses host-group permissions, which is why the same tool
works for one colleague and not another. +1 test, 895 -> 896.

## [1.16.33] - 2026-08-11

### Fixed — compare_report_facts cried wolf on population drift (ADR 101 addendum)
The first live run of `compare_report_facts` against the reporting side's real
`crosscheck.json` reported `DIVERGENCE — one side is wrong` and told the operator
not to assume the report was stale — while EVERY per-country count matched
exactly (72 countries, DE 116, US 117, …) and only the aggregate counts differed
(total 943→951, blank 2→19, countryless 272→262). That is backwards, and exactly
the cry-wolf ADR 101 exists to prevent: when per-country resolution provably
agrees, an aggregate difference is fleet drift or a naming lag between the runs,
not a resolution defect. New `POP_DRIFT` verdict downgrades the aggregate
population counts (total_hosts / country_host_sum / countryless_by_design /
blank_country_hosts) when every compared per-country count and the distinct-country
total match; `countries` is not downgraded (a distinct-count change is itself a
resolution signal), and a genuine per-country mismatch or absent per-country
evidence keeps `DIVERGE`. Verified: the observed 943↔951 now reads POP_DRIFT with
a "resolution agrees" overall verdict. Also surfaces the LIVE blank-country
sample, not just the reported one, so the follow-up names hosts you can fix.
+4 tests, 891 → 895.

## [1.16.32] - 2026-07-30

### Security — cryptography Bleichenbacher oracle, plus a second advisory the sweep found (ADR 102)
Reported: **GHSA-g6cj-pr64-35w5 / CVE-2026-69247** (High) — cryptography PKCS#7
EnvelopedData decryption exposes a Bleichenbacher oracle through distinguishable
errors and timing; vulnerable `>= 44.0.0, < 50.0.0`, the lock carried 49.0.0.

**Not exploitable here:** nothing in `src/` or `tests/` imports cryptography at
all, let alone the PKCS#7 decryption API, and a padding oracle needs an attacker
able to submit chosen ciphertexts to a decryption endpoint — this server exposes
none. The package is present transitively via `pyjwt[crypto]` <- `mcp`, and as a
declared security floor from an earlier advisory. Bumped regardless, on ADR 083's
reasoning: a dependency we ship is supply chain whether or not our call graph
reaches it, and "not exploitable today" has an expiry date. Floor raised
`>=46.0.7` -> `>=50.0.0` so a future resolution cannot drift back.

**The sweep found a second one, closer to us than the reported High.** Following
ADR 083 (where the reported CVE was only the tip), the whole lock was checked
rather than just the named package. `pip-audit` cannot run on this machine — its
isolated-venv builder dies in `ensurepip` regardless of flags — so all 117
runtime packages were queried against the GitHub Advisory Database, the same
source that raised the alert. That surfaced **GHSA-6hr6-w5qg-qmwg** (medium):
h2 `<= 4.4.0` duplicate-`Host`-header request smuggling. h2 arrives via
`httpx[http2]` and this client sets `http2=True`, so unlike the reported High
the vulnerable code sits directly on our request path. Re-locked 4.3.0 -> 4.4.1
(with hpack 4.2.0). Deliberately NOT promoted to a declared dependency: it is
transitive, nothing caps it, the lock already pins it, and inventing a direct
dependency to floor a transitive package becomes the next over-tight cap — the
exact ADR 082 anti-pattern where our own ceiling blocked the fix we needed.

Verified after both changes: **0 advisories across all 117 runtime packages.**
891 tests, lint and typecheck unchanged.

## [1.16.31] - 2026-07-30

### Added — `compare_report_facts`: cross-system fact diff (ADR 101)
A separate reporting pipeline runs overlapping analytics against the **same**
Zabbix instance — erosion, uptime/SLA, dark-host detection, country resolution —
maintained independently with findings hand-ported one way, and nothing verified
the two agree. Both codebases have shipped defects in the same areas (country
resolution: ADR 093/100 here, 0052 there; dark-host: ADR 100 / 0054; integer
uint trends: ADR 092 / 0048), so a divergence introduced on either side would sit
in a scheduled report until a human noticed two different numbers in two
documents. The concrete near-miss: this server honours `ZABBIX_TRAFFIC_UNIT` and
the reporting side deliberately does not, so if the fleet's items ever switch to
bytes/sec one system's figures move 8x and the other's do not.

The reporting side already publishes its comparable figures to JSON specifically
so this side is a diff rather than a re-derivation. New read-only tool reads
them, recomputes the same quantities live, and renders a per-field diff.

**The load-bearing decision is what it refuses to judge.** Diffing every
recognised field would immediately cry wolf, because some quantities share a name
and are computed to different definitions on purpose — and an invariant that
fires on correct data is one nobody reads, which is the failure mode this exists
to prevent. So: *strict* grading only where definitions are provably identical
(host population + country resolution, same helpers, same host list), and
*advisory* — shown but never judged — for same-named figures whose definitions
differ (uptime/SLA is period-integrated there, point-in-time here per ADR 097) or
whose thresholds are unpublished (erosion counts). Plus: drift is separated from
divergence (the fleet legitimately changes between runs), the facts file's age is
always disclosed from its mtime since it carries no timestamp of its own, and the
countryless-product set is mirrored exactly and pinned by a test — if the two
sets drift, both systems count "missing country" over different populations and
the diff reports a defect that does not exist. Caller paths go through
`confined_input_path` (ADR 076). Added to the `ops` tier. Tool count 165 -> 166.
+18 tests, 873 -> 891.

## [1.16.30] - 2026-07-28

Independent audit of the ADR 093-099 change set (~30 files, previously
unreviewed). Two of those fixes had introduced the same class of defect they
were written to remove; both are fixed here, with the cases that would have
caught them.

### Fixed — the country allow-list erased ~50 real countries (ADR 100)
ADR 093 made `ISO2_CODES` load-bearing (a code outside it is discarded as "not
a country"), but derived it from the ISO-3/English NAME table, which covers
only 200 codes. The ~50 alpha-2 codes with no name entry -- mostly dependencies
and overseas territories -- therefore became invalid: a host in one resolved to
a blank country everywhere `extract_country` is used, dropping silently out of
every country roll-up across ~60 call sites, and `normalize_country` rejected a
genuine ISO-2 code as unknown input. The allow-list is now the union of the
name table and an explicit territory set (250 = 249 official + `XK`), pinned by
a test. The ADR 093 property is unchanged: a non-ISO tag is still rejected.

### Fixed — detect_disruption_wave went blind to total outages (ADR 100)
ADR 098 required an interface to report in BOTH windows, so a renamed item
could not inflate the baseline side and manufacture a drop. But a host that
goes completely dark has NO recent rows, so it was skipped entirely and never
analysed -- precisely the mass-outage case the tool exists to find. The
accompanying test covered only the multi-NIC variant, which is how it shipped.
Aggregation now goes through one shared pure `aggregate_host_windows`: a bond
is not additional to its slaves (no double count), only interfaces in both
windows contribute (no phantom drop), and total silence is an outage with
recent = 0.0 (not an absent host). Independent NICs sum rather than max -- two
active 3 Mbps cards genuinely carry 6, and a max would drop that host under a
5 Mbps floor.

### Fixed — get_trends was only half-bounded (ADR 100)
ADR 096 bounded the default call, but an explicit `time_from` left `time_till`
open while still sending `limit`, which the server applies to an ascending
scan -- so the caller got the OLDEST rows of the range, now sorted newest-first
and therefore more misleading than before. The window now anchors on its end
and walks back far enough to hold `limit` rows.

### Changed — seasonal_floor min_samples scales with the widened bucket (ADR 100)
ADR 097 widened the bucket to the target hour +/-1 but left `min_samples` at 3,
a threshold set for a bucket a third of the size -- so a floor could form from a
single day's three consecutive hours. Raised to 9 (3 hours x 3 days).

Also: the ADR 098 traffic guard scanned only `tools/` despite claiming
tree-wide coverage (now the whole package, with named-constant definitions
exempted); the aggregation test imports the shared rule instead of restating
it; two tautological assertions replaced; stale CLAUDE.md module row fixed.
870 -> 872 tests.

## [1.16.29] - 2026-07-28

### Added — investigation-ready `get_host` (ADR 099)
`get_host` rendered name, id, status, groups and interfaces and nothing else,
so the tool that exists to answer "tell me about this host" cost three further
calls before it answered anything -- while already fetching `output: "extend"`
and discarding the rest. It now also returns country (resolved from inventory,
which rides along on the call already being made), product/tier, provider,
datacenter, current inbound traffic, service-check state, cost and bandwidth
macros, and linked templates. Every enrichment block is individually
best-effort -- a macro or traffic failure can never turn a working identity
lookup into an error -- and `brief=True` skips the extra calls entirely.

### Fixed — an empty country filter no longer asserts absence (ADR 099)
`search_hosts` and `search_hosts_by_location` returned a flat "no hosts found"
when a country filter matched nothing. That conflates "there are none" with
"their country cannot be derived" -- and the confident empty reads as fact.
ADR 093 fixed the extractor bug behind the observed case, but the ambiguity is
structural: any naming convention the parser cannot read reproduces it. Both
tools now append a note naming the hosts whose name looks like the requested
country but does not resolve to it, and pointing at the likely inventory gap.
The loose matcher is used only to EXPLAIN an empty result, never to assign a
country, so a false positive costs a hint rather than a wrong verdict.

### Changed — pinned ruff in CI and the project (ADR 099)
The workflow installed `ruff` unpinned while the project floated `ruff>=0.4`,
so CI silently ran a newer version with newer rules: a commit could lint clean
locally and fail CI, which is what happened with B033 (the same
pass-local/fail-CI class as ADR 084). Both now pin the identical version.
+13 tests, 857 -> 870.

## [1.16.28] - 2026-07-28

### Fixed — one traffic conversion everywhere, and two defects it exposed (ADR 098)
ADR 087 established a single raw->Mbps divisor honouring `ZABBIX_TRAFFIC_UNIT`,
but routed only `get_peak_analysis` through it: 24 call sites across 9 modules
kept dividing by a literal `1e6`. That is right only on the default bits/s
config; under bytes/s every one reads 8x low. Ratio verdicts survive, absolute
floors do not -- a host genuinely doing 30 Mbps reads 3.75, falls under the
5.0 Mbps `min_baseline` gate and is dropped from block analysis with no row and
no warning (the same floor guards `detect_disruption_wave` and the acute
regional detector). Added named `to_mbps`/`to_kbps`/`from_mbps` helpers, swept
every site, and widened the regression lock from one file to an AST sweep of the
whole tool tree. The one genuine literal collision -- a memory rate that also
divides by 1_000_000 -- is resolved by naming it (`MB_DECIMAL`/`GB_DECIMAL`).

Fixing this surfaced two further defects. `import zbbx_mcp.fetch` as the FIRST
import raised ImportError: data and fetch import each other and data re-exported
fetch's symbols eagerly, so importing fetch first hit a partially-initialised
module -- it had never fired only because every entry point happens to import
data first. Broken with a lazy PEP 562 `__getattr__`, with a TYPE_CHECKING block
keeping the names visible to linters without a runtime edge. And a sixth
instance of the ADR 094 wildcard bug: `get_predictive_alerts` holds its search
term in a config dict, so the inline scanner could not see it -- its disk metric
queried an exact key, fetched ZERO items, and the whole disk forecast never ran
(with the ADR 097 sign inversion, disk prediction was dead twice over). The
wildcard guard now also covers the variable-built form.

Also: `detect_disruption_wave` summed every matching NIC per host, but `bond0`
IS its slaves, so a bonded host double-counted and the absolute floor admitted
hosts at half the intended threshold; and baseline/recent were accumulated
independently, so an interface with baseline rows but no recent rows inflated
only the baseline side and manufactured a drop that never happened. Now max
across interfaces, over interfaces present in both windows. +11 tests, 845 -> 856.

## [1.16.27] - 2026-07-27

Review batch: five root causes behind a class of defect that returns a
confident wrong answer instead of an error. Eight were confirmed against a
live instance. No tool-count change (165).

### Fixed — country extraction picked a tag over the real country (ADR 093)
`extract_country` scanned one alternation with `re.search`, which returns the
LEFTMOST match, so a datacenter/role/market segment beat the indexed
`<cc><digit>` suffix that actually encodes location. Where that tag happened to
be a real ISO code, hosts were reported in a country they have no presence in —
verified live, a country filter returned several hosts, carrying real traffic,
that are physically elsewhere. Where it wasn't a real code, the bogus-but-truthy
value short-circuited `resolve_country`'s inventory fallback, so correct
inventory data could not fix the host. Now the indexed form is tried first
regardless of position, and only a real ISO 3166-1 code is ever returned (the
allow-list is derived from the existing reference table). `normalize_country`
validates two-letter input instead of echoing any two letters back.

### Fixed — `searchWildcardsEnabled` made bare search terms EXACT (ADR 094)
With the flag set, Zabbix stops wrapping the term in `%…%` and only translates
`*`. Five hardcoded literals therefore matched nothing and rendered complete,
plausible answers built from zero rows: `analyze_server_roles` classified the
ENTIRE fleet as "idle" at 0.0 Mbps; `get_service_uptime_report`'s per-hour
traffic gate (ADR 081) never engaged; `get_low_disk_servers` saw no `vfs.fs.*`
items; `get_web_scenario_status` could never report a failure; and
`fetch_traffic_map`'s tag-based NIC discovery was dead. Wildcards are now
explicit, and an AST guard fails the suite on any literal search term without
one.

### Fixed — audit resource/action tables were offset from Zabbix (ADR 095)
Verified live: rows the code labelled "Trigger" named hosts, and rows labelled
"Host group" named items — the whole table was shifted, so every row borrowed a
neighbouring object class's label while `resource=` filtered on a different
class than requested. Four modules also inlined the host resource type as `2`,
which is not an assigned type, so those filters matched zero rows and
`get_external_ip_history` reported no rotations ever. Corrected to the 6.0+
constants, named once in `data.py` and used everywhere; an unmapped code now
renders `Type N` rather than borrowing a wrong label.

### Fixed — sortfield, removed write params, unread output, trend order (ADR 096)
`mediatype.get` was sorted by `name`, which is not a sort column — a hard -32500
on every call, so `get_media_types` never worked. `maintenance.create` passed
`hostids`/`groupids`, removed in 7.2, so the tool was broken outright.
`get_alert_summary` never requested the `clock` its current/previous split reads,
so every row counted as current and the default `compare=True` window silently
covered twice the stated period. `get_trends` passed sort params `trend.get`
ignores, so `limit` returned the OLDEST retained hours. Also: `map.get` select
params don't support `"count"` (counts always rendered `?`), and `nextcheck` is
not a web-scenario property. Both time arguments now accept `YYYY-MM-DD` via the
shared `parse_time` instead of raising on it. New sortfield allow-list guard.

### Fixed — analytics correctness cluster (ADR 097)
Eleven calculation defects that produced plausible numbers: the `pused` disk
predictor was sign-inverted (trend left as used-% while the current value was
flipped to free-%) making it all false negatives on a `pused` fleet; `or 100`
ranked a 0%-uptime host as perfectly healthy and thereby broke the worst-wins
canonical fold; the health matrix could render a >100% ratio; the CPU floor was
derived from hourly means rather than maxima; `history: 0` blinded the
blast-radius tool to unsigned-integer items; unjudged and agent-down hosts were
counted as "healthy"; peak analysis picked the first interface rather than the
busiest; the seasonal floor was effectively the bucket minimum, so one freak-low
hour permanently silenced that hour-of-day; the country summary described the
post-filter subset and labelled a median "Avg"; trend direction was garbage below
four points; and the in-progress hour always scored DOWN. `get_predictive_alerts`
moved to a new `tools/predictive.py` (`executive.py` hit its size budget).

## [1.16.26] - 2026-07-24

### Fixed — uptime under-count: read value_max, not integer-truncated value_avg (task 175)
ADR 092. `get_service_uptime_report` computed hourly uptime from service-check
trends using `value_avg`. A service check is a 0/1 unsigned-int item, so its
trends live in `trends_uint`, whose columns are integer (bigint): Zabbix stores
the hourly average `sum/count` truncated toward zero, so an hour up 59 of 60
minutes reads `value_avg=0` and was scored a full outage — a 60x over-penalty
that systematically understated uptime and could read a flappy-but-reachable
host near 0%. Now requests `value_max`: up-hour = `value_max>=0.5` (protocol
responded at least once), down-hour = `value_max=0` (fully dark); a partial
hour is a flap (that dimension is `detect_check_flaps`' job), not a full down.
The traffic gate keeps `value_avg` (accurate for the large-uint NIC counter).
`compute_host_uptime` is unchanged; its per-hour input is now documented as an
up-indicator with the truncation reason inline. `get_sla_dashboard` is
unaffected (point-in-time `lastvalue`); `get_trends` needed no table change
(`trend.get` returns `trends_uint` rows — the earlier empty result was a
date-arg parse error). +2 wire tests, 816 -> 818.

## [1.16.25] - 2026-07-24

### Added — detect_traffic_erosion: cohort-relative multi-week slow-decline detector
ADR 091. `detect_traffic_drops` (ADR 040) is acute — it compares a recent window
against a 7-day seasonal baseline and fires on a large same-hour drop — so it is
structurally blind to a *gradual* multi-week decline, where each day sits only
slightly below the trailing week yet the host bleeds most of its traffic over a
couple of months (gradual reachability loss, effectiveness decay, slow demand
rot). Hourly-trend uptime smooths the shape away and country-aggregated geo
trends hide a subset eroding inside a healthy region, so nothing in the suite
covered this class.

New read-only `detect_traffic_erosion` (group/product/country/region scope,
`weeks` <= 12) fits a slope to each host's weekly-mean throughput and judges it
**cohort-relative**: a host is flagged as eroding only when it declines
materially faster than its scope's median slope, so a fleet-wide demand dip that
drags every host down together is labelled `demand`, not erosion. Verdict
priority: insufficient (< 4 weekly points) -> idle (peak below the Mbps floor,
the denominator rule) -> recovering (a rise is never a drop) -> eroding (>= the
decline threshold AND faster than the cohort) -> demand (declining, tracking the
cohort) -> stable. A scope with fewer than 3 non-idle peers has no meaningful
cohort (a one-host median is that host's own slope), so it falls back to an
absolute-decline verdict and says so. Interface selection, the traffic-unit
divisor, and test-host exclusion reuse the acute detector's machinery. Output
ranks the declining set (eroding before demand), states the cohort median, and
counts the rest. Added to the `ops` tier. Tool count 164 -> 165. +19 tests,
797 -> 816.

## [1.16.24] - 2026-07-21

### Added — detect_check_flaps: minute-level flap matrix (noise/real split)
ADR 090, task 174. A minute-level flap audit proved three things triggers
cannot see: same-minute dips on geographically distant hosts are prober/egress
noise (must be subtracted before scoring any host); a TEST-class check flaps
several times more than the production check on the same host (script noise,
weight ~0); and chronically degraded hosts -- prod check failing in most hours,
1-2 minutes at a time -- fire ZERO triggers, because consecutive-fail triggers
are blind to short dips (and hourly trend uptime smooths them away).

New bounded, read-only `detect_check_flaps` (hosts/group/country scope,
max_hosts <= 12, window <= 7d) pulls raw minute history for the configured
service checks and classifies every flap-minute in priority order: fleet-
correlated (>=2 distant countries, or >=3 hosts unknown-country) -> discard;
host-correlated (>=2 prod services, one host) -> real event; TEST-only ->
tracked at weight 0 (and TEST dips can never fabricate fleet noise); residual
prod flaps -> honest rate/day. Output: ranked matrix + noise summary +
rate-based trigger candidates (chronic rate, zero problem events -- the hosts
alerting misses). Scoped sweeps drop test hosts (ADR 080 semantics); the
operator follow-up (a rate-based Zabbix trigger) is deliberately left to the
operator. Tool count 163 -> 164. +14 tests, 783 -> 797.

## [1.16.23] - 2026-07-17

### Added — shared exclude_test seam; more fleet verdicts drop test boxes
ADR 089. ADR 080 wired test-host exclusion into 6 tools; most fleet-verdict
tools still counted test boxes (the tell: detect_traffic_anomalies sat in the
same file as the already-fixed detect_traffic_drops, still unfiltered).

Rather than hand-thread include_test into ~20 more tools (error-prone, and a
blanket sweep would wrongly hide test hosts from search_hosts / maps / CRUD
where they should show), added the exclusion to the shared seam:
fetch_enabled_hosts gains exclude_test (forces groups=True since a test box is
often in a production group, filters via is_test_host, cache-keyed, logs the
count). Wired detect_traffic_anomalies with the full named-note treatment
(matching detect_traffic_drops), and the whole-fleet reports generate_ceo_report,
generate_service_brief, get_expansion_report via the seam. The direct-host.get
verdict tools (inventory_load lists, floods, disruption detectors) are a
deliberate deferred batch. Tool count unchanged (163). +4 tests, 779 -> 783.

## [1.16.22] - 2026-07-17

### Fixed — silent-degradation cluster (removed/renamed API fields)
ADR 088. Four Zabbix fields removed/renamed in modern versions that this 7.4.9
instance *silently ignores* rather than rejecting — so a column just went blank
instead of erroring:
- `host.get` output `available` (removed 6.0, now `active_available`, same 0/1/2
  encoding) -> the `[available]` tag and "agent unavailable" count never
  populated. Fixed in search_hosts + get_capacity_planning + format_host_list.
- `discoveryrule.get` output `lastclock` -> LLD rules have no last-poll field, so
  the "last run" column showed a permanent 1970. Dropped.
- `item.get selectHosts: [..., "groups"]` in get_domain_status -> groups can't
  nest in a selectHosts subquery, so the domain group column was always empty.
  Now resolved via a separate host.get with selectGroups.
- `maintenance.get selectGroups` (renamed selectHostGroups in 7.0) -> added
  maintenance.get to the client's 6.x<->7.2 shim for portability.

Extended the output-field guard (ADR 085) with host.get/discoveryrule.get so
neither removed field can return. Tool count unchanged (163). +5 tests,
774 -> 779.

## [1.16.21] - 2026-07-17

### Fixed — traffic-unit conversion: get_peak_analysis 8x, bytes divisor 64x
ADR 087. Two conflicting bytes->Mbps conversions had drifted apart.
`get_peak_analysis` hardcoded `value * 8 / 1_000_000`, but the repo default is
bits/s (everything else divides by TRAFFIC_DIVISOR=1e6) -- so on a standard
deployment it reported **8x the true Mbps** and disagreed with every other tool
for the same item. Separately the bytes-mode divisor was 8_000_000, but
bytes/s->Mbps is /125_000, so it was 64x too low (latent, config-gated).

Routed get_peak_analysis through the shared TRAFFIC_DIVISOR and fixed the bytes
divisor to 125_000 -- one conversion, correct in both bits and bytes mode,
consistent across all tools. The peak/trough ratio was always right (both
endpoints scaled together); only the absolute Mbps labels were wrong. Tool
count unchanged (163). +4 tests, 770 -> 774.

## [1.16.20] - 2026-07-17

### Fixed — month-boundary sort scrambled daily-trend verdicts
ADR 086. Daily trend keys were formatted `"%b %d"` ("Jul 03"), which sorts
*lexically* — and `"Jul" < "Jun"`. So any window crossing a month boundary
(essentially always for the 30d default) scrambled every `sorted(daily)` that
was treated as time order, silently flipping verdicts: `get_traffic_drop_timeline`
took the wrong day as "today" and skipped genuinely-blocked countries (a
false negative in blocking detection); `detect_regional_anomalies` split
recent/baseline on the scrambled order so real drops read as 0%;
`get_executive_dashboard` computed growth backwards; geo-trend and CEO-report
trend direction were affected too.

Fixed at the source: keys are now `"%Y-%m-%d"` (ISO, sortable, year-carrying),
so every consumer is chronological by construction, with a `day_label` helper
rendering the compact "Mon DD" label at display. `get_month_over_month` now
parses ISO directly, dropping its year-guess. Tool count unchanged (163).
+4 tests, 766 -> 770.

## [1.16.19] - 2026-07-17

### Fixed — get_users was dead on every call (-32602)
ADR 085. `user.get` requested the removed `type` field (Zabbix 5.2 replaced
user types with role-based `roleid`) plus `rows_per_page`, so the API rejected
the whole call with -32602. Fixed the output to valid 7.x fields and resolve
role names via `role.get` (best-effort, falls back to the raw id).

Two guard blind spots let this ship: no guard validated the top-level `output`
list, and get_users built its params in a *variable* that the inline-only AST
scanners never saw. Added an output-field guard (`DENIED_OUTPUT_FIELDS`) whose
scanner resolves variable-built params by nearest-preceding assignment. Tool
count unchanged (163). +7 tests, 759 -> 766.

## [1.16.18] - 2026-07-17

### Fixed — stdio-shutdown race in the server subprocess test (CI-only)
ADR 084. After the mcp 1.28.1 bump (ADR 083), CI failed on
`test_server.py::test_tools_list` — but only in CI, and only on 1.28.1; locally
it passed every time including ten back-to-back runs. The harness batch-wrote
the JSON-RPC messages and closed stdin immediately (`subprocess.run(input=...)`),
so the server saw `initialize`, `notifications/initialized`, `tools/list` and
then EOF at once. The hardened SDK tears the stdio session down on EOF, and that
teardown raced the pending `tools/list` handler: the handler won on a fast local
machine, the shutdown won on a slow/loaded CI runner.

The server was correct; the harness was not. `_run_jsonrpc` now spawns the
server with `Popen`, drains stdout on a reader thread, and holds stdin **open**
until every request id has a matching response before closing it — so the
EOF-driven shutdown has nothing left to race. `stderr` goes to `DEVNULL` to
avoid a full-pipe deadlock. Test-only change; suite unchanged at 759.

## [1.16.17] - 2026-07-17

### Security — mcp 1.28.1 + click 8.4.2 (second CVE round), and a cap fix
ADR 083. Auditing the lockfile right after ADR 082 surfaced two more advisories
plus a self-inflicted one:

- **CVE-2026-59950 (High)** — mcp's deprecated WebSocket transport accepted the
  handshake with no Host/Origin validation (fixed 1.28.1). We are **not
  exploitable** (that transport is not reachable through FastMCP and we never
  wire it up), but the vulnerable code shipped in the pinned version.
- **PYSEC-2026-2132 (High)** — command injection in `click.edit()` (transitive
  via uvicorn, fixed 8.3.3). We do not call it, but the version was in the tree.
- **ADR 082's own cap blocked the fix.** Its `mcp>=1.27.2,<1.28.0` bound
  excluded 1.28.1 within the hour -- the exact over-tight-cap anti-pattern that
  ADR warned about.

Raised the constraint to `mcp>=1.28.1,<2.0.0` (1.28.1 clears both mcp CVEs; the
bound is widened to the major boundary so a future 1.x security patch is not
blocked by us again -- safe because our only private-API coupling degrades
gracefully and the subprocess handshake test gates real breaks). Bumped click
to 8.4.2 lockfile-only. A pip-audit over the re-locked tree now reports no known
vulnerabilities. Tool count unchanged (163); suite unchanged at 759.

## [1.16.16] - 2026-07-17

### Security — bump `mcp` for CVE-2026-52869 (HTTP transport principal check)
ADR 082. CVE-2026-52869 / GHSA-jpw9-pfvf-9f58 (High): the MCP Python SDK's HTTP
transports (SSE, streamable-HTTP) served session requests without verifying the
authenticated principal. Affected `mcp <= 1.27.1`, fixed in 1.27.2.

The server defaults to stdio (not affected), but it also exposes
`--transport sse` and `--transport streamable-http`, so any operator running an
HTTP transport was exposed. Worse, our own pin `<1.26.0` (dating to v0.2.0) was
actively blocking the patched release — an over-tight cap that had become a
security liability.

Raised the constraint to `mcp>=1.27.2,<1.28.0` and re-locked (1.25.0 -> 1.27.2).
The bump crosses two minors and we depend on FastMCP internals (the
compression/logging layer walks `_tool_manager._tools` and rebinds `tool.fn`),
so the risk was compatibility: verified that surface still holds under 1.27.2,
and the subprocess JSON-RPC handshake test exercises the real dispatch path.
Tool count unchanged (163); suite unchanged at 759.

## [1.16.15] - 2026-07-14

### Fixed — per-hour traffic gate + test-pattern gaps
ADR 081. (1) The uptime traffic gate (ADR 075) was a window-wide boolean, so
a host that served for a week and then hard-died mid-window read ~100%
instead of ~50% — the task-168 inflation through the side door.
`compute_host_uptime` now takes a set of traffic hour buckets (built by the
new `traffic_hours_from_trends` from physical-NIC trends over the same
window); a missing check-hour is rescued only if THAT hour had traffic.
(2) The ADR 080 test pattern missed dot separators (`a.test.b`) and numbered
test boxes (`x-test2-y`) that the sibling pipeline excludes — default is now
`(?:^|[-_.\s])test\d*(?:[-_.\s]|$)`. Tool count unchanged (163).
+9 tests, 750 -> 759.

## [1.16.14] - 2026-07-14

### Added — test-host exclusion wired into the remaining fleet verdicts
ADR 080 (completing v1.16.13). `get_service_uptime_report`,
`get_service_health_matrix`, `get_at_risk_hosts` and `bulk_diagnose` now also
exclude test/staging hosts by default, each gaining `include_test: bool =
False`; several now request `selectGroups`, without which half the detection
signal is unavailable. Every one of them names the hosts it dropped rather
than dropping them in silence.

One deliberate exception: `bulk_diagnose` drops test boxes only from a *scoped*
sweep (`group`/`country`). A host named explicitly in `hosts` is always
diagnosed -- naming it is the request to look at it, and silently returning
nothing would be the worst possible answer. Tool count unchanged (163).
+3 tests, 747 -> 750.

## [1.16.13] - 2026-07-14

### Added — test/staging hosts excluded from fleet verdicts
ADR 080. Non-production boxes were monitored alongside production ones with
nothing to tell them apart, so a test box landed in every fleet-wide verdict it
was scoped into: padding "analysed N servers" counts, adding phantom failures
to protocol sweeps, and dragging on uptime aggregates.

Classifying by host group does not work here: the test boxes are full members
of **production** groups while the instance's test groups go unused — a group
check would miss exactly the hosts that cause the damage. A bare `"test" in
name` substring is wrong too; it swallows `latest`, `contest`, `fastest`.

New pure `is_test_host()` applies one **token-bounded** pattern to the host
name **and** to every group name, taking the union — neither signal is reliable
alone. The separator class includes whitespace (group names use spaces where
host names use dashes). Overridable via `ZABBIX_TEST_NAME_RE`; an invalid regex
falls back to the default rather than crashing.

`partition_test_hosts()` splits rather than filters, and `excluded_test_note()`
names what was dropped — an invisible skip is the class of bug ADR 011 exists
to kill. Wired into `search_items` and `detect_traffic_drops`, both gaining
`include_test: bool = False`. Tool count unchanged (163). +23 tests, 724 -> 747.

## [1.16.12] - 2026-07-13

### Added — docs guard: no deployment magnitudes in public docs
ADR 079. Documentation prose written against a running system absorbs that
run's numbers (host counts, subnet spreads, regional footprints). Those are one
execution's output rather than facts about this codebase: they date instantly
and a reader cannot verify them. A string deny-list cannot catch the class at
all — numbers and ISO country codes are invisible to it by construction.

Added a guard over `docs/adr/*.md`, `CHANGELOG.md`, `README.md` and
`CLAUDE.md` covering `fleet of <n>`, observed host/server/cluster counts,
deployment-scale (3+ digit) counts, subnet-spread counts, and regional
footprints (two or more ISO-2 codes in a row, validated against the repo's own
ISO-3166 dataset). Configured thresholds and caps are design facts about this
codebase and keep passing. Documentation now describes scale qualitatively;
the reasoning in an ADR is what carries its value, and magnitudes were only
ever illustration. +3 tests, 721 -> 724.

## [1.16.11] - 2026-07-13

### Fixed — diagnose traffic: collapsed baseline window + carrier dilution
ADR 078. Found while cross-checking a support report: `diagnose_host` printed
"No traffic items / trend data available" for hosts visibly moving tens of
Mbps, which `detect_traffic_drops` analysed fine. Three defects:

1. The baseline window **collapsed** whenever `traffic_hours >= 24`
   (`baseline_from` was pinned to `now-24h` while `baseline_till` was
   `now-traffic_hours`, so the range went empty at 24h and inverted at 168h).
   The baseline came back `None` and the **`traffic_lost` verdict became
   unreachable** — widening the window silently degraded a dead host to
   `healthy`. The default of 6h happened to work, which is why it survived.
2. **Carrier dilution:** traffic was a flat mean across *every* NIC's trend
   rows, so a busy carrier beside idle NICs read low by the idle count (live:
   `bond0` ~60 Mbps + idle `eno4` → reported 30.1 Mbps).
3. `diagnose` and `fetch_traffic_map` used **two different definitions** of
   "traffic item" (exact hardcoded key list vs glob + physical-NIC prefix), so
   the two tools disagreed on which NICs counted.

Fixed with three pure helpers: `_traffic_windows` (baseline always abuts the
recent window, never collapses at any width), `_carrier_traffic_mbps` (per-NIC
means; the busiest baseline interface is the carrier and *both* windows are
measured on it, so an idle peer neither dilutes the figure nor masks a
collapse), and a shared `is_physical_traffic_in_key` now used by both paths.
Tool count unchanged (163). +11 tests, 710 → 721.

## [1.16.10] - 2026-07-13

### Fixed — `get_problem_detail` was dead on every problem (-32602)
ADR 077. `problem.get` asked for `selectAcknowledges: [..., "alias", ...]`,
but `alias` is not a field of the acknowledge object — it was the pre-5.4
*user* field (renamed `username`). Zabbix rejects the entire call with
-32602, so the tool failed on every input, not just acknowledged problems.
Found live while triaging a real problem. A second bug rode along: the
renderer printed `a.get('alias', '?')`, so the acknowledgement author would
have shown as `?` forever even if the API had accepted the request.

Fixed the requested fields, and restored the author via a best-effort
`user.get` lookup (`userid` → `username`); a token without `user.get` rights
falls back to `user <id>` instead of crashing, and no lookup fires when a
problem has no acknowledgements.

The ADR 072 guard missed this because it checked parameter *names*, not the
field *values* inside them. Added a **select-field guard** that AST-scans the
literals inside known `select*` lists against the sets Zabbix accepts, plus a
not-vacuous test so it cannot pass by failing to look. Both -32602 shapes are
now CI failures. Tool count unchanged (163). +6 tests, 704 → 710.

## [1.16.9] - 2026-07-09

### Security — filesystem confinement for caller-supplied paths
ADR 076. Validated the repo against advisory GHSA-99mq-fjjc-6v9j
(CWE-22/CWE-73 path traversal in a sibling MCP). The same root cause was
present in a weaker form: tools taking a caller `file_path`/`source_xlsx`/
`log_path` (`audit_external_ips`, the cost/billing importers,
`export_cost_audit`, `get_telemetry_summary`) read it with only an
existence check, so a prompt-injected caller could read `~/.ssh`,
`~/.claude.json`, `/etc/*`, etc. Added a shared confinement layer in
`utils.py` — `realpath` (symlink-safe) + `commonpath` (sibling-prefix-safe)
against an allowlist of roots (`~/Downloads`, `~/Documents`, `~/Desktop`,
temp; extend with `ZBBX_FILE_ROOTS`), a read size cap, and a filename
guard. Every caller read/write path is routed through it. Also fixed a
`safe_output_path` prefix bug (`startswith` let `<root>-evil` pass) and the
report/export writers that bypassed confinement with a raw `os.path.join`.
No single tool both reads a caller file and egresses it (our Slack tools
generate from live Zabbix data), so the headline 7.5 does not apply. Tool
count unchanged (163). +19 tests, 685 → 704.

## [1.16.8] - 2026-07-08

### Fixed — time-honest uptime + trend-retention honesty
ADR 075 (tasks 168-170). `get_service_uptime_report` used *observed*
trend rows as the denominator, so a host that wrote one sample then died
read 100%, and chronically-dead hosts were dropped from the report
entirely (the worst offenders became invisible — live proof: 3 premium
hosts at 0.00% in the reports SLA showed absent/healthy here). New shared
pure `uptime.py`: the denominator now spans every hour from a host's
first observed sample to now (a missing hour is DOWN), with a per-host
traffic gate that rescues deprecated-check false-downs (an hour with real
traffic counts up when the check is silent). Added a trend-retention
coverage note, and a `get_month_over_month` guard that renders `n/a` +
warning instead of a fabricated delta when history can't fill the prior
period. `get_sla_dashboard` relabelled a current snapshot (it never was a
period average). +14 tests; 671 → 685. Gated tasks 163/171 unchanged.

## [1.16.7] - 2026-07-07

### Changed — file-length budgets (tests + docs only, zero runtime change)
ADR 074. Answering "prevent very long files, or fine for AI?" with
evidence: structured big modules are fine (navigable per tool-gate);
the real cost was the accumulation sink — `test_analytics.py` at 4,104
lines / 67 classes across ~10 domains, where every new test defaulted.
Split mechanically (AST, classes moved whole) into 9 domain files
(277–742 lines); verification invariant: identical collected count
(669 → 669), all green. New `TestFileLengthGuard`: src ≤ 1,100 / tests
≤ 1,000 lines, **no grandfathered exceptions** — the whole repo fits at
adoption. CLAUDE.md rule added. 669 → 671 tests.

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
- **Comment placeholder cleanup.** The compound-hostname example in
  `costs_import.py` was concrete; replaced with the generic
  `parent-a a-b`, which carries the same example value.

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
