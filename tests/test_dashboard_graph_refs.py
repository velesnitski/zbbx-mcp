"""A dashboard built from graph widgets still references hosts (ADR 131).

Widget references are typed. Only 2/3/4 (group/host/item) were decoded, but a
classic **Graph** widget names a *graphid* — type 6 — and the graph belongs to
its items' hosts. A dashboard built entirely from graph widgets therefore
resolved to zero hosts, and both readers reported that as fact: the detail view
printed no host section, and `find_host_dashboard` answered "not found on any
dashboard" for a host whose graphs are on one.

The host was there. The decoder was not. Same shape as the rest of this class:
what the tool could not read was rendered as what the fleet does not have.
"""

from __future__ import annotations

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp.tools import dashboards as dash
from zbbx_mcp.tools.dashboards import collect_widget_refs

HOSTID = "25664"
HOSTNAME = "edge-aq9001"

GRAPH_PAGES = [{"name": "Page 1", "widgets": [
    {"type": "graph", "fields": [{"type": "6", "value": "901"}]},
    {"type": "graph", "fields": [{"type": "6", "value": "902"}]},
]}]

DASHBOARDS = [{"dashboardid": "163", "name": "Astro overview", "pages": GRAPH_PAGES}]

GRAPHS = [
    {"graphid": "901", "name": "Traffic in",
     "hosts": [{"hostid": HOSTID, "host": HOSTNAME}]},
    {"graphid": "902", "name": "CPU",
     "hosts": [{"hostid": HOSTID, "host": HOSTNAME}]},
]


class TestCollectWidgetRefs:
    def test_graph_widgets_yield_graph_ids(self):
        refs = collect_widget_refs(GRAPH_PAGES)
        assert refs["graphs"] == {"901", "902"}
        assert refs["hosts"] == set()          # none named directly — the bug

    def test_direct_host_fields_still_collected(self):
        refs = collect_widget_refs(
            [{"widgets": [{"type": "problems",
                           "fields": [{"type": "3", "value": HOSTID}]}]}])
        assert refs["hosts"] == {HOSTID}

    def test_an_undecodable_widget_is_counted_not_ignored(self):
        """Pattern-based widgets cannot be resolved to ids — say so."""
        refs = collect_widget_refs(
            [{"widgets": [{"type": "svggraph",
                           "fields": [{"type": "1", "value": "astro-*"}]}]}])
        assert refs["undecoded"] == {"svggraph": 1}
        assert refs["hosts"] == set()

    def test_a_widget_with_no_fields_is_not_undecodable(self):
        # Nothing to read is not the same as failing to read something.
        refs = collect_widget_refs([{"widgets": [{"type": "clock", "fields": []}]}])
        assert refs["undecoded"] == {}


class TestFindHostThroughGraphs:
    def _client(self):
        return RecordingClient({
            "host.get": [{"hostid": HOSTID, "host": HOSTNAME}],
            "dashboard.get": DASHBOARDS,
            "graph.get": GRAPHS,
        })

    def test_a_host_referenced_only_via_a_graph_is_found(self):
        out = run_tool(dash, "find_host_dashboard", self._client(), host_id=HOSTID)
        assert "not found on any dashboard" not in out
        assert "Astro overview" in out
        assert "via graph 'Traffic in'" in out

    def test_a_host_on_no_dashboard_still_reports_not_found(self):
        """The fix must not make everything match."""
        c = RecordingClient({
            "host.get": [{"hostid": "99999", "host": "edge-bv9001"}],
            "dashboard.get": DASHBOARDS,
            "graph.get": GRAPHS,
        })
        out = run_tool(dash, "find_host_dashboard", c, host_id="99999")
        assert "not found on any dashboard" in out

    def test_undecodable_widgets_qualify_a_negative_answer(self):
        c = RecordingClient({
            "host.get": [{"hostid": "99999", "host": "edge-bv9001"}],
            "dashboard.get": [{"dashboardid": "9", "name": "Patterns",
                               "pages": [{"widgets": [
                                   {"type": "svggraph",
                                    "fields": [{"type": "1", "value": "astro-*"}]}]}]}],
            "graph.get": [],
        })
        out = run_tool(dash, "find_host_dashboard", c, host_id="99999")
        assert "not found on any dashboard" in out
        assert "cannot decode" in out          # the negative is qualified


class TestDashboardDetailResolvesGraphs:
    def test_hosts_behind_graph_widgets_are_listed(self):
        c = RecordingClient({
            "dashboard.get": DASHBOARDS,
            "graph.get": GRAPHS,
            "host.get": [{"hostid": HOSTID, "host": HOSTNAME, "status": "0",
                          "groups": [{"name": "astro"}]}],
        })
        out = run_tool(dash, "get_dashboard_detail", c, dashboard_id="163")
        assert "Referenced Hosts (1)" in out
        assert HOSTNAME in out

    def test_the_page_list_names_the_graphs(self):
        c = RecordingClient({
            "dashboard.get": DASHBOARDS,
            "graph.get": GRAPHS,
            "host.get": [{"hostid": HOSTID, "host": HOSTNAME, "status": "0",
                          "groups": [{"name": "astro"}]}],
        })
        out = run_tool(dash, "get_dashboard_detail", c, dashboard_id="163")
        assert "Traffic in" in out and "CPU" in out
        assert "[Graph] Traffic in" in out
