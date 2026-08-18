"""Reading the reporting side's actual schema (ADR 101 addendum).

`compare_report_facts` judged flat field names the reporting side never
published, so every run returned "nothing comparable" — truthful, and worse
than a wrong answer, because a check that never compares anything reads as a
pass. These two helpers translate the published shape and, separately, report
contradictions inside the file that need no live data to see.
"""

from __future__ import annotations

from zbbx_mcp.tools.crosscheck import adapt_reported_facts, internal_consistency

# The published shape: a `_meta` block plus one entry per country.
SNAPSHOT = {
    "_meta": {"generated": "2026-01-01T00:00", "total_servers": 10,
              "total_countries": 2, "avg_cpu": 5.0},
    "AA": {"servers": 6, "vpn_up": 5, "vpn_total": 6},
    "BB": {"servers": 4, "vpn_up": 4, "vpn_total": 4},
}


class TestAdapt:
    def test_country_count_is_translated(self):
        # Same definition on both sides, so it is safe to judge.
        out, _notes = adapt_reported_facts(SNAPSHOT)
        assert out["countries"] == 2

    def test_server_count_is_not_mapped_onto_host_count(self):
        """The load-bearing decision.

        A server count and a host count are different populations. Mapping one
        onto the other would manufacture a disagreement out of two correct
        numbers — the exact false alarm ADR 101 exists to avoid.
        """
        out, notes = adapt_reported_facts(SNAPSHOT)
        assert "total_hosts" not in out
        assert any("not compared" in n for n in notes)

    def test_country_count_falls_back_to_counting_blocks(self):
        snap = {k: v for k, v in SNAPSHOT.items() if k != "_meta"}
        snap["_meta"] = {"total_servers": 10}
        out, _ = adapt_reported_facts(snap)
        assert out["countries"] == 2

    def test_a_flat_file_is_passed_through_untouched(self):
        flat = {"total_hosts": 5, "countries": 3}
        out, notes = adapt_reported_facts(flat)
        assert out == flat
        assert notes == []


class TestInternalConsistency:
    def test_silent_on_a_coherent_file(self):
        assert internal_consistency(SNAPSHOT) == []

    def test_total_disagreeing_with_the_sum_is_reported(self):
        bad = {**SNAPSHOT, "_meta": {**SNAPSHOT["_meta"], "total_servers": 99}}
        found = internal_consistency(bad)
        assert any("disagrees with itself" in n for n in found)

    def test_country_count_disagreeing_with_blocks_is_reported(self):
        bad = {**SNAPSHOT, "_meta": {**SNAPSHOT["_meta"], "total_countries": 9}}
        assert any("country blocks" in n for n in internal_consistency(bad))

    def test_numerator_above_denominator_is_reported(self):
        # An availability figure over 100% is derivable from such a file.
        bad = {**SNAPSHOT, "AA": {"servers": 6, "vpn_up": 99, "vpn_total": 6}}
        assert any("exceed 100%" in n for n in internal_consistency(bad))

    def test_flat_file_yields_nothing(self):
        assert internal_consistency({"total_hosts": 5}) == []
