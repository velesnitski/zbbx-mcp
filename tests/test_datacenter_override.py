"""Configured datacenter ranges (ADR 122).

`ZABBIX_DATACENTER_CIDRS` supplies `(range, city)` pairs that are searched
ahead of the built-in table, most-specific-first. Same shape and same failure
semantics as the provider override in ADR 120.
"""

from __future__ import annotations

import ipaddress
import json

import pytest

from zbbx_mcp import classify as classify_mod
from zbbx_mcp.classify import resolve_datacenter


@pytest.fixture(autouse=True)
def _reset():
    classify_mod._EXTRA_DC_NETS = None
    yield
    classify_mod._EXTRA_DC_NETS = None


def _cfg(monkeypatch, value):
    monkeypatch.setenv("ZABBIX_DATACENTER_CIDRS", value)
    classify_mod._EXTRA_DC_NETS = None


class TestOverride:
    def test_unset_changes_nothing(self):
        assert classify_mod.get_extra_dc_nets() == []

    def test_inline_json_is_consulted(self, monkeypatch):
        _cfg(monkeypatch, json.dumps({"Acme": [["203.0.113.0/24", "Rivertown, ZZ"]]}))
        assert resolve_datacenter("203.0.113.9") == ("Acme", "Rivertown, ZZ")

    def test_file_path_is_consulted(self, monkeypatch, tmp_path):
        f = tmp_path / "dc.json"
        f.write_text(json.dumps({"Acme": [["198.51.100.0/24", "Lakeside, ZZ"]]}))
        _cfg(monkeypatch, str(f))
        assert resolve_datacenter("198.51.100.7") == ("Acme", "Lakeside, ZZ")

    def test_configured_ranges_win_over_the_builtin_table(self, monkeypatch):
        """Constructed, not drawn from the shipped table.

        The built-in table is empty by default, and its contents are a
        deployment's business either way — a test that indexes it breaks the
        moment that changes.
        """
        net = ipaddress.ip_network("203.0.113.0/24")
        probe = str(net.network_address + 1)
        monkeypatch.setattr(classify_mod, "_DC_NETS",
                            [("Builtin", "Origin, ZZ", net)])
        assert resolve_datacenter(probe) == ("Builtin", "Origin, ZZ")   # baseline
        _cfg(monkeypatch, json.dumps({"Configured": [[str(net), "Elsewhere, ZZ"]]}))
        assert resolve_datacenter(probe) == ("Configured", "Elsewhere, ZZ")

    def test_most_specific_wins_among_configured_ranges(self, monkeypatch):
        _cfg(monkeypatch, json.dumps({
            "Broad": [["203.0.113.0/24", "Broadville, ZZ"]],
            "Narrow": [["203.0.113.8/29", "Narrowton, ZZ"]],
        }))
        assert resolve_datacenter("203.0.113.9") == ("Narrow", "Narrowton, ZZ")
        assert resolve_datacenter("203.0.113.200") == ("Broad", "Broadville, ZZ")

    def test_unusable_config_disables_rather_than_half_applies(self, monkeypatch):
        # A partial merge resolves some addresses against configured data and
        # others against the built-in table, with nothing saying which.
        for bad in ('{"X": [["not-a-cidr", "C"]]}', "{oops",
                    '["not","an","object"]', '{"X": ["missing-the-city"]}'):
            _cfg(monkeypatch, bad)
            assert classify_mod.get_extra_dc_nets() == []

    def test_unparseable_address_is_still_unknown(self, monkeypatch):
        _cfg(monkeypatch, json.dumps({"X": [["203.0.113.0/24", "C, ZZ"]]}))
        assert resolve_datacenter("not-an-ip") == ("Unknown", "")

    def test_address_outside_every_range_reports_no_city(self, monkeypatch):
        # Provider-only detection still answers; the city stays empty rather
        # than being guessed.
        _cfg(monkeypatch, json.dumps({"X": [["203.0.113.0/24", "C, ZZ"]]}))
        _prov, city = resolve_datacenter("192.0.2.1")
        assert city == ""


class TestBuiltinTable:
    def test_the_builtin_table_is_empty_by_default(self):
        # The mapping is supplied per deployment, so an unconfigured install
        # ships none of it and reports no city rather than a guessed one.
        assert classify_mod.DATACENTER_CIDRS == {}
        assert classify_mod._DC_NETS == []

    def test_unconfigured_resolution_reports_no_city(self):
        _prov, city = resolve_datacenter("203.0.113.9")
        assert city == ""

    def test_any_entry_present_parses_and_names_a_city(self):
        # Vacuous while the table is empty, and deliberately kept: it is the
        # check that matters the moment anything is added back.
        bad = []
        for prov, entries in classify_mod.DATACENTER_CIDRS.items():
            for cidr, city in entries:
                try:
                    ipaddress.ip_network(cidr, strict=False)
                except ValueError as exc:
                    bad.append(f"{prov}: {cidr} ({exc})")
                if not city.strip():
                    bad.append(f"{prov}: {cidr} has no city")
        assert not bad, "; ".join(bad)

    def test_lookup_order_is_most_specific_first(self, monkeypatch):
        """The ordering IS the mechanism.

        `resolve_datacenter` returns the first match, so if this regresses an
        address inside two ranges silently resolves to the broader one — the
        wrong city, reported confidently. Asserted against constructed data so
        it holds whether or not anything is loaded.
        """
        broad = ipaddress.ip_network("203.0.113.0/24")
        narrow = ipaddress.ip_network("203.0.113.8/29")
        monkeypatch.setattr(classify_mod, "_DC_NETS", sorted(
            [("B", "Broadville, ZZ", broad), ("N", "Narrowton, ZZ", narrow)],
            key=lambda x: -x[2].prefixlen))
        assert resolve_datacenter("203.0.113.9") == ("N", "Narrowton, ZZ")
        assert resolve_datacenter("203.0.113.200") == ("B", "Broadville, ZZ")
