"""Requested inventory fields must exist in Zabbix's schema (ADR 129).

Host inventory is not free-form: `host_inventory` has a fixed column set. Asking
`host.get` for a name outside it is **not an error** — the request is accepted
and simply returns nothing for that field (ADR 088's silent-degradation class).

So an invented field name produces an empty inventory dict, which is
indistinguishable from "this host has no inventory filled in". Every caller
asked for `country_code` and `country_name`; neither is a Zabbix inventory
field. The country fallback in `resolve_country` could therefore never fire,
from the day it shipped.

The unit tests did not catch it, because they *supplied* the invented shape:
`{"inventory": {"country_code": "NL"}}` is a dict Zabbix cannot produce. They
proved the parser worked on data that never arrives. That is the failure this
file guards against — the request, the parse, and the schema are checked
against each other rather than each being tested alone.
"""

from __future__ import annotations

import ast
import pathlib

from zbbx_mcp.country import (
    HOST_INVENTORY_FIELDS,
    INVENTORY_COUNTRY_FIELDS,
    resolve_country,
)

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = sorted((ROOT / "src").rglob("*.py"))


class TestRequestedFieldsExist:
    def test_the_fields_we_request_are_real_zabbix_fields(self):
        unknown = [f for f in INVENTORY_COUNTRY_FIELDS
                   if f not in HOST_INVENTORY_FIELDS]
        assert not unknown, (
            f"{unknown} are not Zabbix host-inventory fields. `host.get` will "
            "accept the request and return nothing for them, so the country "
            "fallback silently never fires")

    def test_the_dead_field_names_are_gone_from_requests(self):
        """The specific names that were requested for months and never existed."""
        offenders = []
        for p in SRC:
            for n, line in enumerate(p.read_text().splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue          # the ADR note explaining the defect
                if "country_code" in line or "country_name" in line:
                    offenders.append(f"{p.relative_to(ROOT)}:{n}")
        assert not offenders, (
            f"{offenders} reference an inventory field Zabbix does not have")

    def test_every_selectinventory_site_uses_the_shared_constant(self):
        """Request and parse must not drift apart again.

        They lived in different files with nothing connecting them, which is
        how every caller ended up asking for a field the parser's own module
        never mentioned. Inline literals reopen exactly that gap.
        """
        sites = []
        for p in SRC:
            for n, line in enumerate(p.read_text().splitlines(), 1):
                if "selectInventory" not in line or line.lstrip().startswith("#"):
                    continue
                if '"""' in line or "``" in line:
                    continue          # docstring prose
                sites.append((p.relative_to(ROOT), n, line.strip()))
        assert len(sites) >= 4, (
            f"found only {len(sites)} selectInventory call site(s) — the scan "
            "is broken, so a pass here would mean nothing")
        inline = [s for s in sites if "INVENTORY_COUNTRY_FIELDS" not in s[2]]
        assert not inline, f"inline inventory field list(s): {inline}"

    def test_the_schema_set_is_actually_populated(self):
        # A guard whose ground-truth set is empty would pass on anything.
        assert len(HOST_INVENTORY_FIELDS) > 60
        assert "site_country" in HOST_INVENTORY_FIELDS
        assert "country_code" not in HOST_INVENTORY_FIELDS


class TestResolveCountryReadsWhatWeRequest:
    def test_parsed_fields_are_a_subset_of_requested_fields(self):
        """Whatever `resolve_country` reads, some caller must have asked for."""
        src = (ROOT / "src/zbbx_mcp/country.py").read_text()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "resolve_country")
        literals = {n.value for n in ast.walk(fn)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        read_fields = literals & HOST_INVENTORY_FIELDS
        assert not read_fields - set(INVENTORY_COUNTRY_FIELDS), (
            f"{read_fields - set(INVENTORY_COUNTRY_FIELDS)} is read but never "
            "requested — it will always be absent")

    def test_site_country_resolves(self):
        assert resolve_country({"host": "control-plane-01",
                                "inventory": {"site_country": "Netherlands"}}) == "NL"

    def test_location_is_a_fallback(self):
        assert resolve_country({"host": "control-plane-01",
                                "inventory": {"location": "Germany"}}) == "DE"

    def test_a_trailing_code_in_free_text_is_read(self):
        assert resolve_country({"host": "control-plane-01",
                                "inventory": {"location": "Amsterdam, NL"}}) == "NL"

    def test_a_city_alone_does_not_invent_a_country(self):
        # normalize_country validates, so non-countries yield "" rather than a
        # confident wrong code.
        assert resolve_country({"host": "control-plane-01",
                                "inventory": {"site_city": "Springfield"}}) == ""

    def test_the_hostname_still_wins(self):
        assert resolve_country({"host": "edge-aq9001",
                                "inventory": {"site_country": "France"}}) == "AQ"
