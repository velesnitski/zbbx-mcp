"""Pure-helper tests for alert triage (tasks 164/165, ADR 067).

Fixtures use generic placeholder hosts/triggers — the regexes only care
about token shape (hyphen/dot tokens, bare `letters+digits`), not real names.
"""

from zbbx_mcp.alert_triage import (
    classify_host_triage,
    classify_match,
    detect_severity,
    detect_state,
    extract_host_candidates,
    parse_alert_line,
    top_severity_label,
)


class TestDetectSeverity:
    def test_finds_word(self):
        assert detect_severity("Disaster: Service Down") == "Disaster"
        assert detect_severity("High | 9 hosts | Agent error") == "High"

    def test_info_normalised(self):
        assert detect_severity("info: foo") == "Information"

    def test_none(self):
        assert detect_severity("some line with no severity") is None


class TestDetectState:
    def test_resolved(self):
        assert detect_state("✅ RESOLVED: foo on bar") == "resolved"
        assert detect_state("trigger recovered") == "resolved"

    def test_problem(self):
        assert detect_state("🔴 REAL PROBLEM: foo") == "problem"
        assert detect_state("trigger is firing") == "problem"

    def test_none(self):
        assert detect_state("just a hostname line db14") is None


class TestExtractHostCandidates:
    def test_single_hyphen_host(self):
        assert extract_host_candidates("Service: TLS UDP 443 error on node-eu-br3") == ["node-eu-br3"]

    def test_multiple_comma_separated(self):
        out = extract_host_candidates("Service Down — node-eu-a1, node-eu-b2, node-us-c3")
        assert out == ["node-eu-a1", "node-eu-b2", "node-us-c3"]

    def test_domain(self):
        assert extract_host_candidates("HTTPS check example.com") == ["example.com"]

    def test_multi_vip_suffix_dropped(self):
        # "bb2" is a VIP suffix of the preceding host, not its own candidate.
        assert extract_host_candidates("Service error on node-eu-a1 bb2") == ["node-eu-a1"]

    def test_bare_short_host_kept(self):
        assert extract_host_candidates("Watchdog DOWN on db1") == ["db1"]

    def test_ignores_ports_and_durations(self):
        # 8080, 20m are not hosts.
        assert extract_host_candidates("Service error on port 8080 for 20m") == []

    def test_in_token_bare_match_suppressed(self):
        # "br3" inside "node-eu-br3" must not become a second candidate.
        assert extract_host_candidates("error on node-eu-br3") == ["node-eu-br3"]

    def test_dedupes_preserving_order(self):
        out = extract_host_candidates("db14 problem, db14 again, node-eu-x1")
        assert out == ["db14", "node-eu-x1"]


class TestParseAlertLine:
    def test_full_line(self):
        p = parse_alert_line("🔴 Disaster: Service Down — node-eu-a1, node-eu-b2")
        assert p["severity"] == "Disaster"
        assert p["claimed_state"] == "problem"
        assert p["hosts"] == ["node-eu-a1", "node-eu-b2"]
        assert "Service Down" in p["trigger"]

    def test_empty(self):
        p = parse_alert_line("")
        assert p["hosts"] == [] and p["severity"] is None


class TestClassifyMatch:
    def test_not_found(self):
        assert classify_match("foo", [])["status"] == "NOT_FOUND"

    def test_exact_wins_over_fuzzy(self):
        found = [{"host": "node-eu-a1", "hostid": "1"},
                 {"host": "node-eu-a1 bb2", "hostid": "2"}]
        m = classify_match("node-eu-a1", found)
        assert m["status"] == "EXACT" and m["chosen"]["hostid"] == "1"

    def test_single_fuzzy(self):
        found = [{"host": "node-eu-a1 bb2", "hostid": "2"}]
        m = classify_match("node-eu-a1", found)
        assert m["status"] == "FUZZY" and m["chosen"]["hostid"] == "2"

    def test_ambiguous(self):
        found = [{"host": "a", "hostid": "1"}, {"host": "b", "hostid": "2"}]
        m = classify_match("x", found)
        assert m["status"] == "AMBIGUOUS" and m["chosen"] is None


