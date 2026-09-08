# 138. A country filter that means where the box is

## Status

Accepted (v1.16.63)

## Context

Every `country` filter in this server — `get_server_map`, `get_server_load`,
`get_traffic_report`, `search_hosts_by_location`, the regional tools — matches
the two-letter code in the **hostname**. That is a naming convention. It is set
once, by hand, when a host is provisioned, and nothing ever reconciles it with
where the host actually is.

The two drift, and not at the margins. Asked what serves one country, the
hostname filter returned a set of which a substantial fraction sat in
datacenters in three *other* countries — and missed a larger set of hosts
that were physically in the country but named for a neighbour. The answer it
gave was not slightly wrong; it was a different fleet.

The server already knew better. `resolve_datacenter(ip)` (ADR 122) returns the
real `"City, CC"`, and five tools print it in a City column. None filters by
it. So the honest answer to "what serves this country" was a hand-merge across
several truncated tables, reading a column the tool refused to use.

## Decision

**`get_geo_inventory(country)` filters on the resolved datacenter, and only on
that.** It reports the in-country fleet split Free / Paid and by product and
tier, with carrier-NIC traffic (ADR 137) and CPU.

**It names the disagreements instead of hiding them.** Two lists follow the
figures: hosts *named* for the country that sit elsewhere, with their real
city — excluded from the count and said so; and hosts in the country named for
somewhere else — included, and counted by the name they carry, because a
hostname-based tool would have missed every one of them.

**Unresolved is a third answer, not a rounding.** A host named for the country
whose IP matches no datacenter range is reported as unknown. It is neither in
nor out. Folding it in would assert a location nobody measured; dropping it
would make a coverage gap read as a smaller fleet. The fix for that bucket is
`build_datacenter_overrides`, which drafts the missing ranges — and the tool
says so.

`datacenter_cc` parses the code out of `"City, CC"`; an empty or malformed
string yields `""`, which every caller must read as *unknown*.

## Consequences

The existing `country` filters are unchanged. They answer a different question
— "hosts named for X" — and changing their meaning under callers who rely on it
would be the silent-widening mistake of ADR 137 in reverse. The tool
descriptions now say which question each answers.

Real geo is only as complete as `ZABBIX_DATACENTER_CIDRS`. The built-in table
is empty by design (ADR 122), so on a fresh deployment every host lands in
`unresolved` and the tool reports a fleet of zero with a large unknown list.
That is correct: it does not know, and the unknown list is the work order.

Free / Paid uses the same tier words as `get_product_summary`, so the two
surfaces cannot disagree about what a free host is.
