# ADR 120 — Provider ranges are generated and operator-supplied, not curated

**Status**: Accepted (2026-08-18)
**Affected**: `src/zbbx_mcp/classify.py` (`_load_provider_cidrs`,
`get_extra_provider_nets`, `detect_provider`, `PROVIDER_CIDRS`),
`src/zbbx_mcp/data/provider_cidrs.json`, `scripts/gen_provider_cidrs.py`,
`tests/test_provider_override.py`.

## Context

`detect_provider` resolved an address against a table hand-maintained in the
source of `classify.py`. Three problems, and editing the table fixes none of
them.

**It cannot be complete.** There is no registry of every hosting provider, and
allocations move between them. Any hand-built list is a snapshot of whatever
its authors happened to know, and it starts going stale the day it ships. The
table covered a small share of routed IPv4 space, so most addresses resolved to
`Other`.

**A wrong entry does not fail loudly.** An address in a mistaken range is not
reported as unknown — it is attributed to the wrong provider, confidently, in
output someone acts on. This is the defect class this codebase keeps finding,
and a hand-maintained allocation table is a standing invitation to it.

That was not hypothetical. Two entries were wrong:

| Range | Table said | Actually announced by |
|---|---|---|
| `18.0.0.0/8` | AWS | AS3 MIT — AWS holds only part of the /8 |
| `34.128.0.0/…` | AWS | Google Cloud |

Both had been answering questions for as long as the table existed.

**Growing it by hand makes the second problem worse.** Every added range is
another chance to be confidently wrong, and a wrong range is invisible in
review — it looks exactly like a right one.

## Decision

**Generate the table instead of curating it.** `scripts/gen_provider_cidrs.py`
derives `data/provider_cidrs.json` from the public prefix-to-AS dataset at
<https://iptoasn.com>, which maps every routed IPv4 prefix to the AS that
announces it. The selection rule is mechanical and stated in full in the
script: drop unrouted space, aggregate each AS's prefixes, rank ASes by routed
address count, keep the top N and each one's largest few blocks.

Aggregation summarises exact start–end ranges and collapses only adjacent
blocks, so the address set is unchanged — the process cannot attribute an
address to an AS that does not announce it. Truncating to the largest blocks
costs coverage but likewise cannot introduce a wrong answer. Every failure mode
of the generator is "resolves to `Other`", never "resolves to the wrong name".

Result: **a substantially broader table**, a larger share of routed IPv4 space, up from
a smaller table. All previous providers and ranges are retained;
probe addresses drawn from the old table resolve identically and
the 4 that changed are the corrections above.

Display names are pinned (`KEEP_NAMES`) so reports keep saying `Vultr` and
`Linode` rather than following whatever string the routing dataset carries this
month, and regeneration unions with the file already on disk so a narrower
cutoff can never silently drop an operator that used to resolve.

**And let the deployment override all of it.** `ZABBIX_PROVIDER_CIDRS` — a JSON
object of `{"Provider": ["a.b.c.d/n", ...]}`, given as a file path or inline —
is consulted **before** the generated table. Whoever runs this knows their own
address space exactly; that should not require a release to act on.

Unparseable configuration disables the override entirely rather than
half-applying it. A partial merge is worse than none: some addresses would
resolve against operator data and others silently against the generated table,
with nothing in the output saying which.

If the packaged data file is missing or corrupt, loading degrades to an empty
table and logs a warning. The server keeps running and every address answers
`Other` — honest rather than wrong — instead of taking the process down over a
packaging defect.

## Consequences

- Coverage grows substantially, and correctness is now a property of a
  public dataset rather than of a maintainer's recollection.
- The table is refreshed by re-running a script, not by editing source.
- `classify.py` gets shorter; the data lives in the package as
  JSON and ships in the wheel.
- `DATACENTER_CIDRS` is still hand-maintained and still inline. It has the same
  shape and the same exposure to the same defect; worth the same treatment, but
  it is smaller and not urgent.
- The repo-wide address guard (ADR 119 / v1.16.50) exempts *declared network
  addresses of the allocation tables*, and now reads them from the generated
  file — so the exemption tracks the data rather than needing maintenance.

## Verification

Generator: aggregation preserves the exact address set; regeneration unions
rather than replaces; name pinning survives a hyphenated leading AS token.

Runtime: override consulted ahead of the table, most-specific-wins within the
override, both configured forms accepted, unusable input disabling rather than
half-applying, a missing or corrupt data file degrading rather than raising,
every shipped CIDR parsing as a true network address, and no entry narrower
than a `/24`.
