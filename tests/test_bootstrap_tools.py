"""Drafting the deployment-specific range files (ADR 120 / 122).

The pure halves only: grouping, reverse-DNS naming, city inference. The tool
wrappers add Zabbix I/O and a PTR lookup, neither of which belongs in a unit
test.
"""

from __future__ import annotations

import pytest

from zbbx_mcp.tools.bootstrap import (
    DC_CODES,
    city_from_ptr,
    group_blocks,
    rdns_label,
)


class TestCityFromPtr:
    @pytest.mark.parametrize(("name", "expected"), [
        ("host.fsn1.example.de", "Falkenstein, DE"),
        ("ns1.gra5.example.net", "Gravelines, FR"),
        ("a.b.nbg1.example.de", "Nuremberg, DE"),
    ])
    def test_known_codes_resolve(self, name, expected):
        assert city_from_ptr(name) == expected

    def test_unknown_name_yields_nothing(self):
        # An unmatched name must produce "" so the caller writes a placeholder.
        # Guessing here would put a wrong city in the file, which is reported
        # as confidently as a right one.
        assert city_from_ptr("plain.example.com") == ""

    def test_empty_input(self):
        assert city_from_ptr("") == ""

    def test_matching_is_case_insensitive(self):
        assert city_from_ptr("HOST.FSN1.EXAMPLE.DE") == "Falkenstein, DE"

    def test_every_code_maps_to_a_city_with_a_country(self):
        bad = [c for c, city in DC_CODES.items() if "," not in city]
        assert not bad, f"codes without a country: {bad}"


class TestRdnsLabel:
    def test_takes_the_operator_tail(self):
        assert rdns_label("a.clients.your-server.de") == "clients.your-server.de"

    def test_short_name_kept_whole(self):
        assert rdns_label("example.com") == "example.com"

    def test_empty_input(self):
        assert rdns_label("") == ""


class TestGroupBlocks:
    # RFC 5737 addresses are what ADR 119 mandates for fixtures, and Python
    # reports them as non-global — hence routable_only=False here.
    PAIRS = [("h1", "198.51.100.5"), ("h2", "198.51.100.9"),
             ("h3", "203.0.113.7"), ("bad", "not-an-ip")]

    def test_groups_by_prefix(self):
        out = group_blocks(self.PAIRS, 24, False, routable_only=False)
        assert out["198.51.100.0/24"] == ["h1", "h2"]
        assert out["203.0.113.0/24"] == ["h3"]

    def test_prefix_width_is_honoured(self):
        # Expected keys are derived, not written out: the /16 supernet of a
        # documentation /24 is not itself a documentation range, and the
        # fixture guard rejects such literals — correctly.
        import ipaddress
        out = group_blocks(self.PAIRS, 16, False, routable_only=False)
        expected = {
            str(ipaddress.ip_network(f"{ip}/16", strict=False))
            for _h, ip in self.PAIRS if _h != "bad"
        }
        assert set(out) == expected
        # Not asserted: that a wider prefix yields fewer blocks. RFC 5737
        # provides only /24s, so two documentation addresses can never share a
        # /16 — there is no fixture that could demonstrate it.

    def test_unparseable_addresses_are_dropped(self):
        out = group_blocks(self.PAIRS, 24, False, routable_only=False)
        assert not any("bad" in hosts for hosts in out.values())

    def test_routable_only_drops_non_global(self):
        # The production default. Also the reason the flag exists: with it on,
        # documentation addresses cannot reach the grouping at all.
        assert group_blocks(self.PAIRS, 24, False) == {}

    def test_private_space_is_dropped_by_default(self):
        assert group_blocks([("h", "10.0.0.1")], 24, False) == {}
