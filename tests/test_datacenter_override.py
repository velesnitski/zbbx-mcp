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
        prov, city, net = classify_mod._DC_NETS[0]
        probe = str(net.network_address + 1)
        assert resolve_datacenter(probe) == (prov, city)          # baseline
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


class TestBuiltinTableShape:
    def test_every_entry_parses_and_names_a_city(self):
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

    def test_lookup_is_ordered_most_specific_first(self):
        """The ordering IS the mechanism.

        `resolve_datacenter` returns the first match, so if this regresses an
        address inside two ranges silently resolves to the broader one — the
        wrong city, reported confidently.
        """
        lengths = [net.prefixlen for _, _, net in classify_mod._DC_NETS]
        assert lengths == sorted(lengths, reverse=True)
