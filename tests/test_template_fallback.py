"""Template-fallback classification (ADR 115).

A host can sit only in a mixed group that classifies to infrastructure while
running a production stack, which makes it invisible to every product-scoped
view. Its template is the deploy's own statement of what it runs, and it
separates families that share a group.

The rule that keeps this safe: groups win whenever they answer. The template
may only rescue a host the groups left as non-serving — it can never override
a real product group.
"""

import asyncio

import pytest

from tests.wiretest import RecordingClient
from zbbx_mcp import classify as classify_mod
from zbbx_mcp.classify import template_product_group
from zbbx_mcp.fetch import fetch_enabled_hosts


@pytest.fixture(autouse=True)
def _reset_map(monkeypatch):
    # The map is cached on first use; every test sets its own.
    classify_mod._TEMPLATE_PRODUCT_MAP = None
    yield
    classify_mod._TEMPLATE_PRODUCT_MAP = None


def _cfg(monkeypatch, value, *, infra_map=True):
    """Configure the template map, and (by default) a product map under which
    the group ``mixed`` classifies as infrastructure.

    Both halves are needed for the fallback to do anything: with no product
    map at all, ``classify_host`` returns the first group name as the product,
    so every group "answers" and nothing is ever non-serving.
    """
    monkeypatch.setenv("ZABBIX_TEMPLATE_PRODUCT_MAP", value)
    classify_mod._TEMPLATE_PRODUCT_MAP = None
    if infra_map:
        monkeypatch.setattr(
            classify_mod, "get_product_map",
            lambda: {"mixed": ("Infrastructure", "Mixed"),
                     "real-product-group": ("SomeProduct", "Tier")})


class TestMapParsing:
    def test_unset_disables_the_feature(self):
        assert classify_mod.get_template_product_map() == {}

    def test_json_form(self, monkeypatch):
        _cfg(monkeypatch, '{"TplA": "GroupA"}')
        assert classify_mod.get_template_product_map() == {"TplA": "GroupA"}

    def test_pair_form(self, monkeypatch):
        _cfg(monkeypatch, "TplA:GroupA,TplB:GroupB")
        assert classify_mod.get_template_product_map() == {
            "TplA": "GroupA", "TplB": "GroupB"}

    def test_malformed_json_disables_rather_than_raises(self, monkeypatch):
        # A broken map must not take the server down, and must not silently
        # half-apply either — it disables the whole fallback.
        _cfg(monkeypatch, '{"TplA": ')
        assert classify_mod.get_template_product_map() == {}


class TestResolution:
    def test_non_serving_group_plus_known_template_reclassifies(self, monkeypatch):
        _cfg(monkeypatch, '{"TplA": "GroupA"}')
        assert template_product_group(
            [{"name": "mixed-infra-group"}], [{"name": "TplA"}]) == "GroupA"

    def test_a_real_product_group_always_wins(self, monkeypatch):
        # The load-bearing rule. Groups are the explicit statement; the
        # template is only consulted when they decline to make one.
        _cfg(monkeypatch, '{"TplA": "GroupA"}')
        assert template_product_group(
            [{"name": "real-product-group"}], [{"name": "TplA"}]) is None

    def test_without_a_product_map_nothing_is_non_serving(self, monkeypatch):
        # Then classify_host answers with the group's own name for every
        # group, so no host is ever eligible. Declining is correct: with no
        # product map there is no notion of "infrastructure" to rescue from.
        _cfg(monkeypatch, '{"TplA": "GroupA"}', infra_map=False)
        assert template_product_group(
            [{"name": "mixed"}], [{"name": "TplA"}]) is None

    def test_unknown_template_changes_nothing(self, monkeypatch):
        _cfg(monkeypatch, '{"TplA": "GroupA"}')
        assert template_product_group(
            [{"name": "mixed"}], [{"name": "SomeOtherTpl"}]) is None

    def test_no_templates_changes_nothing(self, monkeypatch):
        _cfg(monkeypatch, '{"TplA": "GroupA"}')
        assert template_product_group([{"name": "mixed"}], []) is None

    def test_disabled_map_never_reclassifies(self):
        assert template_product_group(
            [{"name": "mixed"}], [{"name": "TplA"}]) is None


class TestFetchInjection:
    def _hosts(self):
        return [
            {"hostid": "1", "host": "node-a",
             "groups": [{"name": "mixed"}], "parentTemplates": [{"name": "TplA"}]},
            {"hostid": "2", "host": "node-b",
             "groups": [{"name": "mixed"}], "parentTemplates": [{"name": "Other"}]},
        ]

    def test_templates_are_not_fetched_when_unconfigured(self):
        # An unconfigured deployment must pay nothing — no extra select.
        c = RecordingClient({"host.get": self._hosts()})
        asyncio.run(fetch_enabled_hosts(c, groups=True, extra_output=["x"]))
        params = [p for m, p in c.calls if m == "host.get"][0]
        assert "selectParentTemplates" not in params

    def test_templates_are_fetched_when_configured(self, monkeypatch):
        _cfg(monkeypatch, '{"TplA": "GroupA"}')
        c = RecordingClient({"host.get": self._hosts()})
        asyncio.run(fetch_enabled_hosts(c, groups=True, extra_output=["x"]))
        params = [p for m, p in c.calls if m == "host.get"][0]
        assert params["selectParentTemplates"] == ["name"]

    def test_matching_host_gets_the_group_prepended(self, monkeypatch):
        _cfg(monkeypatch, '{"TplA": "GroupA"}')
        c = RecordingClient({"host.get": self._hosts()})
        out = asyncio.run(fetch_enabled_hosts(c, groups=True, extra_output=["x"]))
        by = {h["host"]: h for h in out}
        assert by["node-a"]["groups"][0]["name"] == "GroupA"
        # original group is kept, not replaced — the evidence survives
        assert {g["name"] for g in by["node-a"]["groups"]} == {"GroupA", "mixed"}
        assert [g["name"] for g in by["node-b"]["groups"]] == ["mixed"]

    def test_no_templates_requested_means_no_injection(self, monkeypatch):
        # groups=False: nothing to compare against, so the fallback is inert
        # rather than guessing from the template alone.
        _cfg(monkeypatch, '{"TplA": "GroupA"}')
        c = RecordingClient({"host.get": self._hosts()})
        asyncio.run(fetch_enabled_hosts(c, groups=False, extra_output=["x"]))
        params = [p for m, p in c.calls if m == "host.get"][0]
        assert "selectParentTemplates" not in params
