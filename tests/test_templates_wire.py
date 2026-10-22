"""Template tools wire test: ``template.get`` shape and rendering, the exact
``host.massadd`` / ``host.massremove`` payloads link / unlink send, and the
rollback journal entries that let ``rollback_last`` undo them.
Fixtures are synthetic.
"""

import httpx

from tests.wiretest import JournalingClient, RecordingClient, run_tool
from zbbx_mcp.rollback import Action
from zbbx_mcp.tools import rollback_tools, templates

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


def test_link_template_adds_with_massadd_and_journals():
    c = JournalingClient()
    out = run_tool(templates, "link_template", c, host_id="1", template_id="501")
    # host.massadd adds; `templates` on host.update would replace the whole list.
    assert c.calls == [("host.massadd", {"hosts": [{"hostid": "1"}],
                                         "templates": [{"templateid": "501"}]})]
    assert out == "Template 501 linked to host 1."
    [e] = c.rollback_log.entries
    assert (e.action, e.object_type, e.object_id, e.snapshot) == \
        (Action.CREATE, "template_link", "501", {"hostid": "1", "templateid": "501"})


def test_link_template_rollback_unlinks_without_clear():
    c = JournalingClient()
    run_tool(templates, "link_template", c, host_id="1", template_id="501")
    out = run_tool(rollback_tools, "rollback_last", c)
    assert c.calls[-1] == ("host.massremove", {"hostids": ["1"], "templateids": ["501"]})
    assert out.startswith("Rolled back link: unlinked template 501 from host 1")
    assert c.rollback_log.entries == []


def test_link_template_error_journals_nothing():
    def boom(_p):
        raise ValueError("No permissions to referred object")

    c = JournalingClient({"host.massadd": boom})
    out = run_tool(templates, "link_template", c, host_id="1", template_id="501")
    assert out == "Error linking template: No permissions to referred object"
    assert c.rollback_log.entries == []


def test_unlink_template_keeps_items_unless_clear():
    plain = JournalingClient()
    out = run_tool(templates, "unlink_template", plain, host_id="1", template_id="501")
    assert plain.calls == [("host.massremove", {"hostids": ["1"], "templateids": ["501"]})]
    assert out == "Template 501 unlinked from host 1 (inherited items and triggers kept)."
    [e] = plain.rollback_log.entries
    assert (e.action, e.object_type, e.object_id, e.snapshot) == \
        (Action.DELETE, "template_link", "501",
         {"hostid": "1", "templateid": "501", "cleared": False})

    clear = JournalingClient()
    out_c = run_tool(templates, "unlink_template", clear, host_id="1", template_id="501",
                     clear=True)
    assert clear.calls == [("host.massremove", {"hostids": ["1"], "templateids_clear": ["501"]})]
    assert out_c.startswith("Template 501 unlinked and cleared from host 1.")
    assert "cannot be undone" in out_c
    [e] = clear.rollback_log.entries
    assert e.snapshot["cleared"] is True


def test_unlink_template_rollback_relinks():
    c = JournalingClient()
    run_tool(templates, "unlink_template", c, host_id="1", template_id="501")
    out = run_tool(rollback_tools, "rollback_last", c)
    assert c.calls[-1] == ("host.massadd", {"hosts": [{"hostid": "1"}],
                                            "templates": [{"templateid": "501"}]})
    assert out.startswith("Rolled back unlink: relinked template 501 to host 1")
    assert "history" not in out
    assert c.rollback_log.entries == []

    c = JournalingClient()
    run_tool(templates, "unlink_template", c, host_id="1", template_id="501", clear=True)
    out = run_tool(rollback_tools, "rollback_by_index", c, index=0)
    assert c.calls[-1][0] == "host.massadd"
    assert "history" in out and "not restored" in out


def test_unlink_template_error():
    def boom(_p):
        raise httpx.ReadTimeout("slow")

    c = JournalingClient({"host.massremove": boom})
    out = run_tool(templates, "unlink_template", c, host_id="1", template_id="501")
    assert out == "Error unlinking template: slow"
    assert c.rollback_log.entries == []
