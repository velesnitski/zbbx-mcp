# ADR 093: country extraction — indexed form first, ISO-validated

**Status:** Accepted
**Date:** 2026-07-27

## Problem

`extract_country()` matched a single alternation with `re.search`:

```
(?:[-_]([a-z]{2})\d)      # the country+index form
|(?:[-_]([a-z]{2})[-_])   # a bare two-letter segment
```

`re.search` returns the **leftmost** match, so a two-letter segment earlier
in the name always beat the indexed suffix that actually encodes location.
Host names commonly carry a datacenter / role / market tag in exactly that
position, which produced two failure modes:

1. **Silent misclassification.** Where the tag happened to be a real ISO
   code, hosts were filed under a country they have no presence in. Geo
   reports, density maps and country filters then showed confident,
   authoritative-looking infrastructure in the wrong place — verified live:
   a country filter returned several hosts, carrying real traffic, that are
   physically elsewhere.
2. **An unrescuable blank.** Where the tag was not a real code, the
   bogus-but-truthy string was returned anyway, and `resolve_country()`
   short-circuits on `if cc: return cc` — so the Zabbix inventory fallback
   never ran. Correct inventory data could not fix the host, which made the
   gap look like missing metadata rather than a parsing bug.

`normalize_country()` compounded it: any two alphabetic characters were
echoed back as a country code without validation, so a filter on an
unassigned pair silently matched nothing instead of reporting bad input.

## Decision

- Split the alternation into two explicit patterns and try them **in
  priority order**: every indexed (`<cc><digit>`) candidate first, then bare
  segments. Position no longer decides; form does.
- Use a lookahead for the segment pattern so adjacent segments are both
  visited rather than the first consuming its neighbour's separator.
- Return a code **only if it is a real ISO 3166-1 alpha-2 code**. The
  allow-list `ISO2_CODES` is derived from the existing reference table, so
  the two cannot drift. A tag that merely looks like a code now yields
  `""`, which both keeps it out of reports and lets `resolve_country` fall
  through to inventory.
- `normalize_country()` validates two-letter input against the same set.

## Test approach

`tests/test_geo_country.py`: the indexed form wins regardless of order
(both directions asserted); a non-ISO tag cannot mask a real code; a name
with no real code yields `""` so the inventory fallback runs; an unassigned
pair is rejected by `normalize_country`. Fixtures use arbitrary Pacific
island codes so they describe no real deployment. The superseded
"first wins" and "any two letters pass" tests are replaced, with the reason
recorded inline.

## Consequences

- Country is now derived from the form that encodes it, and can never be a
  tag. Hosts whose name carries both a market tag and a physical index now
  resolve to the physical location.
- A host with no parseable country falls through to inventory as designed,
  so tagging hosts in Zabbix is now an effective fix.

## Not included

- **Warning when a country filter matches nothing but name patterns
  suggest otherwise.** A useful second layer, but the extractor was the
  defect; the heuristic is a separate change.
