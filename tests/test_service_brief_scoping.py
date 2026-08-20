"""A check nobody carries is not a healthy check (ADR 128).

`generate_service_brief` builds its per-protocol table from `blocked_by_check`,
which is populated only from checks that FAILED. So a key carried by no host
produces no rows — and the empty-rows branch rendered "all healthy".

That is a green verdict on a protocol nobody measures, and it fails in the
opposite direction from the health-matrix defect (ADR 126): the matrix scored
absence as DOWN, this scored it as fine. The second is worse, because DOWN gets
investigated and green does not.

Live, a premium fleet carries none of one configured key while serving that
protocol under several differently-named check items.
"""

from __future__ import annotations

import pathlib
import time

import pytest

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp.tools import service_brief

CARRIED = "carried_check.sh[{HOST.IP}]"
ORPHAN = "orphan_check.sh[{HOST.IP}]"

HOSTS = [
    {"hostid": "1", "host": "edge-aq9001", "groups": [{"name": "prod"}]},
    {"hostid": "2", "host": "edge-bv9001", "groups": [{"name": "prod"}]},
]


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setattr(service_brief, "KEY_service_PRIMARY", CARRIED, raising=False)
    monkeypatch.setattr(service_brief, "KEY_service_SECONDARY", ORPHAN, raising=False)
    monkeypatch.setattr(service_brief, "KEY_service_TERTIARY", "", raising=False)


def _run(tmp_path, carried_value: str) -> str:
    """Both hosts carry CARRIED; NO host carries ORPHAN."""
    now = int(time.time())

    def item_get(params):
        if "tags" in params or "search" in params:
            return []                       # traffic discovery: no NIC items
        key = (params.get("filter") or {}).get("key_")
        if isinstance(key, list):           # the service-key fetch
            return [{"hostid": h["hostid"], "key_": CARRIED,
                     "lastvalue": carried_value, "state": "0",
                     "lastclock": str(now)} for h in HOSTS]
        return []                           # cpu / fallback traffic

    client = RecordingClient({"host.get": HOSTS, "item.get": item_get})
    out = run_tool(service_brief, "generate_service_brief", client,
                   output_dir=str(tmp_path))
    files = list(pathlib.Path(tmp_path).glob("*.html"))
    assert files, f"no report written: {out}"
    return files[0].read_text()


class TestUncarriedCheck:
    def test_a_check_no_host_carries_is_not_reported_healthy(self, tmp_path):
        html = _run(tmp_path, "1")
        assert "no host carries this check" in html
        assert "not measured, not healthy" in html

    def test_the_orphan_row_is_not_the_all_healthy_row(self, tmp_path):
        """The exact defect: both cases produced zero rows, one verdict."""
        html = _run(tmp_path, "1")
        orphan_row = next(r for r in html.split("<tr>") if "orphan_check" in r)
        carried_row = next(r for r in html.split("<tr>") if "carried_check" in r)
        assert "all healthy" not in orphan_row
        assert "all healthy" in carried_row          # genuinely healthy

    def test_a_carried_check_with_no_failures_still_says_healthy(self, tmp_path):
        # The fix must not turn every green into a warning.
        html = _run(tmp_path, "1")
        assert "all healthy" in html
        assert "host(s) carry it" in html

    def test_the_section_renders_even_when_nothing_is_failing(self, tmp_path):
        """Previously gated on `if blocked_by_check`.

        With no failures anywhere the whole section was omitted — taking the
        "nobody carries this key" disclosure with it, exactly when a reader
        would conclude the fleet is fine.
        """
        html = _run(tmp_path, "1")           # every check passing
        assert "Blocked Servers by Check" in html
        assert "no host carries this check" in html


class TestSlaDashboardDisclosesItsCoverage:
    """Skipping a host without the item is right; skipping it silently is not.

    `get_sla_dashboard` reads ONE configured key and drops every host that does
    not carry it. Dropping is correct — no evidence must not become a down vote
    — but the output reads as a whole-fleet SLA, so a fleet serving under a
    different key vanishes from the denominator with nothing to show for it.
    """

    def _run(self, carriers: list[str]) -> str:
        import time as _t

        from zbbx_mcp.tools import executive
        now = int(_t.time())
        hosts = [{"hostid": str(n), "host": f"edge-aq900{n}",
                  "groups": [{"name": "prod"}]} for n in (1, 2, 3)]

        def item_get(params):
            if "tags" in params or "search" in params:
                return []
            return [{"hostid": h, "lastvalue": "1", "state": "0",
                     "lastclock": str(now)} for h in carriers]

        return run_tool(executive, "get_sla_dashboard",
                        RecordingClient({"host.get": hosts, "item.get": item_get}))

    def test_hosts_without_the_item_are_counted_and_named_as_excluded(self,
                                                                     monkeypatch):
        from zbbx_mcp.tools import executive
        monkeypatch.setattr(executive, "KEY_service_PRIMARY", CARRIED, raising=False)
        out = self._run(carriers=["1"])          # 1 of 3 carries it
        assert "2 enabled host(s) carry no item" in out
        assert "not counted up, not counted down" in out

    def test_no_note_when_every_host_carries_it(self, monkeypatch):
        from zbbx_mcp.tools import executive
        monkeypatch.setattr(executive, "KEY_service_PRIMARY", CARRIED, raising=False)
        out = self._run(carriers=["1", "2", "3"])
        assert "carry no item" not in out
