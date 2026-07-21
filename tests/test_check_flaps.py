"""detect_check_flaps tests (task 174, ADR 090).

Pure-core classification (the audit's three proven facts) + wire contract.
Fixtures are synthetic.
"""

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp.tools import check_flaps as flaps_mod
from zbbx_mcp.tools.check_flaps import classify_flap_minutes, is_test_check

M = 1_000_000  # a minute bucket base


class TestIsTestCheck:
    def test_bracketed_and_token_forms_match(self):
        assert is_test_check("[TEST] service check")
        assert is_test_check("Service TEST check")
        assert is_test_check("service-test-probe")

    def test_lookalikes_do_not_match(self):
        for name in ("latest check", "attestation probe", "contest", "smartest"):
            assert not is_test_check(name), name

    def test_empty_safe(self):
        assert not is_test_check("")


class TestClassifyFlapMinutes:
    """The audit's three facts, as invariants."""

    def _cc(self):
        return {"h1": "AA", "h2": "BB", "h3": "AA"}

    def test_fleet_correlated_distant_dips_are_prober_noise(self):
        # Same minute, two hosts in different countries -> noise, not counted
        # toward any host's rate.
        dips = {("h1", "i1"): {M}, ("h2", "i2"): {M}}
        out = classify_flap_minutes(dips, set(), self._cc(), 1.0)
        assert out["h1"]["fleet_noise_min"] == 1
        assert out["h2"]["fleet_noise_min"] == 1
        assert out["h1"]["rate_per_day"] == 0.0
        assert out["h2"]["rate_per_day"] == 0.0

    def test_same_country_pair_is_not_fleet_noise(self):
        # Two hosts, same country — a shared DC event is a real event, not
        # prober noise (distinct-country requirement).
        dips = {("h1", "i1"): {M}, ("h3", "i3"): {M}}
        out = classify_flap_minutes(dips, set(), self._cc(), 1.0)
        assert out["h1"]["fleet_noise_min"] == 0
        assert out["h1"]["prod_flap_min"] == 1

    def test_three_hosts_unknown_country_fallback(self):
        cc = {"h1": "", "h2": "", "h3": ""}
        dips = {("h1", "i1"): {M}, ("h2", "i2"): {M}, ("h3", "i3"): {M}}
        out = classify_flap_minutes(dips, set(), cc, 1.0)
        assert all(out[h]["fleet_noise_min"] == 1 for h in ("h1", "h2", "h3"))

    def test_host_correlated_two_services_is_real_event(self):
        # >=2 prod checks on ONE host, same minute -> host event.
        dips = {("h1", "i1"): {M}, ("h1", "i2"): {M}}
        out = classify_flap_minutes(dips, set(), self._cc(), 1.0)
        assert out["h1"]["host_event_min"] == 1
        assert out["h1"]["prod_flap_min"] == 0
        assert out["h1"]["rate_per_day"] == 1.0

    def test_test_only_dip_is_script_noise_weight_zero(self):
        # The audit: a TEST-class check flaps ~3x the prod check -> tracked
        # separately, never in the rate.
        dips = {("h1", "t1"): {M, M + 1, M + 2}, ("h1", "i1"): {M + 5}}
        out = classify_flap_minutes(dips, {"t1"}, self._cc(), 1.0)
        assert out["h1"]["test_noise_min"] == 3
        assert out["h1"]["prod_flap_min"] == 1
        assert out["h1"]["rate_per_day"] == 1.0   # only the prod dip counts

    def test_test_items_do_not_create_fleet_noise(self):
        # TEST-check dips on distant hosts must not fabricate prober noise.
        dips = {("h1", "t1"): {M}, ("h2", "t2"): {M}}
        out = classify_flap_minutes(dips, {"t1", "t2"}, self._cc(), 1.0)
        assert out["h1"]["fleet_noise_min"] == 0
        assert out["h1"]["test_noise_min"] == 1

    def test_chronic_low_grade_rate_accumulates(self):
        # The invisible-to-triggers class: 1-2 min dips across many hours.
        minutes = {M + i * 60 for i in range(30)}   # 30 isolated dip-minutes
        dips = {("h1", "i1"): minutes}
        out = classify_flap_minutes(dips, set(), self._cc(), 3.0)
        assert out["h1"]["prod_flap_min"] == 30
        assert out["h1"]["rate_per_day"] == 10.0   # 30 / 3d — chronic

    def test_no_dips_no_hosts(self):
        assert classify_flap_minutes({}, set(), {}, 1.0) == {}


class TestDetectCheckFlapsWire:
    def _client(self, history):
        return RecordingClient({
            "host.get": [
                {"hostid": "1", "host": "edge-aa1", "groups": [{"name": "edge"}]},
            ],
            "hostgroup.get": [{"groupid": "9"}],
            "item.get": [
                {"itemid": "i1", "hostid": "1", "name": "svc check",
                 "key_": "svc_check.sh", "value_type": "3"},
            ],
            "history.get": history,
            "event.get": [],
        })

    def test_requires_scope(self, monkeypatch):
        monkeypatch.setenv("ZABBIX_SERVICE_CHECK_KEY", "svc_check.sh")
        out = run_tool(flaps_mod, "detect_check_flaps", self._client([]))
        assert "required" in out

    def test_matrix_and_candidates(self, monkeypatch):
        # data.py reads the env at import; patch the module constants instead.
        monkeypatch.setattr(flaps_mod, "KEY_service_PRIMARY", "svc_check.sh")
        history = [
            {"itemid": "i1", "clock": str(60_000_000 + i * 3600), "value": "0"}
            for i in range(31)
        ] + [{"itemid": "i1", "clock": "60000030", "value": "1"}]
        client = self._client(history)
        out = run_tool(flaps_mod, "detect_check_flaps", client, group="edge")
        # wire: history requested with the item's value_type and window
        sent = client.sent("history.get")
        assert sent["history"] == 3 and sent["itemids"] == ["i1"]
        # 31 isolated dips / 3d > 10/day and zero problem events -> candidate
        assert "edge-aa1" in out
        assert "trigger candidates" in out

    def test_no_service_keys_message(self, monkeypatch):
        monkeypatch.setattr(flaps_mod, "KEY_service_PRIMARY", "")
        monkeypatch.setattr(flaps_mod, "KEY_service_SECONDARY", "")
        monkeypatch.setattr(flaps_mod, "KEY_service_TERTIARY", "")
        out = run_tool(flaps_mod, "detect_check_flaps", self._client([]),
                       group="edge")
        assert "No service check keys" in out
