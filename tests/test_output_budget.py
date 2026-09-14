"""The tool owns its output budget (ADR 142).

The server cuts every response at ``ZABBIX_RESPONSE_BUDGET``. For a batch of
trends that cut fell mid-table, after a partial host, and neither side could
say which hosts were missing. The tool now stops on its own, whole hosts at a
time, and its last line names the rest with the exact ``hosts=`` that fetches
them. The same ``hosts=`` reaches the load and traffic reports, and an empty
match on any of the three lists the labels that do exist.

Fixture names use documentation country codes and the reserved band (ADR 127).
"""

from __future__ import annotations

import time

import pytest

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp import budget as budget_mod
from zbbx_mcp import classify as classify_mod
from zbbx_mcp.budget import (
    fit_host_blocks,
    missing_hosts_note,
    no_match_message,
    omitted_hosts_line,
    parse_host_list,
    render_budget,
    response_budget,
)
from zbbx_mcp.server import _compress_response
from zbbx_mcp.tools import inventory_load, traffic, trends_compare

NOW = int(time.time())
METRIC_KEYS = {
    "cpu": "system.cpu.util[,idle]",
    "traffic": "net.if.in[eth0]",
    "load": "system.cpu.load[percpu,avg5]",
}


def _names(n: int, code: str = "aq") -> list[str]:
    return [f"srv-{code}{9001 + i}" for i in range(n)]


def _hosts(names: list[str], group: str = "app_free") -> list[dict]:
    return [{"hostid": str(i + 1), "host": n, "groups": [{"name": group}],
             "interfaces": [{"ip": f"192.0.2.{(i % 200) + 1}"}]}
            for i, n in enumerate(names)]


def _trends_client(hosts: list[dict]) -> RecordingClient:
    def host_get(p):
        wanted = (p.get("filter") or {}).get("host")
        if wanted:
            return [h for h in hosts if h["host"] in wanted]
        if p.get("hostids"):
            return [h for h in hosts if h["hostid"] in p["hostids"]]
        return hosts

    def item_get(p):
        return [
            {"itemid": f"i{h}-{m}", "hostid": h, "key_": key, "lastvalue": "40",
             "lastclock": str(NOW - 60), "name": m, "units": "", "value_type": "0"}
            for h in p.get("hostids", []) for m, key in METRIC_KEYS.items()
        ]

    def trend_get(p):
        return [{"itemid": i, "clock": str(NOW - 3600 * k), "num": "60",
                 "value_min": "10", "value_avg": "50", "value_max": "90"}
                for i in p.get("itemids", []) for k in range(1, 6)]

    return RecordingClient({"host.get": host_get, "item.get": item_get, "trend.get": trend_get})


def _trends(fleet, **kw) -> str:
    return run_tool(trends_compare, "get_trends_batch", _trends_client(fleet), **kw)


def _shown_hosts(out: str, fmt: str) -> dict[str, int]:
    """host -> number of metric lines rendered for it."""
    counts: dict[str, int] = {}
    for ln in out.splitlines():
        if fmt == "table":
            if not ln.startswith("| srv-"):
                continue
            host = ln.split("|")[1].strip()
        else:
            if not ln.startswith("srv-"):
                continue
            host = ln.split("|")[0]
        counts[host] = counts.get(host, 0) + 1
    return counts


# --- the budget is one setting, read in one place -----------------------------


class TestResponseBudget:
    def test_default_matches_the_server_default(self, monkeypatch):
        monkeypatch.delenv("ZABBIX_RESPONSE_BUDGET", raising=False)
        assert response_budget() == budget_mod.DEFAULT_RESPONSE_BUDGET == 6000

    def test_reads_the_same_setting_the_server_uses(self, monkeypatch):
        monkeypatch.setenv("ZABBIX_RESPONSE_BUDGET", "1234")
        assert response_budget() == 1234
        assert render_budget() == 1234 - budget_mod.RENDER_HEADROOM

    def test_zero_disables_both_the_server_cut_and_the_tool_cut(self, monkeypatch):
        monkeypatch.setenv("ZABBIX_RESPONSE_BUDGET", "0")
        assert response_budget() == 0
        assert render_budget() == 0
        assert "[truncated" not in _compress_response("x" * 20_000)

    def test_garbage_falls_back_rather_than_raising(self, monkeypatch):
        monkeypatch.setenv("ZABBIX_RESPONSE_BUDGET", "lots")
        assert response_budget() == budget_mod.DEFAULT_RESPONSE_BUDGET

    def test_server_truncation_follows_the_helper(self, monkeypatch):
        monkeypatch.setenv("ZABBIX_RESPONSE_BUDGET", "300")
        cut = _compress_response("line\n" * 200)
        assert "[truncated" in cut
        assert len(cut) < 400


