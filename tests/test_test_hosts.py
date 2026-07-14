"""Test/staging host detection (ADR 080).

A non-production box left inside a production group silently corrupts every
fleet verdict it lands in. Neither signal is reliable alone — the box may be
named `*-test-*` but grouped in production, or grouped correctly but blandly
named — so the rule is the union of host name and group names.

Fixtures are synthetic: real host and group names have no place in this repo.
"""

import pytest

from zbbx_mcp import classify
from zbbx_mcp.classify import is_test_host
from zbbx_mcp.data import excluded_test_note, partition_test_hosts

PROD_GROUP = "edge"


@pytest.fixture(autouse=True)
def _reset_pattern(monkeypatch):
    # The compiled pattern is cached; clear it so env overrides take effect.
    monkeypatch.setattr(classify, "_TEST_RE", None)
    monkeypatch.delenv("ZABBIX_TEST_NAME_RE", raising=False)
    yield
    classify._TEST_RE = None


def host(name, *groups):
    return {"host": name, "groups": [{"name": g} for g in groups]}


class TestIsTestHostByName:
    @pytest.mark.parametrize("name", [
        "app-deploy-test",     # trailing token
        "test-portal-a",       # leading token
        "edge-test-a1",        # middle token
        "n1-test-core",
        "n2_test_core",        # underscore separators
    ])
    def test_test_names_match(self, name):
        assert is_test_host(host(name, PROD_GROUP)) is True

    @pytest.mark.parametrize("name", [
        "latest-node1", "contest-eu", "fastest-a1", "attestation1", "protest",
    ])
    def test_substring_lookalikes_do_not_match(self, name):
        # A bare "test" substring would swallow every one of these.
        assert is_test_host(host(name, PROD_GROUP)) is False

    def test_plain_production_host(self):
        assert is_test_host(host("edge-a1", PROD_GROUP)) is False


class TestIsTestHostByGroup:
    def test_group_marks_a_blandly_named_host(self):
        assert is_test_host(host("srv-01", "pool_test")) is True

    def test_group_with_space_separators(self):
        # Group names use spaces where host names use dashes — the boundary
        # class must allow whitespace or this case is missed entirely.
        assert is_test_host(host("srv-01", "QA test hosts")) is True

    def test_production_groups_do_not_match(self):
        for g in ("edge", "web", "infra", "core-servers",
                  "Templates/Applications"):
            assert is_test_host(host("srv-01", g)) is False, g

    def test_name_wins_even_inside_a_production_group(self):
        # The real-world case this ADR exists for: a test box that is a full
        # member of a production group. A group-only check would miss it.
        assert is_test_host(host("edge-test-a1", PROD_GROUP)) is True

    def test_string_groups_accepted(self):
        assert is_test_host({"host": "srv-01", "groups": ["pool_test"]}) is True

    def test_missing_groups_key_is_safe(self):
        assert is_test_host({"host": "edge-test-a1"}) is True
        assert is_test_host({"host": "edge-a1"}) is False


class TestEnvOverride:
    def test_custom_pattern(self, monkeypatch):
        monkeypatch.setenv("ZABBIX_TEST_NAME_RE", r"(?:^|[-_])stage(?:[-_]|$)")
        classify._TEST_RE = None
        assert is_test_host(host("srv-stage-1", PROD_GROUP)) is True
        assert is_test_host(host("edge-test-a1", PROD_GROUP)) is False

    def test_invalid_pattern_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("ZABBIX_TEST_NAME_RE", "([unclosed")
        classify._TEST_RE = None
        assert is_test_host(host("edge-test-a1", PROD_GROUP)) is True


class TestPartitionAndNote:
    def _fleet(self):
        return [
            host("edge-a1", PROD_GROUP),
            host("edge-test-a1", PROD_GROUP),
            host("edge-test-b2", PROD_GROUP),
            host("edge-b2", PROD_GROUP),
        ]

    def test_partition_splits_prod_from_test(self):
        prod, test = partition_test_hosts(self._fleet())
        assert [h["host"] for h in prod] == ["edge-a1", "edge-b2"]
        assert [h["host"] for h in test] == ["edge-test-a1", "edge-test-b2"]

    def test_note_names_what_was_dropped(self):
        _, test = partition_test_hosts(self._fleet())
        note = excluded_test_note(test)
        assert "2 test host(s) excluded" in note
        assert "edge-test-a1" in note and "edge-test-b2" in note
        assert "include_test=true" in note

    def test_note_empty_when_nothing_excluded(self):
        assert excluded_test_note([]) == ""

    def test_note_truncates_long_lists(self):
        many = [host(f"srv-test-{i}", PROD_GROUP) for i in range(9)]
        note = excluded_test_note(many, max_names=3)
        assert "9 test host(s) excluded" in note and "+6 more" in note


class TestBulkDiagnoseHonoursExplicitNames:
    """ADR 080 — a scoped sweep drops test boxes; an explicitly named host is
    always diagnosed. Naming a host *is* the request to look at it, and
    silently returning nothing would be the worst possible answer."""

    def _run(self, **kwargs):
        from tests.wiretest import RecordingClient, run_tool
        from zbbx_mcp.tools import diagnose as diagnose_mod

        rec = {"hostid": "1", "host": "edge-test-a1",
               "groups": [{"name": PROD_GROUP}], "interfaces": []}
        client = RecordingClient({"host.get": [rec], "hostgroup.get": [{"groupid": "9"}]})
        out = run_tool(diagnose_mod, "bulk_diagnose", client, **kwargs)
        return out

    def test_explicit_test_host_is_kept(self):
        out = self._run(hosts="edge-test-a1")
        assert "edge-test-a1" in out
        assert "excluded" not in out

    def test_scoped_sweep_drops_and_reports_it(self):
        out = self._run(group=PROD_GROUP)
        assert "1 test host(s) excluded" in out
        assert "edge-test-a1" in out          # named in the footer, not silent

    def test_scoped_sweep_can_keep_them(self):
        out = self._run(group=PROD_GROUP, include_test=True)
        assert "excluded" not in out
