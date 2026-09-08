# 139. A contradicted range is a question, not an answer

## Status

Accepted (v1.16.64)

## Context

ADR 138 made `get_geo_inventory` filter on the datacenter resolved from the
IP, and it was right to: the hostname is a naming convention. The resolution
comes from `ZABBIX_DATACENTER_CIDRS`, a hand-maintained list of ranges, and
the tool trusts every entry in it completely.

One entry covered a provider's entire `/16` with a single city. That block
held hosts named for two countries. The majority were where the entry said;
the minority were not, and the tool reported them under "named for this
country but located elsewhere" — a confident, specific finding, built on an
input nobody had checked. The figure reached a report before the operator
caught it, and nothing in the output had said that the range covered hosts
which disagreed with it.

The same session produced a second, smaller gap. Getting 7-day trends for a
named set of hosts meant either a country/tier filter whose output could run
past what the client shows, or the comparison tool, which has no `min` and no
trend column. Neither takes a list of hosts.

## Decision

**`get_geo_inventory` reports every configured range, relevant to the asked
country, whose covered hosts are named for more than one country** — the
range, its city, the per-country counts, and the hosts whose names disagree
with it. The hosts stay where the range put them. ADR 138 still holds: the IP
outranks the name. What changes is that the reader is told when the two
disagree, which is the only signal available that a range deserves a look.

**The disagreeing hosts are not moved to `unresolved`.** Majority vote is a
heuristic, and it is wrong in the other direction just as often: a facility
can serve two markets, and a correct range covering hosts named for both would
then be reported as suspect and its minority un-placed. A contradiction is a
question. The tool asks it and names the hosts; narrowing the range, or not,
is the operator's call.

**`get_trends_batch` takes an explicit `hosts` list.** A caller who names the
hosts has chosen the set: `max_results` does not trim it, and a name Zabbix
does not know is reported, not dropped. A missing row in a long table is
invisible, which is the same failure as a wrong range — an absence that reads
as a fact.

## Consequences

The disclosure fires on every query that touches a mixed range, including
correct ones. That is the intended cost: it goes away when the entry is
narrowed to the facility it actually describes, which is also what makes
every other tool's City column right.

Only ranges that touch the asked country are reported — placed in it, or
covering hosts named for it — so a query about one country is not noisy
about another's.

Fixtures use a documentation prefix and uninhabited-territory codes, per the
hostname guard's policy (ADR 127).
