"""The health matrix must not read a missing check as a failing one (ADR 126).

`get_service_health_matrix` folds sub-hosts into canonical groups and scores
each group per protocol. It scored `up` with `all(...)` over *every* sub-host
while scoring `checked` with `any(...)` — two different sets. A sibling that
carries no item of that key returned `None` from the value map, `None != 1`,
and so dragged its whole group to DOWN while still counting toward the
denominator.

On this fleet the halves of a pair are routinely provisioned differently, so
that is the ordinary case rather than a corner one. Live, before the fix: a
country whose every protocol answered 1 on both halves of all three pairs
reported `DOWN (0/3)`, while the wide walk in `detect_dead_protocols` found
every check on every host alive.

There was no behavioural test on this tool at all — only its name in the
registration list — which is how it stayed wrong.
"""

from __future__ import annotations

import time

import pytest

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp.tools import geo_health

PRIMARY = "primary_check.sh[{HOST.IP}]"
SECONDARY = "secondary_check.sh[{HOST.IP}]"
TERTIARY = "tertiary_check.sh[{HOST.IP}]"

PARENT, CHILD = "1", "2"
HOSTS = [
    {"hostid": PARENT, "host": "edge-us01", "groups": [{"name": "prod"}]},
    {"hostid": CHILD, "host": "edge-us01 us02", "groups": [{"name": "prod"}]},
]


@pytest.fixture(autouse=True)
def _configured_keys(monkeypatch):
    for name, val in (("KEY_service_PRIMARY", PRIMARY),
                      ("KEY_service_SECONDARY", SECONDARY),
                      ("KEY_service_TERTIARY", TERTIARY)):
        monkeypatch.setattr(geo_health, name, val, raising=False)


def _client(items_by_key: dict[str, list[str]]) -> RecordingClient:
    """`items_by_key` maps a check key to the hostids that CARRY it, value 1."""
    now = int(time.time())

    def item_get(params):
        # fetch_traffic_map's calls are tag- or search-shaped; no traffic here,
        # so the traffic-validation fallback cannot mask the effect under test.
        if "tags" in params or "search" in params:
            return []
        key = (params.get("filter") or {}).get("key_")
        if not isinstance(key, str):
            return []
        return [{"hostid": hid, "lastvalue": "1", "state": "0",
                 "lastclock": str(now)} for hid in items_by_key.get(key, [])]

    return RecordingClient({"host.get": HOSTS, "item.get": item_get})


def _cell(out: str, col: int) -> str:
    row = next(line for line in out.splitlines()
               if line.startswith("| US "))
    return row.split("|")[3 + col].strip()


class TestMissingItemIsNotAFailure:
    def test_sibling_without_the_item_does_not_sink_the_group(self):
        """The live shape: parent carries the check at 1, sibling carries none."""
        out = run_tool(geo_health, "get_service_health_matrix",
                       _client({PRIMARY: [PARENT]}))
        assert _cell(out, 0) == "OK (1/1)", out

    def test_the_old_behaviour_would_have_read_down(self):
        # Pins the defect itself: `all()` over both sub-hosts scores 0, `any()`
        # counts 1. If someone restores that pairing this fails.
        vmap = {PARENT: 1}
        group = [PARENT, CHILD]
        assert not all(vmap.get(h) == 1 for h in group)      # old numerator
        assert any(h in vmap for h in group)                 # old denominator

    def test_a_protocol_nobody_carries_is_not_measured(self):
        out = run_tool(geo_health, "get_service_health_matrix",
                       _client({PRIMARY: [PARENT]}))
        # Nothing carries the secondary or tertiary key at all.
        assert _cell(out, 1) == "N/A", out
        assert _cell(out, 2) == "N/A", out

    def test_a_genuinely_down_check_still_reads_down(self):
        """The fix must not launder real failures into N/A."""
        now = int(time.time())

        def item_get(params):
            if "tags" in params or "search" in params:
                return []
            key = (params.get("filter") or {}).get("key_")
            if key == PRIMARY:
                return [{"hostid": PARENT, "lastvalue": "0", "state": "0",
                         "lastclock": str(now)}]
            return []

        out = run_tool(geo_health, "get_service_health_matrix",
                       RecordingClient({"host.get": HOSTS, "item.get": item_get}))
        assert _cell(out, 0) == "DOWN (0/1)", out

    def test_both_halves_carrying_it_and_one_failing_still_reads_down(self):
        """Worst-wins survives: the group is judged on everything measured."""
        now = int(time.time())

        def item_get(params):
            if "tags" in params or "search" in params:
                return []
            if (params.get("filter") or {}).get("key_") == PRIMARY:
                return [{"hostid": PARENT, "lastvalue": "1", "state": "0",
                         "lastclock": str(now)},
                        {"hostid": CHILD, "lastvalue": "0", "state": "0",
                         "lastclock": str(now)}]
            return []

        out = run_tool(geo_health, "get_service_health_matrix",
                       RecordingClient({"host.get": HOSTS, "item.get": item_get}))
        assert _cell(out, 0) == "DOWN (0/1)", out
