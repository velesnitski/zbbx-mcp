# ADR 129 — A field you request but cannot receive

**Status**: Accepted (2026-08-20)
**Affected**: `country.py` (`HOST_INVENTORY_FIELDS`,
`INVENTORY_COUNTRY_FIELDS`, `resolve_country`), `fetch.py`, `tools/hosts.py`,
`tools/diagnose.py`, `data.py`, `tests/test_inventory_fields.py`.
**Extends**: ADR 088, ADR 099, ADR 119.

## Context

ADR 099 added a country fallback: when a host name carries no country code,
`resolve_country` reads it from Zabbix host inventory. Five call sites passed
`selectInventory` to bring the fields along on a request they were already
making, and `resolve_country` parsed them.

The fields requested were `country_code` and `country_name`. **Neither is a
Zabbix host-inventory field.** `host_inventory` has a fixed column set; the
country field is `site_country`, alongside `site_city`, `location` and
`location_lat`/`location_lon`.

Asking for a field outside that set is not an error. `host.get` accepts the
request and returns nothing for it — the silent-degradation behaviour this repo
already documented in ADR 088, where removed API fields were ignored rather
than rejected. So the inventory dict never carried the requested keys, every
branch in the fallback saw an empty string, and the feature was unreachable
from the day it shipped.

Two things kept it looking alive.

**The empty result is the same shape as the honest one.** A host with no
inventory filled in also yields nothing. There is no observable difference
between "the field does not exist" and "this host has no inventory", so
nothing in the output ever looked wrong.

**The tests supplied the shape Zabbix cannot produce.** They passed fixtures
like `{"inventory": {"country_code": "NL"}}` and asserted the parser returned
`NL`. It did. The parser was correct; the dict was fiction. A test that
invents its own wire shape proves the code handles that shape, and nothing
about whether the shape ever arrives — ADR 119's question again, one level
lower down.

The root enabler was structural: the fields **requested** lived in `fetch.py`,
`hosts.py` and `diagnose.py`, while the fields **parsed** lived in
`country.py`. Nothing connected them, so they could disagree indefinitely
without any single file looking wrong.

## Decision

**Request and parse from one constant, in one place.**
`INVENTORY_COUNTRY_FIELDS` sits in `country.py` immediately above
`resolve_country`, which iterates it. Every `selectInventory` site passes it
rather than an inline list.

**Check the constant against the schema.** `HOST_INVENTORY_FIELDS` carries
Zabbix's documented inventory column set, and a test asserts every requested
field is in it. The API cannot reject a bad field name, so the validation
happens here instead.

**Validate what is parsed.** `site_country` and `location` are free text.
Values go through `normalize_country`, which accepts ISO-2, ISO-3 or an
English name and returns `""` for anything else, so a city or a rack note
yields no country rather than a confident wrong one. A trailing token after a
comma gets one validated attempt, which handles `"<city>, <cc>"`.

## Consequences

The fallback can now fire. Whether it *does* depends on whether this
deployment populates `site_country` at all — a separate question, and one the
fix does not prejudge: an unpopulated field yields `""` exactly as before, and
the inventory-gap note already names hosts whose country cannot be resolved.

Four tests tie the pieces together rather than testing each alone: requested
fields exist in the schema, no dead name survives anywhere in `src`, every
call site uses the shared constant instead of an inline literal, and every
inventory field `resolve_country` reads is one some caller requests. Five of
the ten were confirmed to fail against the shipped code first.

The general rule: **an API that accepts a request without honouring it turns a
typo into a silent feature deletion.** Where the wire cannot reject bad input,
something local has to — and a test that builds its own fixtures cannot be
that thing, because it will build the input the code expects.
