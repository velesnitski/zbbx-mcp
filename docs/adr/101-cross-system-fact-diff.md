# ADR 101: `compare_report_facts` — diff a sibling pipeline's facts, and refuse to judge the rest

**Status:** Accepted
**Date:** 2026-07-30

## Problem

A separate reporting pipeline runs overlapping analytics against the **same**
Zabbix instance as this server — traffic erosion, uptime/SLA, dark-host
detection, country resolution — and the two are maintained independently, with
findings hand-ported one way. Nothing verified they agree.

That gap has teeth. Both codebases have shipped defects in the *same* areas:
country resolution (this server's ADR 093/100, theirs 0052), the dark-host
outage invariant (ADR 100, theirs 0054), integer-truncated uint trends (ADR 092,
their 0048). A divergence introduced on either side sits in a scheduled report
until a human happens to notice two different numbers in two documents — and by
then both numbers look authoritative.

The concrete near-miss: this server honours `ZABBIX_TRAFFIC_UNIT`, the reporting
side deliberately does not (their ADR 0043/0056). If the fleet's items ever
switch to bytes/sec, one system's figures move by 8× and the other's do not.
Nothing would have caught that except a comparison.

The reporting side already publishes its comparable figures to a JSON file
specifically so this side is *a diff rather than a re-derivation*. This ADR is
the other half.

## Decision

New read-only tool **`compare_report_facts`** (`tools/crosscheck.py`, tool count
165 → 166, added to the `ops` tier): read the published facts, recompute the
same quantities live, and render a per-field diff.

**The load-bearing decision is what the tool refuses to judge.** A naive
implementation would diff every recognised field and would immediately cry wolf,
because some quantities carry the same name on both sides and are computed to
different definitions *on purpose*. An invariant that fires on correct data is
an invariant nobody reads — which is the exact failure mode this whole exercise
exists to prevent. So every field is classified:

- **strict** — provably identical definitions: host population and country
  resolution, both derived from the same host list via the same two helpers
  (`extract_country`, `classify_host`). A mismatch here is a real defect on one
  side. Fields: `total_hosts`, `countries`, `country_host_sum`,
  `countryless_by_design`, `blank_country_hosts`, and the intersection of the
  per-country counts.
- **advisory** — same subject, definitions not guaranteed to match, or the
  thresholds behind the number are not published. Shown side by side and
  explicitly **not** judged. Uptime/SLA is period-integrated there and
  point-in-time here (ADR 097); the erosion counts depend on thresholds the
  facts file does not carry.

Three further honesty constraints:

1. **Drift is not divergence.** The fleet legitimately changes between the two
   runs, so a count delta within `drift_tolerance` (default 2) is `DRIFT`.
   Only a larger delta is `DIVERGE`.
2. **Staleness is always disclosed.** The facts file carries no timestamp of
   its own, so its age comes from the file mtime and is stated on every run;
   past 48h the output says outright that differences are as likely to be fleet
   change as disagreement. An undated snapshot cannot support a strict verdict.
3. **The countryless-product set is mirrored exactly** from the reporting side
   (`infrastructure`, `monitoring`, `unknown`, `""`). If the two sets drift, the
   two systems compute "hosts missing a country" over different populations and
   the diff reports a defect that does not exist. This is pinned by a test.

The caller-supplied path goes through `confined_input_path` (ADR 076), so a
facts path cannot be used to read outside the allowed roots.

## Test approach

`tests/test_crosscheck.py` (+18). The fact builder: country tallies, the
"per-country counts always reconcile to the fleet" invariant, role-named hosts
counted as *by design* rather than as missing data, and its converse (a
country-bearing host with no derivable country **is** a gap). The comparison:
identical inputs are all `MATCH`; a small delta is `DRIFT` and a large one
`DIVERGE`; `drift_tolerance=0` makes any delta diverge; an absent field is
`MISSING`, not a violation; per-country compares only the intersection, because
the published list is truncated. Two tests exist specifically to prove the tool
**won't** cry wolf: SLA and erosion come back `ADVISORY` and do not move the
overall verdict. Wire: missing path is explained rather than an error, a path
outside the allowed roots is refused, non-object JSON is rejected, and staleness
appears in the output. 873 → 891.

## Consequences

- Divergence between two independently-maintained systems reading one instance
  is now caught deliberately, instead of by someone noticing two numbers.
- The check is quiet by construction: only provably-comparable fields can raise
  an alarm, so a `DIVERGE` row means something is actually wrong.

## Not included

- **Comparing SLA or erosion numerically.** Possible only once both sides
  publish the parameters that produced them; until then a diff would be
  measuring a definition difference and calling it a defect.
- **Running the comparison in CI.** This server is an interactive stdio process,
  not a library the reporting job can import — which is why the reporting side
  asserts its own invariants there and publishes facts for this side to diff.

---

## Addendum (2026-08-11) — population drift is not divergence

Dogfooding the tool on the reporting side's first live `crosscheck.json` exposed
a real defect **in this tool**: it reported `DIVERGENCE — one side is wrong` and
told the operator *"check which, rather than assuming the report is stale"* —
while **every compared per-country count matched exactly** (72 countries, DE 116,
US 117, …). Only the aggregate counts differed (total 943→951, blank 2→19,
countryless 272→262).

That is exactly backwards, and it is the cry-wolf ADR 101 was written to prevent.
When per-country resolution provably agrees, an aggregate-count difference cannot
be a resolution defect — it is fleet drift or a naming lag between the two runs,
and "the report is stale" is the *most* likely explanation, not one to dismiss.
The blind spot was that the tool could not tell *count drift* from *resolution
divergence*.

The discriminator was already computed and unused: **per-country agreement**. So
`total_hosts` / `country_host_sum` / `countryless_by_design` / `blank_country_hosts`
are downgraded from `DIVERGE` to a new `POP_DRIFT` verdict when every compared
per-country count matches and the distinct-country total matches (`_resolution_agrees`).
`countries` itself is not downgraded — a change in the distinct-country count is a
resolution signal. A genuine per-country mismatch, or no per-country evidence at
all, leaves `DIVERGE` untouched (verified against the observed data:
943↔951 now reads POP_DRIFT with a "resolution agrees" overall verdict, not
DIVERGENCE). The tool also now surfaces the **live** blank-country sample, so
"investigate blank_country_hosts" names hosts you can actually fix.

Lesson, recorded because it is the whole point of the tool: a consistency check
is only as good as its ability to stay quiet on benign difference. This one
learned to.