# --- whole-host fitting ---------------------------------------------------------


class TestFitHostBlocks:
    def _blocks(self, n: int, lines: int = 3) -> list[tuple[str, str]]:
        return [(name, "\n".join(f"{name}|m{k}|1|2|3|4|stable" for k in range(lines)))
                for name in _names(n)]

    def test_under_budget_is_untouched(self):
        blocks = self._blocks(5)
        text, omitted = fit_host_blocks("hdr", blocks, budget=10_000)
        assert omitted == []
        assert text == "hdr\n" + "\n".join(b for _, b in blocks)
        assert "not shown" not in text

    def test_unlimited_budget_never_cuts(self):
        text, omitted = fit_host_blocks("hdr", self._blocks(300), budget=0)
        assert omitted == []
        assert "not shown" not in text

    def test_cut_keeps_whole_hosts_in_order_and_fits(self):
        blocks = self._blocks(200)
        text, omitted = fit_host_blocks("hdr", blocks, budget=2000)
        assert len(text) <= 2000
        kept = [name for name, _ in blocks if name not in omitted]
        assert kept == [name for name, _ in blocks[:len(kept)]]     # a prefix
        for _name, body in blocks[:len(kept)]:
            assert body in text                                     # every metric line
        for name in omitted:
            assert f"{name}|m0" not in text                         # none of theirs

    def test_closing_line_names_up_to_twelve_and_counts_the_rest(self):
        blocks = self._blocks(200)
        text, omitted = fit_host_blocks("hdr", blocks, budget=2000)
        last = text.splitlines()[-1]
        n, total = len(omitted), len(blocks)
        assert last.startswith(f"{n} of {total} hosts not shown: ")
        assert f", +{n - 12} more." in last
        assert f"Request them with hosts={','.join(omitted[:12])} or narrow the filter." in last
        assert omitted[12] not in last

    def test_a_short_tail_is_named_in_full_without_a_more_count(self):
        line = omitted_hosts_line(["srv-aq9001", "srv-aq9002"], total=9)
        assert line == (
            "2 of 9 hosts not shown: srv-aq9001, srv-aq9002. "
            "Request them with hosts=srv-aq9001,srv-aq9002 or narrow the filter."
        )

    def test_the_closing_line_itself_always_fits(self):
        # Every budget from "nothing fits" upward must leave room for the line
        # that names the rest, or the server would cut the one line that matters.
        blocks = self._blocks(40)
        for budget in range(300, 3000, 37):
            text, omitted = fit_host_blocks("hdr", blocks, budget=budget)
            if omitted:
                assert len(text) <= budget, budget
                assert text.endswith("or narrow the filter.")


# --- get_trends_batch: format= and the budget -----------------------------------


