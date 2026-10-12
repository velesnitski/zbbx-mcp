"""Wire tests for ``get_maps`` / ``get_map_detail``. Fixtures are synthetic."""

import httpx

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp.tools import maps


def _boom(_params):
    raise httpx.ConnectError("unreachable")


def test_get_maps_counts_elements_and_links_client_side():
    data = [
        {"sysmapid": "1", "name": "AQ edge", "width": "800", "height": "600",
         "selements": [{"selementid": "a"}, {"selementid": "b"}], "links": [{"linkid": "l"}]},
        {"sysmapid": "2", "name": "BV edge", "width": "400", "height": "300",
         "selements": [], "links": []},
        {"sysmapid": "3", "name": "Broken"},
    ]
    c = RecordingClient({"map.get": data})
    out = run_tool(maps, "get_maps", c)
    p = c.sent("map.get")
    assert p["selectSelements"] == ["selementid"] and p["selectLinks"] == ["linkid"]
    assert "**Found: 3 maps**" in out
    assert "**AQ edge** — 2 elements, 1 links (800×600) (mapid: 1)" in out
    assert "**BV edge** — 0 elements, 0 links (400×300)" in out
    assert "**Broken** — ? elements, ? links (?×?) (mapid: 3)" in out


def test_get_maps_empty_and_error():
    assert run_tool(maps, "get_maps", RecordingClient({"map.get": []})) == "No maps found."
    out = run_tool(maps, "get_maps", RecordingClient({"map.get": _boom}))
    assert out.startswith("Error querying Zabbix:")


def test_get_map_detail_lists_elements_and_caps_links_at_20():
    links = [{"selementid1": str(i), "selementid2": str(i + 1), "color": "00FF00"} for i in range(23)]
    data = [{
        "sysmapid": "7", "name": "Fleet", "width": "1024", "height": "768",
        "selements": [
            {"elementtype": "0", "label": "srv-aq9001"},
            {"elementtype": "3", "label": "app_free"},
            {"elementtype": "9", "label": "mystery"},
        ],
        "links": links,
    }]
    c = RecordingClient({"map.get": data})
    out = run_tool(maps, "get_map_detail", c, map_id="7")
    p = c.sent("map.get")
    assert p["sysmapids"] == ["7"] and p["selectSelements"] == "extend"
    assert out.startswith("# Map: Fleet")
    assert "**Size:** 1024×768" in out
    assert "## Elements (3)" in out
    assert "- [Host] srv-aq9001" in out and "- [Host group] app_free" in out
    assert "- [?] mystery" in out
    assert "## Links (23)" in out
    assert "- Element 0 ↔ Element 1 (color: #00FF00)" in out
    assert "- Element 19 ↔ Element 20" in out and "Element 20 ↔" not in out
    assert "*... and 3 more links*" in out


def test_get_map_detail_without_elements_or_links():
    c = RecordingClient({"map.get": [{"sysmapid": "8", "name": "Empty"}]})
    out = run_tool(maps, "get_map_detail", c, map_id="8")
    assert "# Map: Empty" in out and "## Elements" not in out and "## Links" not in out


def test_get_map_detail_not_found_and_error():
    out = run_tool(maps, "get_map_detail", RecordingClient({"map.get": []}), map_id="99")
    assert out == "Map '99' not found."
    out = run_tool(maps, "get_map_detail", RecordingClient({"map.get": _boom}), map_id="1")
    assert out.startswith("Error querying Zabbix:")
