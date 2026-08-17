"""Dead protocols behind an UP-if-any host (ADR 114).

The live case this exists for: one protocol check reading 0 on every host of a
fleet for a full day, with zero alerts, because each host stayed UP on its
other protocols. As per-host rows that is hundreds of lines nobody reads; the
finding is the RATIO.
"""

import time

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp.tools import dead_protocols as dp_mod
from zbbx_mcp.tools.dead_protocols import (
    ALIVE,
    DIED,
    HOST_DARK,
    NEVER_UP,
    TOO_YOUNG,
    aggregate_by_check,
    classify_protocol,
)

NB = 10_000
RECENT = set(range(NB - 24, NB))


class TestClassify:
    def test_answered_in_window_is_alive(self):
        assert classify_protocol(RECENT, RECENT, RECENT, NB).state == ALIVE

    def test_single_pass_anywhere_in_window_is_still_alive(self):
        # UP-if-any is the rule; one answer is enough. The tool must not
        # contradict the availability definition, only see behind it.
        assert classify_protocol({NB - 20}, RECENT, RECENT, NB).state == ALIVE

    def test_never_passed_is_never_up(self):
        v = classify_protocol(set(), RECENT, RECENT, NB)
        assert v.state == NEVER_UP and v.dead_h > 0

    def test_passed_only_before_the_window_is_died(self):
        old = set(range(NB - 40, NB - 30))
        v = classify_protocol(old, RECENT | old, RECENT, NB)
        assert v.state == DIED
        assert v.dead_h == 30            # last pass 30h ago
        assert v.since_bucket == NB - 30

    def test_too_few_observed_hours_is_no_verdict(self):
        # A verdict from two samples is an artefact, not a finding.
        v = classify_protocol(set(), set(range(NB - 2, NB)), RECENT, NB)
        assert v.state == TOO_YOUNG
        assert v.dead_h == 0

    def test_fully_dark_host_belongs_to_the_sla_not_here(self):
        # Listing every protocol of a machine that is entirely down would bury
        # the real finding under noise.
        assert classify_protocol(set(), RECENT, set(), NB).state == HOST_DARK

    def test_too_young_is_checked_before_host_dark(self):
        v = classify_protocol(set(), set(range(NB - 2, NB)), set(), NB)
        assert v.state == TOO_YOUNG


class TestAggregate:
    def _row(self, host, key="p_check", kind=NEVER_UP, dead_h=24):
        return {"hostname": host, "key": key, "kind": kind, "dead_h": dead_h}

    def test_dead_everywhere_is_flagged_fleet_wide(self):
        rows = [self._row(f"node-{i}") for i in range(5)]
        agg = aggregate_by_check(rows, {"p_check": 5})
        assert agg[0]["fleet_wide"] is True
        assert agg[0]["dead"] == 5 and agg[0]["judged"] == 5

    def test_dead_on_some_is_not_fleet_wide(self):
        rows = [self._row(f"node-{i}") for i in range(2)]
        agg = aggregate_by_check(rows, {"p_check": 9})
        assert agg[0]["fleet_wide"] is False

    def test_denominator_comes_from_judged_not_from_the_failures(self):
        # Counting only failures would make every key read 100% dead, which
        # would make the ratio — the whole point — meaningless.
        rows = [self._row("node-a")]
        assert aggregate_by_check(rows, {"p_check": 40})[0]["judged"] == 40

    def test_tiny_sample_is_not_a_fleet_claim(self):
        # "Dead on 1 of 1 host" is a host fault, not a platform outage.
        rows = [self._row("node-a")]
        assert aggregate_by_check(rows, {"p_check": 1})[0]["fleet_wide"] is False

    def test_fleet_wide_sorts_above_partial(self):
        rows = ([self._row(f"n{i}", key="everywhere") for i in range(4)]
                + [self._row("n0", key="somewhere")])
        agg = aggregate_by_check(rows, {"everywhere": 4, "somewhere": 20})
        assert agg[0]["key"] == "everywhere"


class TestWire:
    def _client(self, values_by_item, value_type="3", key="proto_a_check.sh[{HOST.IP}]",
                healthy_sibling=True):
        """One host per entry. Each gets the check under test plus — unless
        disabled — a second check that always answers.

        The sibling is what makes this the real scenario: without another
        protocol holding the host UP, a silent check means the whole machine
        is dark, which is the SLA's finding and not this tool's.
        """
        now = int(time.time())
        nb = now // 3600
        hosts, items, trends = [], [], []
        for n, (iid, ups) in enumerate(values_by_item.items()):
            hid = str(100 + n)
            hosts.append({"hostid": hid, "host": f"node-fj{n}",
                          "groups": [{"name": "edge"}]})
            items.append({"itemid": iid, "hostid": hid, "key_": key,
                          "name": "check", "value_type": value_type})
            for h in range(1, 25):
                trends.append({"itemid": iid, "clock": str((nb - h) * 3600),
                               "value_max": "1" if (25 - h) in ups else "0"})
            if healthy_sibling:
                sib = f"9{iid}"
                items.append({"itemid": sib, "hostid": hid,
                              "key_": "proto_b_check.sh[{HOST.IP}]",
                              "name": "check", "value_type": "3"})
                for h in range(1, 25):
                    trends.append({"itemid": sib, "clock": str((nb - h) * 3600),
                                   "value_max": "1"})
        return RecordingClient({"host.get": hosts, "item.get": items,
                                "trend.get": trends})

    def test_dead_on_every_host_renders_as_a_platform_row(self):
        # Three hosts, same check, none ever passing.
        c = self._client({"1": set(), "2": set(), "3": set()})
        out = run_tool(dp_mod, "detect_dead_protocols", c)
        assert "platform, not host" in out
        assert "3/3" in out

    def test_requests_value_max_not_value_avg(self):
        c = self._client({"1": set(), "2": set(), "3": set()})
        run_tool(dp_mod, "detect_dead_protocols", c)
        tr = [p for m, p in c.calls if m == "trend.get"][0]
        assert "value_max" in tr["output"] and "value_avg" not in tr["output"]

    def test_discovery_is_by_key_pattern_not_configured_service_keys(self):
        # Looking only at the three configured keys is how a fleet-wide outage
        # stayed invisible.
        c = self._client({"1": set()})
        run_tool(dp_mod, "detect_dead_protocols", c)
        ig = [p for m, p in c.calls if m == "item.get"][0]
        assert ig["search"]["key_"] == "*check*"
        assert ig["searchWildcardsEnabled"] is True

    def test_healthy_fleet_says_so_plainly(self):
        c = self._client({"1": set(range(1, 25)), "2": set(range(1, 25))})
        out = run_tool(dp_mod, "detect_dead_protocols", c)
        assert "Every judged protocol check answered" in out

    def test_non_uint_items_are_ignored(self):
        # Text/float "check" items exist (GEO checks return strings); a 0/1
        # verdict on them would be meaningless.
        c = self._client({"1": set()}, value_type="4", healthy_sibling=False)
        out = run_tool(dp_mod, "detect_dead_protocols", c)
        assert "No 0/1 protocol-check items" in out

    def test_a_fully_dark_host_is_deferred_not_reported(self):
        # No sibling: the machine itself is down, which the SLA already owns.
        # Reporting each of its protocols here would bury the real findings.
        c = self._client({"1": set()}, healthy_sibling=False)
        out = run_tool(dp_mod, "detect_dead_protocols", c)
        assert "platform, not host" not in out
        assert "could NOT be judged" in out