class TestTrendsBatchBudget:
    HOSTS = _hosts(_names(200))

    @pytest.mark.parametrize("fmt", ["table", "compact"])
    def test_a_large_cohort_renders_within_budget_and_names_the_rest(self, monkeypatch, fmt):
        monkeypatch.setenv("ZABBIX_RESPONSE_BUDGET", "6000")
        out = _trends(self.HOSTS, format=fmt, max_results=500)
        assert len(out) <= 6000
        assert f"for {len(self.HOSTS)} servers" in out
        shown = _shown_hosts(out, fmt)
        assert 0 < len(shown) < len(self.HOSTS)
        assert set(shown.values()) == {3}, "a shown host carries all three metrics"
        last = out.splitlines()[-1]
        n = len(self.HOSTS) - len(shown)
        assert last.startswith(f"{n} of {len(self.HOSTS)} hosts not shown: ")
        first_omitted = _names(200)[len(shown)]
        assert f"hosts={first_omitted}," in last

    @pytest.mark.parametrize("fmt", ["table", "compact"])
    def test_the_server_never_has_to_cut_it(self, monkeypatch, fmt):
        monkeypatch.setenv("ZABBIX_RESPONSE_BUDGET", "6000")
        monkeypatch.delenv("ZABBIX_COMPACT", raising=False)
        out = _trends(self.HOSTS, format=fmt, max_results=500)
        assert _compress_response(out) == out

    @pytest.mark.parametrize("fmt", ["table", "compact"])
    def test_a_small_cohort_is_untouched(self, monkeypatch, fmt):
        monkeypatch.setenv("ZABBIX_RESPONSE_BUDGET", "6000")
        hosts = _hosts(_names(4))
        out = _trends(hosts, format=fmt)
        assert "not shown" not in out
        assert set(_shown_hosts(out, fmt)) == set(_names(4))

    def test_the_default_format_is_the_markdown_table(self):
        out = _trends(_hosts(_names(2)))
        assert "| Server | Metric | Avg | Peak | Min | Current | Trend |" in out
        assert "| srv-aq9001 | cpu |" in out

    def test_compact_is_one_line_per_host_and_metric_with_units_once(self):
        out = _trends(_hosts(_names(2)), format="compact")
        lines = out.splitlines()
        assert "host|metric|avg|peak|min|now|trend (units: cpu=%, traffic=Mbps, load=ratio)" in lines
        body = [ln for ln in lines if ln.startswith("srv-")]
        assert len(body) == 2 * 3
        assert body[0].startswith("srv-aq9001|cpu|")
        assert body[0].count("|") == 6
        assert "Mbps" not in "\n".join(body)                     # units stated once, not per cell
        assert "|---" not in out

    def test_compact_is_smaller_than_the_table_for_the_same_fixture(self, monkeypatch):
        monkeypatch.setenv("ZABBIX_RESPONSE_BUDGET", "0")      # compare full renders
        hosts = _hosts(_names(60))
        table = _trends(hosts, format="table", max_results=500)
        compact = _trends(hosts, format="compact", max_results=500)
        assert len(compact) < 0.7 * len(table), (len(compact), len(table))
        assert _shown_hosts(compact, "compact") == _shown_hosts(table, "table")

    def test_compact_fits_more_hosts_into_the_same_budget(self, monkeypatch):
        monkeypatch.setenv("ZABBIX_RESPONSE_BUDGET", "6000")
        table = _shown_hosts(_trends(self.HOSTS, format="table", max_results=500), "table")
        compact = _shown_hosts(_trends(self.HOSTS, format="compact", max_results=500), "compact")
        assert len(compact) > len(table)

    def test_daily_aggregation_is_budgeted_too(self, monkeypatch):
        monkeypatch.setenv("ZABBIX_RESPONSE_BUDGET", "3000")
        out = _trends(self.HOSTS, aggregation="daily", max_results=500)
        assert len(out) <= 3000
        assert "hosts not shown" in out
        assert "| Server | Metric |" in out

    def test_an_unknown_format_is_refused_not_guessed(self):
        out = _trends(_hosts(_names(2)), format="csv")
        assert "Unknown format 'csv'" in out

    def test_a_named_set_beyond_the_budget_still_states_the_cut(self, monkeypatch):
        monkeypatch.setenv("ZABBIX_RESPONSE_BUDGET", "1500")
        names = _names(30)
        out = _trends(_hosts(names), hosts=",".join(names), max_results=2)
        assert f"for {len(names)} servers" in out
        assert "hosts not shown" in out


# --- hosts= on the load and traffic reports -------------------------------------


def _load_client(hosts: list[dict]) -> RecordingClient:
    def item_get(p):
        key = (p.get("filter") or {}).get("key_") or []
        if "system.cpu.util[,idle]" in key:
            return [{"hostid": h, "itemid": f"c{h}", "key_": "system.cpu.util[,idle]",
                     "lastvalue": "60", "units": "%"} for h in p.get("hostids", [])]
        return [{"hostid": h, "key_": "net.if.in[eth0]", "lastvalue": "8000000", "units": "bps"}
                for h in p.get("hostids", [])]

    return RecordingClient({"host.get": hosts, "item.get": item_get})


