"""An unreported session count is not a session count of zero (ADR 130).

`get_traffic_report` read connections with `host_conns.get(hid, 0)`, so a host
carrying no item for the configured connections key was indistinguishable from
one reporting no clients. Both printed `0`.

On a fleet where that key is not deployed, every row read `0` connections and
`–` bandwidth-per-client: a measurement gap wearing the costume of a finding,
and the third instance of this class found in one day (ADR 126, ADR 128).
"""

from __future__ import annotations

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp.tools import traffic as traffic_mod

CONN_KEY = "sessions_total"
HOSTS = [
    {"hostid": "1", "host": "edge-aq9001", "groups": [{"name": "prod"}],
     "interfaces": [{"ip": "192.0.2.1"}]},
    {"hostid": "2", "host": "edge-bv9001", "groups": [{"name": "prod"}],
     "interfaces": [{"ip": "192.0.2.2"}]},
]


def _run(monkeypatch, conn_rows):
    # The API now returns lastclock because the tool asks for it; a fixture
    # without one models a response that no longer occurs. Fill a live clock
    # where absent so each test keeps its original meaning — an EXPLICIT
    # lastclock "0" is left alone, because that is the sentinel under test.
    conn_rows = [{"lastclock": "1760000000", **r} for r in conn_rows]
    monkeypatch.setattr(traffic_mod, "KEY_CONNECTIONS", CONN_KEY, raising=False)

    def item_get(params):
        key = (params.get("filter") or {}).get("key_")
        if key == CONN_KEY:
            return conn_rows
        if isinstance(key, (list, tuple)):          # traffic keys
            return [{"hostid": "1", "lastvalue": "8000000"},
                    {"hostid": "2", "lastvalue": "4000000"}]
        return []

    return run_tool(traffic_mod, "get_traffic_report",
                    RecordingClient({"host.get": HOSTS, "item.get": item_get}))


def _row(out: str, host: str) -> str:
    return next(ln for ln in out.splitlines() if ln.startswith(f"| {host} "))


class TestAbsentIsNotZero:
    def test_a_host_with_no_connections_item_reads_unmeasured(self, monkeypatch):
        out = _run(monkeypatch, conn_rows=[])
        assert _row(out, "edge-aq9001").split("|")[5].strip() == "–"
        assert "unknown rather than zero" in out

    def test_a_genuine_zero_still_reads_zero(self, monkeypatch):
        """The fix must not launder a real zero into 'unmeasured'."""
        out = _run(monkeypatch, conn_rows=[{"hostid": "1", "lastvalue": "0"}])
        assert _row(out, "edge-aq9001").split("|")[5].strip() == "0"

    def test_a_measured_count_is_unaffected(self, monkeypatch):
        out = _run(monkeypatch, conn_rows=[{"hostid": "1", "lastvalue": "40"}])
        row = _row(out, "edge-aq9001")
        assert row.split("|")[5].strip() == "40"
        assert row.split("|")[6].strip() != "–"      # bw/client now derivable

    def test_the_note_counts_only_the_unmeasured_rows(self, monkeypatch):
        out = _run(monkeypatch, conn_rows=[{"hostid": "1", "lastvalue": "40"}])
        assert "on 1 of 2 server(s)" in out

    def test_no_note_when_everything_is_measured(self, monkeypatch):
        out = _run(monkeypatch, conn_rows=[{"hostid": "1", "lastvalue": "40"},
                                           {"hostid": "2", "lastvalue": "7"}])
        assert "unknown rather than zero" not in out

    def test_sorting_puts_unmeasured_last_not_lowest(self, monkeypatch):
        """Unmeasured must not masquerade as the worst real value."""
        monkeypatch.setattr(traffic_mod, "KEY_CONNECTIONS", CONN_KEY, raising=False)

        def item_get(params):
            key = (params.get("filter") or {}).get("key_")
            if key == CONN_KEY:
                return [{"hostid": "2", "lastvalue": "5", "lastclock": "1760000000"}]
            if isinstance(key, (list, tuple)):
                return [{"hostid": "1", "lastvalue": "8000000"},
                        {"hostid": "2", "lastvalue": "4000000"}]
            return []

        out = run_tool(traffic_mod, "get_traffic_report",
                       RecordingClient({"host.get": HOSTS, "item.get": item_get}),
                       sort_by="connections")
        body = [ln for ln in out.splitlines() if ln.startswith("| edge-")]
        assert body[0].startswith("| edge-bv9001")   # measured 5 outranks unmeasured


class TestNeverCollectedIsNotZero:
    """ADR 137: lastclock=0 means the item never produced a value."""

    def test_a_never_collected_item_reads_unmeasured_not_zero(self, monkeypatch):
        # Same row as `test_a_genuine_zero_still_reads_zero`, but the clock says
        # it never collected. Printed blind, a host moving megabits showed
        # "0 connections" — impossible, and therefore never a measurement.
        out = _run(monkeypatch, conn_rows=[{"hostid": "1", "lastvalue": "0", "lastclock": "0"}])
        assert "| – |" in out, out
        assert "unmeasured" in out.lower() or "shown as –" in out, out

    def test_the_fetch_asks_for_lastclock(self, monkeypatch):
        # Without it the sentinel cannot be honoured, and the parser fails
        # closed — every row would read unmeasured. Guard the request itself.
        import zbbx_mcp.tools.traffic as traffic_mod
        from tests.wiretest import RecordingClient, run_tool
        monkeypatch.setattr(traffic_mod, "KEY_CONNECTIONS", CONN_KEY, raising=False)
        client = RecordingClient({"host.get": [{"hostid": "1", "host": "srv-nl01",
                                                "groups": [{"name": "g"}], "interfaces": []}]})
        run_tool(traffic_mod, "get_traffic_report", client)
        conn_calls = [pr for mth, pr in client.calls
                      if mth == "item.get" and pr.get("filter", {}).get("key_") == CONN_KEY]
        assert conn_calls, "connections item.get was not issued"
        assert "lastclock" in conn_calls[0]["output"]
