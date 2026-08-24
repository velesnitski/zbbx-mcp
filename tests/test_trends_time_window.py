"""A time argument that was not understood must not become a default (ADR 134).

`_parse_epoch` returned 0 for both "not supplied" and "could not parse". The
caller reads 0 as "not supplied" and substitutes its own window, so an
unintelligible argument silently became an absent one:

    get_trends(item, time_from="now-13d", time_till="now-8d")
    → the most recent `limit` hours

Not an error, not an empty result — a full table of real, correctly formatted
data for a window nobody asked about. Found while reconstructing an incident:
the "pre-event baseline" it returned was entirely post-event.

`"now-7d"` is the shape that exposes it. It reads like a relative duration;
`parse_time` accepts only the bare `"7d"`. Nothing in the output distinguished
the two.
"""

from __future__ import annotations

import time

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp.tools import events as events_mod

ITEM = [{"itemid": "1", "name": "Incoming traffic", "key_": "net.if.in[ens3]",
         "units": "bps", "value_type": "3"}]


def _rows(n: int, end: int | None = None):
    end = end or int(time.time())
    return [{"itemid": "1", "clock": str(end - i * 3600), "num": "60",
             "value_min": "1000", "value_avg": "2000", "value_max": "3000"}
            for i in range(n)]


def _client(rows=None):
    return RecordingClient({"item.get": ITEM, "trend.get": rows or _rows(5)})


class TestUnparseableInputIsRefused:
    def test_now_minus_form_is_rejected_not_silently_defaulted(self):
        c = _client()
        out = run_tool(events_mod, "get_trends", c,
                       item_id="1", time_from="now-13d", time_till="now-8d")
        assert "Unrecognised time value" in out
        assert "now-13d" in out

    def test_the_error_names_the_accepted_formats(self):
        out = run_tool(events_mod, "get_trends", _client(),
                       item_id="1", time_from="garbage")
        assert "7d" in out            # the relative form that works
        assert "2026-" in out or "ISO date" in out

    def test_it_does_not_query_zabbix_with_a_wrong_window(self):
        """Refusing must happen BEFORE the call, not after."""
        c = _client()
        run_tool(events_mod, "get_trends", c, item_id="1", time_from="now-13d")
        assert not any(m == "trend.get" for m, _ in c.calls)

    def test_only_the_offending_argument_is_named(self):
        out = run_tool(events_mod, "get_trends", _client(),
                       item_id="1", time_from="7d", time_till="nonsense")
        assert "time_till" in out
        assert "time_from" not in out


class TestValidInputStillWorks:
    def test_no_time_arguments_uses_the_default_window(self):
        out = run_tool(events_mod, "get_trends", _client(), item_id="1")
        assert "Trends:" in out
        assert "Unrecognised" not in out

    def test_iso_dates_are_honoured(self):
        c = _client()
        run_tool(events_mod, "get_trends", c, item_id="1",
                 time_from="2026-08-17", time_till="2026-08-20", limit=100)
        sent = c.sent("trend.get")
        # 2026-08-17T00:00Z .. 2026-08-20T00:00Z
        assert sent["time_from"] == 1786924800
        assert sent["time_till"] == 1787184000

    def test_a_bare_relative_duration_is_accepted(self):
        c = _client()
        run_tool(events_mod, "get_trends", c, item_id="1", time_from="48h")
        assert any(m == "trend.get" for m, _ in c.calls)


class TestWindowClipping:
    def test_a_span_wider_than_limit_is_disclosed(self):
        """`limit` doubles as the window span.

        Asking for 72h with limit=10 reads only the last 10h of it, and said
        nothing — so a caller reasoning about a multi-day range was shown a
        sliver of it with no indication.
        """
        c = _client()
        out = run_tool(events_mod, "get_trends", c, item_id="1",
                       time_from="2026-08-17", time_till="2026-08-20", limit=10)
        assert "Window clipped" in out
        assert "limit=10" in out

    def test_no_warning_when_limit_covers_the_window(self):
        out = run_tool(events_mod, "get_trends", _client(), item_id="1",
                       time_from="2026-08-17", time_till="2026-08-20", limit=100)
        assert "Window clipped" not in out

    def test_no_warning_without_an_explicit_start(self):
        out = run_tool(events_mod, "get_trends", _client(), item_id="1", limit=5)
        assert "Window clipped" not in out