def _load(fleet, **kw) -> str:
    return run_tool(inventory_load, "get_server_load", _load_client(fleet), **kw)


def _traffic_client(hosts: list[dict]) -> RecordingClient:
    def item_get(p):
        key = (p.get("filter") or {}).get("key_")
        if isinstance(key, (list, tuple)):
            return [{"hostid": h["hostid"], "lastvalue": "8000000"} for h in hosts]
        return []

    return RecordingClient({"host.get": hosts, "item.get": item_get})


def _traffic(fleet, **kw) -> str:
    return run_tool(traffic, "get_traffic_report", _traffic_client(fleet), **kw)


class TestHostListHelpers:
    def test_parse_strips_dedupes_and_keeps_order(self):
        assert parse_host_list(" srv-aq9002, srv-aq9001,,srv-aq9002 ") == ["srv-aq9002", "srv-aq9001"]
        assert parse_host_list("") == []

    def test_missing_note_names_the_unknown_and_counts_the_requested(self):
        missing, note = missing_hosts_note(["srv-aq9001", "srv-hm9099"], ["srv-aq9001"])
        assert missing == ["srv-hm9099"]
        assert note == ("_1 of 2 requested host(s) are not enabled hosts in Zabbix: "
                        "srv-hm9099_\n")
        assert missing_hosts_note(["srv-aq9001"], ["srv-aq9001"]) == ([], "")


class TestServerLoadHosts:
    def test_a_named_set_is_the_whole_output(self):
        names = _names(6)
        out = _load(_hosts(names), hosts="srv-aq9002,srv-aq9005")
        assert "(2 servers" in out
        assert "| srv-aq9002 |" in out and "| srv-aq9005 |" in out
        assert "| srv-aq9001 |" not in out

    def test_max_results_cannot_trim_a_named_set(self):
        names = _names(7)
        out = _load(_hosts(names), hosts=",".join(names), max_results=2)
        assert "(7 servers" in out
        for n in names:
            assert f"| {n} |" in out

    def test_an_unknown_name_is_reported_not_dropped(self):
        out = _load(_hosts(_names(2)), hosts="srv-aq9001,srv-hm9099")
        assert "1 of 2 requested host(s) are not enabled hosts in Zabbix: srv-hm9099" in out
        assert "| srv-aq9001 |" in out

    def test_all_names_unknown_is_an_answer_about_the_names(self):
        out = _load(_hosts(_names(2)), hosts="srv-hm9098,srv-hm9099")
        assert out == ("2 of 2 requested host(s) are not enabled hosts in Zabbix: "
                       "srv-hm9098, srv-hm9099.")

    def test_a_named_sub_host_still_inherits_its_parent(self):
        # The list is applied client-side so the parent stays in the fleet the
        # parent map is built from; a server-side filter would drop it.
        hosts = _hosts(["srv-aq9001", "srv-aq9001 aq9002"])
        out = _load(hosts, hosts="srv-aq9001 aq9002")
        row = next(ln for ln in out.splitlines() if ln.startswith("| srv-aq9001 aq9002 |"))
        assert "| 40.0% |" in row                                  # 100 - idle 60, from the parent

    def test_without_hosts_nothing_changes(self):
        names = _names(3)
        out = _load(_hosts(names), max_results=2)
        assert "(2 servers" in out


