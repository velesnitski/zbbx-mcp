"""Cross-system fact diff tests (ADR 101).

The defining property is what the tool REFUSES to judge. A check that fires on
correct data is a check nobody reads, so same-named-but-differently-defined
quantities must come back ADVISORY, never DIVERGE.
"""

import json

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp.tools import crosscheck as cc_mod
from zbbx_mcp.tools.crosscheck import (
    ADVISORY,
    COUNTRYLESS_PRODUCTS,
    DIVERGE,
    DRIFT,
    MATCH,
    MISSING,
    build_live_facts,
    compare_facts,
    summarize,
)


def _host(name, group="edge"):
    return {"hostid": name, "host": name, "groups": [{"name": group}]}


class TestBuildLiveFacts:
    def test_counts_countries_and_totals(self):
        hosts = [_host("node-fj1"), _host("node-fj2"), _host("node-ki1")]
        f = build_live_facts(hosts)
        assert f["total_hosts"] == 3
        assert f["countries"] == 2
        assert f["top_countries"] == {"FJ": 2, "KI": 1}
        assert f["blank_country_hosts"] == 0

    def test_country_sum_always_reconciles_to_the_fleet(self):
        # The invariant the reporting side checks from the aggregate side: if
        # this stops holding, hosts are falling out of country tables.
        hosts = [_host("node-fj1"), _host("no-country-here"), _host("node-ki1")]
        f = build_live_facts(hosts)
        assert f["country_host_sum"] == f["total_hosts"]

    def test_role_named_hosts_are_not_counted_as_missing_data(self):
        # A monitoring/infra host legitimately has no country. Counting it as a
        # gap would make the check fire on every run — and an invariant that
        # always fires is one nobody reads.
        hosts = [
            _host("mon-box", group="monitoring"),
            _host("infra-box", group="Infrastructure"),
            _host("node-fj1"),
        ]
        f = build_live_facts(hosts)
        assert f["countryless_by_design"] == 2
        assert f["blank_country_hosts"] == 0
        assert f["country_host_sum"] == f["total_hosts"] == 3

    def test_a_countryless_host_in_a_country_bearing_product_IS_a_gap(self):
        # The other side of the same rule: an edge host with no derivable
        # country is real missing data, and must be reported.
        f = build_live_facts([_host("no-country-here", group="edge")])
        assert f["blank_country_hosts"] == 1
        assert f["countryless_by_design"] == 0
        assert f["blank_country_sample"] == ["no-country-here"]

    def test_countryless_product_set_matches_the_reporting_side(self):
        # If the two sets drift, both systems compute "missing country" over
        # different populations and the diff reports a defect that isn't there.
        assert {"infrastructure", "monitoring", "unknown", ""} == COUNTRYLESS_PRODUCTS

    def test_empty_fleet_is_coherent(self):
        f = build_live_facts([])
        assert f["total_hosts"] == 0 and f["country_host_sum"] == 0


