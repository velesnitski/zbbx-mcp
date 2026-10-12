"""Wire tests for ``get_media_types`` / ``get_actions``. Fixtures are synthetic."""

import httpx

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp.tools import media


def _boom(_params):
    raise httpx.ConnectError("unreachable")


def test_get_media_types_sorts_by_name_client_side():
    data = [
        {"mediatypeid": "3", "name": "webhook-b", "type": "4", "status": "0",
         "users": [{"userid": "1"}], "maxattempts": "3", "description": "Posts to example.com"},
        {"mediatypeid": "1", "name": "Email", "type": "0", "status": "1", "maxattempts": "1"},
        {"mediatypeid": "2", "name": "custom", "type": "7", "status": "0"},
    ]
    c = RecordingClient({"mediatype.get": data})
    out = run_tool(media, "get_media_types", c)
    # name is not a valid sortfield for mediatype.get (ADR 096)
    assert c.sent("mediatype.get")["sortfield"] == "mediatypeid"
    assert "**Found: 3 media types**" in out
    assert out.index("**custom**") < out.index("**Email**") < out.index("**webhook-b**")
    assert "**Email** (Email) [Disabled]\n  Users: 0 | Max attempts: 1 | mediatypeid: 1" in out
    assert "**custom** (Type 7) [Enabled]" in out
    assert "Users: 1 | Max attempts: 3" in out and "\n  Posts to example.com" in out


def test_get_media_types_empty_and_error():
    assert run_tool(media, "get_media_types", RecordingClient()) == "No media types found."
    out = run_tool(media, "get_media_types", RecordingClient({"mediatype.get": _boom}))
    assert out.startswith("Error querying Zabbix:")


ACTIONS = [
    {"actionid": "1", "name": "Notify ops", "status": "0", "eventsource": "0",
     "operations": [{"operationtype": "0"}, {"operationtype": "1"}, {"operationtype": "42"}]},
    {"actionid": "2", "name": "Autoreg", "status": "1", "eventsource": "2", "operations": []},
]


def test_get_actions_all_sources():
    c = RecordingClient({"action.get": ACTIONS})
    out = run_tool(media, "get_actions", c)
    assert "filter" not in c.sent("action.get")
    assert "**Found: 2 actions**" in out
    assert "**Notify ops** [Enabled] (source: Triggers)\n  Operations: Send message, Remote command, ? | actionid: 1" in out
    assert "**Autoreg** [Disabled] (source: Autoregistration)\n  Operations: none | actionid: 2" in out


def test_get_actions_eventsource_filter_and_empty():
    c = RecordingClient({"action.get": []})
    out = run_tool(media, "get_actions", c, eventsource=3)
    assert c.sent("action.get")["filter"] == {"eventsource": 3}
    assert out == "No actions found."


def test_get_actions_error():
    out = run_tool(media, "get_actions", RecordingClient({"action.get": _boom}))
    assert out.startswith("Error querying Zabbix:")
