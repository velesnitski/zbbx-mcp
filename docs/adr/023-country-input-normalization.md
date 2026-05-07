# ADR 023: Country-input normalization for `search_hosts*` tools

**Status:** Accepted
**Date:** 2026-05-05

## Problem

The country filter on `search_hosts`, `search_hosts_by_location`, and
`get_server_clusters` only accepted ISO-2 codes (`RU`, `DE`, `NL`).
LLM clients pass natural-language names ("Russia", "Saudi Arabia",
"Czechia") roughly half the time despite the docstring; the filter
silently returned zero hits, with no cue that the input was
unrecognised.

A second blind spot: `extract_country(hostname)` returns `""` for any
host whose name has no country segment — typical for management
nodes, web frontends, control-plane boxes. Those hosts were
invisible to a country filter even when their Zabbix inventory
record had a populated `country_code`.

## Decision

### `normalize_country(value)` in `data.py`

Returns the canonical ISO-3166-1 alpha-2 code for any of:
- An ISO-2 code (case-insensitive). Two-letter alphabetic input is
  treated as a code; the existing `UK → GB` alias is applied via
  `_COUNTRY_ALIASES`.
- An ISO-3 code (`RUS`, `USA`, `DEU`).
- An English country name (`Russia`, `United States`, `Czechia`,
  `Saudi Arabia`).

Returns `""` for empty or unrecognised input so callers can decide
on fallback or error message.

The lookup table `_COUNTRY_NAMES` is the standard ISO-3166-1
reference data — every country mapped, not a curated subset that
could hint at the operator's market. ASCII-only English names plus
ISO-3 codes; ~200 keys. No external dependency (`pycountry` rejected
to keep the runtime footprint tight).

### `resolve_country(host)` in `data.py`

Three-step fallback that callers use **inside country-filter
branches only** — `extract_country` remains the source of truth for
"what does the host name claim":

1. `extract_country(host['host'])` — hostname segment (cheapest, the
   common path).
2. `host['inventory']['country_code']` → `normalize_country(...)` to
   handle the "DE"/"de" case.
3. `host['inventory']['country_name']` → `normalize_country(...)`
   for "Germany" / "Russia" inventory entries.

Returns `""` when none of the three sources yields a valid code.

### Wiring

Three tools updated in `tools/hosts.py`:

- `search_hosts`, `get_server_clusters`: branched on `if country:`
  to (a) call `normalize_country` up front, (b) reject unrecognised
  input with a hint, (c) request `selectInventory` in the
  `host.get` call, and (d) match against `resolve_country(h)`.
- `search_hosts_by_location`: same pattern through the cached
  `fetch_enabled_hosts` helper, which gains an `inventory: bool =
  False` opt-in. The opt-in bypasses the existing client-side cache
  so callers without an inventory need don't pay the payload weight.

The result header surfaces the resolved code so the LLM sees
`Found: 4 hosts in SA` even when the input was `Saudi Arabia` — that
makes the input-was-understood signal visible in the rendered
output without parsing the table.

### Docstrings

All three tools' `country:` arg description now reads:
`"ISO-2 / ISO-3 / English name (e.g. RU, RUS, Russia). Empty = all"`.

## Test approach

12 new tests in `test_analytics.py`:

**`normalize_country` (6):**
- ISO-2 passthrough (case-insensitive, whitespace-tolerant).
- `UK → GB` alias preserved.
- ISO-3 codes recognised (`RUS`, `usa`, `DEU`).
- English names recognised, including aliases (`UAE`, `Czech
  Republic`, `United Kingdom`).
- Empty / `None` / unrecognised name → `""`.
- Two-letter unknown still returns the upper-cased input (downstream
  filter just won't match) — no enumeration of valid ISO-2 needed.

**`resolve_country` (6):**
- Hostname segment wins over disagreeing inventory.
- Inventory `country_code` used when hostname is unhelpful.
- Inventory `country_name` (normalised) used when code is empty.
- All-empty inventory → `""`.
- Missing `inventory` key (older Zabbix or no `selectInventory`) →
  `""`.
- Inventory unknown name (`Atlantis`) falls through cleanly.

The wiring in `tools/hosts.py` is configuration-level — pure
helpers feed existing handler logic. The 374 pre-change tests still
pass; the 12 new ones bring the total to 386.

## Consequences

- 386 tests pass (374 pre-change + 12 new).
- Tool count unchanged at 155.
- `WRITE_TOOLS` unchanged.
- `data.py` exposes two new public symbols (`normalize_country`,
  `resolve_country`); no other module's surface area changes.
- Tool descriptions for the three `search_hosts*` tools change
  slightly (one extra phrase). Reflected in the compact handshake —
  net effect is small (a few tokens) and offset by the existing
  schema-strip from ADR 017.
- `host.get` calls in those three tools now request
  `selectInventory` when the country filter is active. Network cost
  is minor (a few extra fields per host); cache behaviour is
  preserved for the no-filter path.
- `fetch_enabled_hosts` gains a public `inventory: bool` keyword
  argument; existing callers see no behaviour change because the
  default is `False`.

## Not included

- `pycountry` dependency. The embedded dict covers every country in
  ISO-3166-1. `pycountry` would add a runtime install just for what
  is essentially constant reference data.
- Auto-detection of "did the LLM mean a country?" for arbitrary
  free text. The contract is explicit: pass an ISO-2, ISO-3, or
  recognised English name. Anything else gets a clear error
  message.
- Country normalisation in tools that don't currently take a
  `country` argument. If a future tool needs it, the helper is
  ready.