class TestCompareFacts:
    def _live(self, **over):
        base = {
            "total_hosts": 100, "countries": 10, "country_host_sum": 100,
            "countryless_by_design": 5, "blank_country_hosts": 0,
            "top_countries": {"FJ": 40, "KI": 30},
        }
        base.update(over)
        return base

    def test_identical_is_all_match(self):
        live = self._live()
        rows = compare_facts(dict(live), live)
        assert {r.verdict for r in rows} == {MATCH}
        overall, _ = summarize(rows)
        assert overall == "consistent"

    def test_small_count_delta_is_drift_not_divergence(self):
        # The fleet legitimately changes between the two runs.
        reported = self._live(total_hosts=99)
        rows = compare_facts(reported, self._live(), drift_tolerance=2)
        r = next(x for x in rows if x.field == "total_hosts")
        assert r.verdict == DRIFT
        overall, _ = summarize(rows)
        assert "drift" in overall

    def test_large_delta_is_divergence(self):
        reported = self._live(total_hosts=50)
        rows = compare_facts(reported, self._live(), drift_tolerance=2)
        r = next(x for x in rows if x.field == "total_hosts")
        assert r.verdict == DIVERGE
        overall, _ = summarize(rows)
        assert "DIVERGENCE" in overall

    def test_tolerance_zero_makes_any_delta_diverge(self):
        rows = compare_facts(self._live(total_hosts=99), self._live(), drift_tolerance=0)
        assert next(x for x in rows if x.field == "total_hosts").verdict == DIVERGE

    def test_absent_field_is_missing_not_a_violation(self):
        reported = {"total_hosts": 100}
        rows = compare_facts(reported, self._live())
        assert next(x for x in rows if x.field == "countries").verdict == MISSING

    def test_per_country_only_compares_the_intersection(self):
        # The published list is truncated to its top entries, so a country
        # absent there is not evidence of anything.
        reported = self._live(top_countries={"FJ": 40})
        rows = compare_facts(reported, self._live())
        fields = {r.field for r in rows}
        assert "country[FJ]" in fields
        assert "country[KI]" not in fields

    def test_sla_is_advisory_never_judged(self):
        # Period-integrated there vs point-in-time here. Diffing these would
        # manufacture divergence on correct data — the whole point of ADR 101.
        reported = self._live()
        reported["premium_sla"] = {"weighted_pct": 99.4, "servers": 12}
        rows = compare_facts(reported, self._live())
        r = next(x for x in rows if x.field.startswith("premium_sla"))
        assert r.verdict == ADVISORY
        assert "not comparable" in r.note
        # and it must not affect the overall verdict
        assert summarize(rows)[0] == "consistent"

    def test_erosion_is_advisory_because_thresholds_are_unpublished(self):
        reported = self._live()
        reported["erosion"] = {"eroding": 7, "judged": 40}
        rows = compare_facts(reported, self._live())
        r = next(x for x in rows if x.field.startswith("erosion"))
        assert r.verdict == ADVISORY
        assert "thresholds" in r.note

    def test_nothing_comparable_says_so(self):
        overall, _ = summarize(compare_facts({}, {}))
        assert "nothing comparable" in overall


class TestCompareReportFactsWire:
    def _client(self, hosts):
        return RecordingClient({"host.get": hosts})

    def test_missing_path_is_explained_not_an_error(self, monkeypatch):
        monkeypatch.delenv("ZABBIX_CROSSCHECK_FACTS", raising=False)
        out = run_tool(cc_mod, "compare_report_facts", self._client([]))
        assert "No facts file given" in out

    def test_diff_renders_and_flags_divergence(self, tmp_path, monkeypatch):
        facts = {
            "total_hosts": 2, "countries": 1, "country_host_sum": 2,
            "countryless_by_design": 0, "blank_country_hosts": 0,
            "top_countries": {"FJ": 2},
            "premium_sla": {"weighted_pct": 99.1},
        }
        p = tmp_path / "crosscheck.json"
        p.write_text(json.dumps(facts))
        monkeypatch.setenv("ZBBX_FILE_ROOTS", str(tmp_path))
        # Live fleet has a country the report says nothing about AND a
        # different total -> strict divergence on the counts.
        hosts = [_host("node-fj1"), _host("node-ki1"), _host("node-ki2"),
                 _host("node-ki3"), _host("node-ki4"), _host("node-ki5")]
        out = run_tool(cc_mod, "compare_report_facts", self._client(hosts),
                       facts_path=str(p), drift_tolerance=1)
        assert "DIVERGENCE" in out
        assert "total_hosts" in out
        assert "Investigate:" in out
        # SLA still shown, still not judged
        assert "ADVISORY" in out
        # staleness is always disclosed
        assert "old (mtime)" in out

    def test_path_outside_allowed_roots_is_refused(self, monkeypatch):
        monkeypatch.setenv("ZBBX_FILE_ROOTS", "/nonexistent-root-for-test")
        out = run_tool(cc_mod, "compare_report_facts", self._client([]),
                       facts_path="/etc/passwd")
        assert "Cannot read facts file" in out

    def test_non_object_json_is_rejected(self, tmp_path, monkeypatch):
        p = tmp_path / "bad.json"
        p.write_text("[1, 2, 3]")
        monkeypatch.setenv("ZBBX_FILE_ROOTS", str(tmp_path))
        out = run_tool(cc_mod, "compare_report_facts", self._client([]),
                       facts_path=str(p))
        assert "must contain a JSON object" in out