class TestTrafficReportHosts:
    def test_a_named_set_is_the_whole_output(self):
        out = _traffic(_hosts(_names(5)), hosts="srv-aq9003")
        assert "(1 servers" in out
        assert "| srv-aq9003 |" in out and "| srv-aq9001 |" not in out

    def test_max_results_cannot_trim_a_named_set(self):
        names = _names(7)
        out = _traffic(_hosts(names), hosts=",".join(names), max_results=2)
        assert "(7 servers" in out
        for n in names:
            assert f"| {n} |" in out

    def test_an_unknown_name_is_reported_not_dropped(self):
        out = _traffic(_hosts(_names(2)), hosts="srv-aq9001,srv-hm9099")
        assert "1 of 2 requested host(s) are not enabled hosts in Zabbix: srv-hm9099" in out
        assert "| srv-aq9001 |" in out

    def test_all_names_unknown_is_an_answer_about_the_names(self):
        out = _traffic(_hosts(_names(2)), hosts="srv-hm9098,srv-hm9099")
        assert out == ("2 of 2 requested host(s) are not enabled hosts in Zabbix: "
                       "srv-hm9098, srv-hm9099.")

    def test_no_traffic_is_distinguished_from_no_match(self):
        hosts = _hosts(_names(2))
        out = run_tool(traffic, "get_traffic_report",
                       RecordingClient({"host.get": hosts, "item.get": []}))
        assert out == "No traffic data found for the 2 host(s) matching the filters."

    def test_without_hosts_nothing_changes(self):
        out = _traffic(_hosts(_names(3)), max_results=2)
        assert "(2 servers" in out


# --- an empty match says what exists ---------------------------------------------


@pytest.fixture
def product_map(monkeypatch):
    pmap = {
        "app_free": ("Alpha", "Free"),
        "app_paid": ("Alpha", "Paid"),
        "app_beta": ("Beta", "Free"),
    }
    monkeypatch.setattr(classify_mod, "get_product_map", lambda: pmap)
    return pmap


def _mixed_fleet() -> list[dict]:
    hosts = _hosts(_names(3, "aq"), "app_free")
    hosts += _hosts(_names(2, "bv"), "app_paid")
    hosts += _hosts(_names(1, "hm"), "app_beta")
    for i, h in enumerate(hosts):
        h["hostid"] = str(i + 1)
    return hosts


class TestNoMatchListsWhatExists:
    EXPECTED = ("No servers match the filters. Among 6 enabled hosts, product labels "
                "present: Alpha, Beta; tier labels present: Free, Paid. "
                "Filters match a label exactly.")

    def test_the_message_lists_sorted_distinct_labels(self, product_map):
        assert no_match_message(_mixed_fleet()) == self.EXPECTED

    def test_the_listing_is_capped_and_counts_the_rest(self, monkeypatch):
        pmap = {f"g{i:02d}": (f"Prod{i:02d}", f"Tier{i:02d}") for i in range(14)}
        monkeypatch.setattr(classify_mod, "get_product_map", lambda: pmap)
        hosts = [{"hostid": str(i), "host": f"srv-aq{9001 + i}", "groups": [{"name": f"g{i:02d}"}]}
                 for i in range(14)]
        msg = no_match_message(hosts)
        assert "Prod00, Prod01, Prod02, Prod03, Prod04, Prod05, Prod06, Prod07, Prod08, Prod09, +4 more;" in msg
        assert "Tier09, +4 more." in msg
        assert "Prod10" not in msg

    def test_no_enabled_hosts_at_all_is_said_plainly(self):
        assert no_match_message([]) == "No servers match the filters: no enabled hosts in scope."

    def test_trends_batch(self, product_map):
        out = _trends(_mixed_fleet(), tier="Fre")
        assert out == self.EXPECTED

    def test_trends_batch_keeps_the_missing_note_ahead_of_it(self, product_map):
        # hosts= is a server-side filter on this tool, so the listing is scoped
        # to the named hosts: the one that exists is Free, and Paid was asked.
        out = _trends(_mixed_fleet(), hosts="srv-aq9001,srv-hm9099", tier="Paid")
        assert out.startswith("_1 of 2 requested host(s) are not enabled hosts in Zabbix: srv-hm9099_\n")
        assert out.endswith(
            "No servers match the filters. Among 1 enabled hosts, product labels "
            "present: Alpha; tier labels present: Free. Filters match a label exactly."
        )

    def test_server_load(self, product_map):
        out = _load(_mixed_fleet(), product="Gamma")
        assert out == self.EXPECTED

    def test_traffic_report(self, product_map):
        out = _traffic(_mixed_fleet(), country="zz")
        assert out == self.EXPECTED

    def test_a_typo_is_distinguishable_from_an_empty_set(self, product_map):
        out = _traffic(_mixed_fleet(), tier="Premium")
        assert "tier labels present: Free, Paid" in out
