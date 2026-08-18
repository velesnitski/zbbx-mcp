"""Low coverage is disclosed, not left to look like a finding (ADR 122).

A provider table showing a large `Other` count, or a report with every city
blank, is indistinguishable from a real result — the reader cannot tell
"nothing matched" from "nothing is configured". Both notes say which, and name
the thing that fixes it.

This is the same rule the rest of the codebase follows for absent values: an
unmeasured quantity must never render as if it were measured.
"""

from __future__ import annotations

import json

import pytest

from zbbx_mcp import classify as classify_mod
from zbbx_mcp.classify import datacenter_coverage_note, provider_coverage_note


@pytest.fixture(autouse=True)
def _reset():
    classify_mod._EXTRA_PROVIDER_NETS = None
    classify_mod._EXTRA_DC_NETS = None
    yield
    classify_mod._EXTRA_PROVIDER_NETS = None
    classify_mod._EXTRA_DC_NETS = None


class TestProviderNote:
    def test_silent_when_coverage_is_good(self):
        assert provider_coverage_note(900, 1000) == ""
        assert provider_coverage_note(1000, 1000) == ""

    def test_silent_on_no_data(self):
        # Nothing to report on, and dividing by zero is not a diagnosis.
        assert provider_coverage_note(0, 0) == ""

    def test_speaks_up_when_coverage_is_poor(self):
        note = provider_coverage_note(78, 820)
        assert "742 of 820" in note
        assert "ZABBIX_PROVIDER_CIDRS" in note

    def test_unconfigured_points_at_setup(self):
        note = provider_coverage_note(78, 820)
        assert "bootstrap_provider_overrides.py" in note

    def test_configured_points_at_extending_instead(self, monkeypatch):
        # Already set up: the useful advice is to extend it, not to create it.
        monkeypatch.setenv("ZABBIX_PROVIDER_CIDRS",
                           json.dumps({"X": ["203.0.113.0/24"]}))
        classify_mod._EXTRA_PROVIDER_NETS = None
        note = provider_coverage_note(78, 820)
        assert "Extend" in note
        assert "bootstrap_provider_overrides.py" not in note

    def test_the_threshold_actually_discriminates(self):
        # A rule that fires always, or never, is not a rule.
        assert provider_coverage_note(79, 100) != ""
        assert provider_coverage_note(80, 100) == ""


class TestDatacenterNote:
    def test_speaks_up_when_nothing_is_configured(self):
        assert classify_mod.DATACENTER_CIDRS == {}
        note = datacenter_coverage_note()
        assert "ZABBIX_DATACENTER_CIDRS" in note
        assert "bootstrap_datacenter_ranges.py" in note

    def test_silent_once_configured(self, monkeypatch):
        monkeypatch.setenv("ZABBIX_DATACENTER_CIDRS",
                           json.dumps({"X": [["203.0.113.0/24", "Rivertown, ZZ"]]}))
        classify_mod._EXTRA_DC_NETS = None
        assert datacenter_coverage_note() == ""

    def test_silent_when_a_builtin_table_is_present(self, monkeypatch):
        monkeypatch.setattr(classify_mod, "DATACENTER_CIDRS",
                            {"X": [("203.0.113.0/24", "Rivertown, ZZ")]})
        assert datacenter_coverage_note() == ""
