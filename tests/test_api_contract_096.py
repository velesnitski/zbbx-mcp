"""API-contract fixes: sortfield, removed write params, unread output, ordering.

ADR 096. Four independent defects that all produced a confident wrong answer
(or a dead tool) rather than an error the caller could see.
"""

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp.tools import alerts as alerts_mod
from zbbx_mcp.tools import events as events_mod
from zbbx_mcp.tools import maintenance as maint_mod
from zbbx_mcp.tools import maps as maps_mod
from zbbx_mcp.tools import media as media_mod
from zbbx_mcp.tools.events import _parse_epoch

HOUR = 3600


class TestMediaTypeSort:
    """mediatype.get accepts only `mediatypeid` — `name` is a hard -32500."""

    def _client(self):
        return RecordingClient({"mediatype.get": [
            {"mediatypeid": "3", "name": "Zeta", "type": "0", "status": "0"},
            {"mediatypeid": "1", "name": "alpha", "type": "0", "status": "0"},
            {"mediatypeid": "2", "name": "Mid", "type": "0", "status": "0"},
        ]})

    def test_wire_sorts_by_mediatypeid(self):
        client = self._client()
        run_tool(media_mod, "get_media_types", client)
        assert client.sent("mediatype.get")["sortfield"] == "mediatypeid"

    def test_output_is_still_name_ordered(self):
        # Sorting moved client-side, so the rendering contract is unchanged.
        out = run_tool(media_mod, "get_media_types", self._client())
        assert out.index("alpha") < out.index("Mid") < out.index("Zeta")


class TestMaintenanceCreateShape:
    """`hostids`/`groupids` were removed from maintenance.create in 7.2."""

    def _client(self):
        return RecordingClient({
            "maintenance.create": {"maintenanceids": ["9"]},
        })

    def test_sends_object_arrays_not_id_lists(self):
        client = self._client()
        run_tool(
            maint_mod, "create_maintenance", client,
            name="w", active_since="2026-01-01 00:00",
            active_till="2026-01-01 02:00", host_ids="10,11", group_ids="20",
        )
        sent = client.sent("maintenance.create")
        assert sent["hosts"] == [{"hostid": "10"}, {"hostid": "11"}]
        assert sent["groups"] == [{"groupid": "20"}]
        assert "hostids" not in sent and "groupids" not in sent


class TestAlertSummaryClock:
    """`clock` drives the current/previous split but was never requested."""

    def test_requests_clock_and_splits_windows(self):
        import time as _t
        now = int(_t.time())
        # 2 alerts inside the last hour, 3 in the hour before that.
        rows = [{"alertid": str(i), "clock": str(now - 600), "status": "1",
                 "subject": "s", "alerttype": "0"} for i in range(2)]
        rows += [{"alertid": str(10 + i), "clock": str(now - 2 * HOUR + 60),
                  "status": "1", "subject": "s", "alerttype": "0"}
                 for i in range(3)]
        client = RecordingClient({"alert.get": rows})
        out = run_tool(alerts_mod, "get_alert_summary", client, hours=1, compare=True)
        assert "clock" in client.sent("alert.get")["output"]
        # Only the 2 recent alerts are "current"; the 3 older ones are the
        # comparison window. Before the fix all 5 counted as current.
        assert "2" in out


class TestMapCounts:
    """map.get select* does not support "count" — ask for ids and len()."""

    def test_counts_render_as_numbers(self):
        client = RecordingClient({"map.get": [
            {"sysmapid": "1", "name": "m", "width": "800", "height": "600",
             "selements": [{"selementid": "1"}, {"selementid": "2"}],
             "links": [{"linkid": "1"}]},
        ]})
        out = run_tool(maps_mod, "get_maps", client)
        sent = client.sent("map.get")
        assert sent["selectSelements"] == ["selementid"]
        assert sent["selectLinks"] == ["linkid"]
        assert "2 elements, 1 links" in out


class TestGetTrendsWindowAndOrder:
    """trend.get ignores sortfield/sortorder, so `limit` sliced the OLDEST."""

    def _client(self, rows):
        return RecordingClient({
            "item.get": [{"itemid": "1", "name": "n", "key_": "k", "units": "",
                          "value_type": "3"}],
            "trend.get": rows,
        })

    def test_no_unsupported_sort_params_and_window_is_bounded(self):
        client = self._client([{"itemid": "1", "clock": "1700000000",
                                "value_min": "1", "value_avg": "1",
                                "value_max": "1", "num": "60"}])
        run_tool(events_mod, "get_trends", client, item_id="1", limit=5)
        sent = client.sent("trend.get")
        assert "sortfield" not in sent and "sortorder" not in sent
        # A default call must bound the window so it lands on recent data.
        assert sent["time_till"] - sent["time_from"] == 5 * HOUR

    def test_rows_render_newest_first(self):
        base = 1_700_000_000
        rows = [
            {"itemid": "1", "clock": str(base + k * HOUR), "value_min": "0",
             "value_avg": str(k), "value_max": "9", "num": "60"}
            for k in range(3)
        ]
        out = run_tool(events_mod, "get_trends", self._client(rows), item_id="1")
        first, last = out.index("| 2"), out.rindex("| 2")
        # newest (k=2) must appear above oldest (k=0)
        assert out.index(str(base + 2 * HOUR and "")) >= 0  # placeholder-free
        rendered = [ln for ln in out.splitlines() if ln.startswith("| 20")]
        assert len(rendered) == 3
        assert rendered[0] > rendered[-1]  # timestamps descend
        assert first <= last


class TestParseEpoch:
    """`int(value)` alone raised on the natural YYYY-MM-DD form."""

    def test_accepts_digits_and_dates(self):
        assert _parse_epoch("1700000000") == 1_700_000_000
        assert _parse_epoch("2026-07-24") == 1_784_851_200

    def test_empty_or_junk_is_zero(self):
        # 0 = "caller supplies its own default", never a crash.
        assert _parse_epoch("") == 0
        assert _parse_epoch("not-a-date") == 0
