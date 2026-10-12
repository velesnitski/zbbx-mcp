"""Template tools wire test: ``template.get`` shape and rendering, and the
exact ``host.update`` payloads link / unlink send. Fixtures are synthetic.

Two behaviours pinned here are worth knowing (see the module tests below):
``link_template`` sends ``templates`` on ``host.update``, which in the Zabbix
API *replaces* the host's whole template list; and ``unlink_template`` sends
``templates_clear`` whether or not ``clear`` is set. Neither write records a
rollback journal entry.
"""

import httpx

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp.tools import templates

TEMPLATES = [
    {"templateid": "501", "host": "Template OS Linux", "description": "",
     "hosts": [{"hostid": "1"}, {"hostid": "2"}, {"hostid": "3"}],
     "items": "42", "triggers": "17"},
    {"templateid": "502", "host": "Template App Free", "description": "d" * 150,
     "hosts": [{"hostid": "1"}], "items": "8", "triggers": "3"},
    {"templateid": "503", "host": "Template Ping", "hosts": []},
]


def test_get_templates_default_request():
    c = RecordingClient({"template.get": TEMPLATES})
    out = run_tool(templates, "get_templates", c)
    assert c.sent("template.get") == {
        "output": ["templateid", "host", "name", "description"],
        "selectHosts": ["hostid", "host"],
        "selectItems": "count",
        "selectTriggers": "count",
        "sortfield": "host",
        "limit": 50,
    }
    assert out.startswith("**Found: 3 templates**\n\n")
    assert "- **Template OS Linux** — 42 items, 17 triggers, linked to 3 hosts (id: 501)" in out
    assert "linked to 1 hosts (id: 502)\n  " + "d" * 100 in out
    assert "d" * 101 not in out
    # Missing counts render as "?" rather than a fake zero.
    assert "- **Template Ping** — ? items, ? triggers, linked to 0 hosts (id: 503)" in out


def test_get_templates_search_host_and_truncation_header():
    c = RecordingClient({"template.get": TEMPLATES[:2]})
    out = run_tool(templates, "get_templates", c, search="Template*", host_id="7", max_results=2)
    p = c.sent("template.get")
    assert p["search"] == {"host": "Template*", "name": "Template*"}
    assert p["searchWildcardsEnabled"] is True and p["searchByAny"] is True
    assert p["hostids"] == ["7"] and p["limit"] == 2
    assert out.startswith("**Found: 2 templates** (showing first 2, more may exist)")


def test_get_templates_empty_and_error():
    assert run_tool(templates, "get_templates", RecordingClient()) == "No templates found."

    def boom(_p):
        raise httpx.ConnectError("down")

    out = run_tool(templates, "get_templates", RecordingClient({"template.get": boom}))
    assert out == "Error querying Zabbix: down"


def test_link_template_payload():
    c = RecordingClient()
    out = run_tool(templates, "link_template", c, host_id="1", template_id="501")
    # `templates` on host.update replaces the host's template list in the
    # Zabbix API — host.massadd would be the additive call.
    assert c.calls == [("host.update", {"hostid": "1", "templates": [{"templateid": "501"}]})]
    assert out == "Template 501 linked to host 1."
    assert c.rollback == []  # no journal entry: not undoable via rollback_last


def test_link_template_error():
    def boom(_p):
        raise ValueError("No permissions to referred object")

    out = run_tool(templates, "link_template", RecordingClient({"host.update": boom}),
                   host_id="1", template_id="501")
    assert out == "Error linking template: No permissions to referred object"


def test_unlink_template_sends_templates_clear_in_both_modes():
    plain = RecordingClient()
    out = run_tool(templates, "unlink_template", plain, host_id="1", template_id="501")
    assert out == "Template 501 unlinked from host 1."

    clear = RecordingClient()
    out_c = run_tool(templates, "unlink_template", clear, host_id="1", template_id="501", clear=True)
    assert out_c == "Template 501 unlinked and cleared from host 1."

    expected = ("host.update", {"hostid": "1", "templates_clear": [{"templateid": "501"}]})
    # Today both modes send the identical clearing payload.
    assert plain.calls == [expected]
    assert clear.calls == [expected]
    assert plain.rollback == [] and clear.rollback == []


def test_unlink_template_error():
    def boom(_p):
        raise httpx.ReadTimeout("slow")

    out = run_tool(templates, "unlink_template", RecordingClient({"host.update": boom}),
                   host_id="1", template_id="501")
    assert out == "Error unlinking template: slow"
