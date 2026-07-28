# ADR 099: investigation-ready host detail, and explaining an empty country filter

**Status:** Accepted
**Date:** 2026-07-28

## Problem

Two loose ends from the review batch, both about answering the question the
caller actually asked rather than the narrow one the code implemented.

**1. `get_host` was too sparse to be useful.** It rendered name, id, status,
groups and interfaces — and nothing else. Every real investigation
immediately needs the same four things it omitted: where the host is, what
product it serves, who hosts it, and whether it is currently moving traffic.
So the tool that exists to answer "tell me about this host" reliably cost
three further calls before it answered anything. Worse, it *already* fetched
`output: "extend"` — the data was on the wire and discarded by the formatter.

**2. An empty country filter asserted absence.** `search_hosts` and
`search_hosts_by_location` returned a flat "no hosts found" when a country
filter matched nothing. That is ambiguous: either there genuinely are no such
hosts, or they exist and their country cannot be derived. ADR 093 fixed the
extractor bug that caused the observed case, but the ambiguity is structural
— any naming convention the parser cannot read reproduces it, and a confident
empty result reads as fact.

## Decision

**Enrichment.** `format_host_detail(host, context=None)` gains an optional
context dict; the formatter stays pure and renders only the keys present, so
a missing signal is silently omitted rather than shown as "unknown". A
`_host_context` helper gathers: country (via inventory, which now rides along
on the `host.get` already being made — free), product/tier, provider,
datacenter, linked templates (free, derived from the same response), plus
cost/bandwidth macros and a traffic/service snapshot (two extra calls).

Every enrichment block is **individually best-effort**: an error in macros or
traffic must never turn a working identity lookup into an error string, so
each degrades to an absent key. `brief=True` skips the extra calls for
callers that only want identity.

**Inventory gap.** Two pure helpers in `country.py`:
`name_suggests_country` (deliberately looser than `extract_country` —
accepts the code in any separator-delimited position, including ones the
strict parser rejects) and `country_inventory_gap`, which returns hosts whose
name *looks* like the requested country but does not resolve to it. Both
country-filtering tools now append a note naming those hosts and pointing at
the likely cause. The loose matcher is used **only** to explain an empty
result — never to assign a country — so a false positive costs a hint, not a
wrong verdict.

**CI/local lint drift.** The workflow installed `ruff` unpinned while the
project floated `ruff>=0.4`, so CI silently ran a newer version with new
rules — a commit could lint clean locally and fail CI, which is exactly what
happened (B033, and the same pass-local/fail-CI class as ADR 084). Both are
now pinned to the identical version; bumping is a deliberate, separate
commit.

## Test approach

`tests/test_host_detail.py` (+13): the formatter is unchanged without a
context and renders each identity/state line with one; absent keys are
omitted while a genuine `0.0` still renders (zero traffic is a fact, not
"unknown"); the wire asks for inventory and templates; `brief` makes no extra
calls; and a deliberately failing client proves enrichment errors cannot
break the lookup. For the gap helpers: separator-delimited matching, no match
on a code buried inside a word, and gap detection using shapes the strict
parser genuinely cannot read (a dot separator, a trailing code with no
index). 857 → 870.

## Consequences

- One call answers the question an investigation starts with.
- An empty country filter now distinguishes "none exist" from "cannot tell",
  which is the difference between a fact and a silent gap.
- Local lint results match CI.

## Not included

- **Active problems / recent changes in `get_host`.** That is `diagnose_host`'s
  job; duplicating it here would make a cheap lookup expensive.
- **Auto-writing inventory from the name pattern.** The hint names the hosts;
  deciding the country is an operator call, and a loose matcher must not
  silently become a source of truth.