class TestClassifyHostTriage:
    def test_recovered_when_no_active(self):
        v, action = classify_host_triage([], is_symptom=False)
        assert v == "recovered" and "stale" in action.lower()

    def test_symptom(self):
        probs = [{"name": "x", "severity": "4"}]
        v, _ = classify_host_triage(probs, is_symptom=True)
        assert v == "symptom_of_cluster"

    def test_real_now_reports_top_severity(self):
        probs = [{"name": "x", "severity": "2"}, {"name": "y", "severity": "5"}]
        v, action = classify_host_triage(probs, is_symptom=False)
        assert v == "real_now" and "Disaster" in action


class TestTopSeverityLabel:
    def test_picks_highest(self):
        assert top_severity_label([{"severity": "2"}, {"severity": "4"}]) == "High"

    def test_empty(self):
        assert top_severity_label([]) == "Not classified"


# --- Wire-contract tests for the orchestration (tools/triage.py) ---------
# The pure helpers above never touch Zabbix; these exercise the actual
# client.call sequence — the layer where the live -32602 "unexpected
# parameter selectHosts" bug lived (problem.get rejects selectHosts).

import asyncio  # noqa: E402

from zbbx_mcp.tools import triage as triage_mod  # noqa: E402


class _RecordingClient:
    """Records every (method, params) and returns canned wire results."""

    def __init__(self, problems, trigs, hosts):
        self.calls = []
        self._problems, self._trigs, self._hosts = problems, trigs, hosts

    async def call(self, method, params):
        self.calls.append((method, params))
        if method == "host.get":
            return self._hosts
        if method == "problem.get":
            return self._problems
        if method == "trigger.get":
            return self._trigs
        return []


class _CaptureMCP:
    def __init__(self):
        self.fn = None

    def tool(self):
        def deco(f):
            self.fn = f
            return f
        return deco


class _StubResolver:
    def __init__(self, client):
        self._client = client

    def resolve(self, instance):
        return self._client


def _run_triage(client, text):
    mcp = _CaptureMCP()
    triage_mod.register(mcp, _StubResolver(client))
    return asyncio.run(mcp.fn(text))


class TestTriageWireContract:
    def _client(self):
        return _RecordingClient(
            problems=[{"eventid": "9", "name": "Service Down", "severity": "5",
                       "clock": "1", "objectid": "77", "suppressed": "0"}],
            trigs=[{"triggerid": "77", "hosts": [{"hostid": "1"}],
                    "dependencies": []}],
            hosts=[{"hostid": "1", "host": "node-eu-a1", "name": "node-eu-a1"}],
        )

    def test_problem_get_omits_selecthosts(self):
        # The bug: problem.get must NOT carry selectHosts (Zabbix rejects it).
        client = self._client()
        _run_triage(client, "🔴 Disaster: Service Down on node-eu-a1")
        pget = next(p for m, p in client.calls if m == "problem.get")
        assert "selectHosts" not in pget

    def test_trigger_get_carries_selecthosts(self):
        # The fix maps problem→host through trigger.get (which DOES support it).
        client = self._client()
        _run_triage(client, "🔴 Disaster: Service Down on node-eu-a1")
        tget = next(p for m, p in client.calls if m == "trigger.get")
        assert tget.get("selectHosts") == ["hostid"]

    def test_problem_attributed_to_host_via_trigger(self):
        # A live problem on the resolved host's trigger → real_now (not the
        # old empty-by_host → spurious "recovered").
        out = _run_triage(self._client(),
                          "✅ RESOLVED: Service Down on node-eu-a1")
        assert "real_now" in out  # feed claimed RESOLVED; re-query says otherwise
