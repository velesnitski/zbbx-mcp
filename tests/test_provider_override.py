"""Operator-supplied provider ranges (ADR 120).

A hand-maintained allocation table cannot be complete — there is no registry of
every hosting provider, and allocations move between them. Worse, a range
recorded wrongly does not fail loudly: it attributes an address to the wrong
provider, confidently.

A deployment knows its own address space exactly and can point at an
authoritative dataset, so the built-in table is a generic default and the
precise mapping comes from configuration.
"""

from __future__ import annotations

import ipaddress
import json
import pathlib

import pytest

from zbbx_mcp import classify as classify_mod
from zbbx_mcp.classify import PROVIDER_CIDRS, detect_provider


@pytest.fixture(autouse=True)
def _reset():
    classify_mod._EXTRA_PROVIDER_NETS = None
    yield
    classify_mod._EXTRA_PROVIDER_NETS = None


def _cfg(monkeypatch, value):
    monkeypatch.setenv("ZABBIX_PROVIDER_CIDRS", value)
    classify_mod._EXTRA_PROVIDER_NETS = None


class TestOverride:
    def test_unset_changes_nothing(self):
        assert classify_mod.get_extra_provider_nets() == []
        # A built-in range still resolves exactly as before.
        net = ipaddress.ip_network(next(iter(PROVIDER_CIDRS.values()))[0],
                                   strict=False)
        assert detect_provider(str(net.network_address + 1)) != "Other"

    def test_inline_json_is_consulted(self, monkeypatch):
        _cfg(monkeypatch, json.dumps({"LocalHost Inc": ["203.0.113.0/24"]}))
        assert detect_provider("203.0.113.9") == "LocalHost Inc"

    def test_file_path_is_consulted(self, monkeypatch, tmp_path):
        f = tmp_path / "providers.json"
        f.write_text(json.dumps({"FromFile": ["198.51.100.0/24"]}))
        _cfg(monkeypatch, str(f))
        assert detect_provider("198.51.100.7") == "FromFile"

    def test_operator_ranges_win_over_the_builtin_table(self, monkeypatch):
        # The point of the hook: a local mapping is more precise than a
        # hand-maintained public guess, so it must be searched first.
        prov, cidrs = next(iter(PROVIDER_CIDRS.items()))
        net = ipaddress.ip_network(cidrs[0], strict=False)
        probe = str(net.network_address + 1)
        assert detect_provider(probe) == prov            # baseline
        _cfg(monkeypatch, json.dumps({"Operator Override": [str(net)]}))
        assert detect_provider(probe) == "Operator Override"

    def test_most_specific_override_wins_among_overrides(self, monkeypatch):
        _cfg(monkeypatch, json.dumps({
            "Broad": ["203.0.113.0/24"], "Narrow": ["203.0.113.8/29"],
        }))
        assert detect_provider("203.0.113.9") == "Narrow"
        assert detect_provider("203.0.113.200") == "Broad"

    def test_unusable_config_disables_rather_than_half_applies(self, monkeypatch):
        # A partial merge would be worse than none: some hosts would resolve
        # against operator data and others silently against the built-in, with
        # nothing saying which.
        for bad in ('{"X": ["not-a-cidr"]}', "{oops", '["not","an","object"]'):
            _cfg(monkeypatch, bad)
            assert classify_mod.get_extra_provider_nets() == []

    def test_unknown_input_is_still_unknown(self, monkeypatch):
        _cfg(monkeypatch, json.dumps({"X": ["203.0.113.0/24"]}))
        assert detect_provider("not-an-ip") == "Unknown"

    def test_addresses_outside_every_range_stay_other(self, monkeypatch):
        _cfg(monkeypatch, json.dumps({"X": ["203.0.113.0/24"]}))
        assert detect_provider("192.0.2.1") == "Other"


class TestBuiltinTableShape:
    """The built-in default is a generic starting point, not a precise map.

    Two properties keep it useful as a default: it covers the providers any
    address will most often belong to, and every entry describes an allocation
    rather than an individual machine.
    """

    def test_covers_the_major_public_clouds(self):
        majors = {"AWS", "Azure", "Google Cloud", "Cloudflare", "DigitalOcean",
                  "Linode", "Akamai", "Oracle Cloud", "Alibaba Cloud", "Vultr"}
        assert majors <= set(PROVIDER_CIDRS), majors - set(PROVIDER_CIDRS)

    def test_every_entry_is_a_network_level_allocation(self):
        # Provider detection answers "whose allocation is this address in",
        # so every entry describes a block. /24 is the narrowest that is ever
        # meaningful here; anything longer is a machine, not an allocation.
        narrow = []
        for prov, cidrs in PROVIDER_CIDRS.items():
            for c in cidrs:
                n = ipaddress.ip_network(c, strict=False)
                if n.prefixlen > 24:
                    narrow.append(f"{prov}: {c} (/{n.prefixlen})")
        assert not narrow, "blocks narrower than /24: " + ", ".join(narrow)

    def test_the_table_is_a_broad_reference_set(self):
        # This is a general provider reference and is expected to grow. The
        # floor keeps it from being quietly hollowed out; there is no ceiling,
        # because breadth is the whole point of shipping it.
        assert len(PROVIDER_CIDRS) >= 100
        assert sum(len(v) for v in PROVIDER_CIDRS.values()) >= 1000


class TestGeneratedTable:
    """The table is loaded from `data/provider_cidrs.json`, not compiled in.

    See `scripts/gen_provider_cidrs.py` — the file is derived from the public
    prefix-to-AS dataset, so it can be reproduced rather than trusted.
    """

    def test_the_data_file_ships_with_the_package(self):
        assert pathlib.Path(classify_mod._DATA_FILE).is_file()

    def test_every_cidr_in_the_file_parses(self):
        # A malformed entry would be silently skipped at match time and the
        # address would resolve to "Other" — a wrong answer with no signal.
        bad = []
        for prov, cidrs in PROVIDER_CIDRS.items():
            for c in cidrs:
                try:
                    ipaddress.ip_network(c, strict=True)
                except ValueError as exc:
                    bad.append(f"{prov}: {c} ({exc})")
        assert not bad, "unparseable or non-network entries: " + ", ".join(bad)

    def test_a_missing_data_file_degrades_rather_than_raises(self, monkeypatch):
        # Packaged data going missing is a build defect. Import must not die
        # over it: the server keeps serving and provider detection answers
        # "Other", which is honest rather than wrong.
        monkeypatch.setattr(classify_mod, "_DATA_FILE", "/nonexistent/x.json")
        assert classify_mod._load_provider_cidrs() == {}

    def test_a_corrupt_data_file_degrades_rather_than_raises(
            self, monkeypatch, tmp_path):
        f = tmp_path / "provider_cidrs.json"
        f.write_text("{not json")
        monkeypatch.setattr(classify_mod, "_DATA_FILE", str(f))
        assert classify_mod._load_provider_cidrs() == {}
