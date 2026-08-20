"""Investigation-ready host detail + the inventory-gap hint (ADR 099).

`get_host` returned identity only, so every investigation paid three
follow-up calls for the context it always needs. And a country filter that
matched nothing asserted absence, when the truth may be that the hosts exist
but their country cannot be derived.
"""

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp.country import country_inventory_gap, name_suggests_country
from zbbx_mcp.formatters import format_host_detail
from zbbx_mcp.tools import hosts as hosts_mod

BASE = {
    "hostid": "1", "host": "node-ab1", "name": "node-ab1", "status": "0",
    "groups": [{"name": "edge"}],
    "interfaces": [{"type": "1", "ip": "10.0.0.1", "port": "10050"}],
}


class TestFormatHostDetailContext:
    def test_without_context_is_unchanged(self):
        out = format_host_detail(BASE)
        assert "# Host: node-ab1" in out
        assert "Country:" not in out and "Traffic in:" not in out

    def test_identity_and_state_lines_render(self):
        ctx = {
            "country": "FJ", "product": "Widgets", "tier": "Basic",
            "provider": "SomeCloud", "traffic_mbps": 12.34,
            "service_status": "OK", "cost_month": 42.0, "bw_limit": 500.0,
            "templates": ["Tmpl A", "Tmpl B"],
        }
        out = format_host_detail(BASE, ctx)
        assert "**Country:** FJ" in out
        assert "**Product:** Widgets / Basic" in out
        assert "**Provider:** SomeCloud" in out
        assert "**Traffic in:** 12.3 Mbps" in out
        assert "**Service check:** OK" in out
        assert "**Cost/month:** 42" in out
        assert "**BW limit:** 500 Mbps" in out
        assert "Tmpl A" in out and "Tmpl B" in out

    def test_absent_keys_are_simply_omitted(self):
        out = format_host_detail(BASE, {"country": "FJ"})
        assert "**Country:** FJ" in out
        assert "Provider:" not in out
        assert "Traffic in:" not in out

    def test_zero_traffic_still_renders(self):
        # 0.0 is a fact (host is up, moving nothing) — not "unknown".
        out = format_host_detail(BASE, {"traffic_mbps": 0.0})
        assert "**Traffic in:** 0.0 Mbps" in out


class TestGetHostEnrichment:
    def _client(self):
        return RecordingClient({
            "host.get": [dict(BASE, inventory={"site_country": "FJ"},
                              parentTemplates=[{"name": "Tmpl A"}])],
            "usermacro.get": [
                {"macro": "{$COST_MONTH}", "value": "42"},
                {"macro": "{$BW_LIMIT}", "value": "500"},
            ],
            "item.get": [
                {"hostid": "1", "key_": "net.if.in[eth0]", "lastvalue": "9000000"},
            ],
        })

    def test_enriched_by_default(self):
        out = run_tool(hosts_mod, "get_host", self._client(), host_id="1")
        assert "**Country:** FJ" in out
        assert "**Cost/month:** 42" in out
        assert "Tmpl A" in out

    def test_wire_requests_inventory_and_templates(self):
        client = self._client()
        run_tool(hosts_mod, "get_host", client, host_id="1")
        sent = client.sent("host.get")
        assert "selectInventory" in sent
        assert "selectParentTemplates" in sent

    def test_brief_skips_the_extra_calls(self):
        client = self._client()
        out = run_tool(hosts_mod, "get_host", client, host_id="1", brief=True)
        assert "# Host: node-ab1" in out
        assert "Cost/month" not in out
        assert not any(m == "usermacro.get" for m, _ in client.calls)

    def test_enrichment_failure_does_not_break_the_lookup(self):
        # A macro/traffic error must never turn a working identity lookup
        # into an error string.
        class Flaky(RecordingClient):
            async def call(self, method, params):
                if method in ("usermacro.get", "item.get"):
                    raise ValueError("boom")
                return await super().call(method, params)

        client = Flaky({"host.get": [BASE]})
        out = run_tool(hosts_mod, "get_host", client, host_id="1")
        assert "# Host: node-ab1" in out
        # Must not become the tool's error return...
        assert not out.startswith("Error")
        # ...but the failure IS disclosed rather than swallowed, so a reader
        # can tell "could not read" from "not set" (ADR 103).
        assert "## Not shown" in out
        assert "usermacro.get" in out
        assert "Cost/month" not in out

    def test_missing_macro_and_unreadable_macro_look_different(self):
        """The distinction the silent version destroyed.

        A host with no cost macro and a host whose macros cannot be read both
        render without a Cost line — so the output must say which happened,
        or a permissions wall is indistinguishable from a real absence.
        """
        no_macro = RecordingClient({"host.get": [BASE], "usermacro.get": []})
        out_absent = run_tool(hosts_mod, "get_host", no_macro, host_id="1")

        class Denied(RecordingClient):
            async def call(self, method, params):
                if method == "usermacro.get":
                    raise ValueError("Access denied")
                return await super().call(method, params)

        out_denied = run_tool(hosts_mod, "get_host",
                              Denied({"host.get": [BASE]}), host_id="1")

        assert "Cost/month" not in out_absent and "Cost/month" not in out_denied
        assert "## Not shown" not in out_absent      # genuinely not set
        assert "## Not shown" in out_denied          # could not be read
        assert out_absent != out_denied


class TestCountryInventoryGap:
    def test_name_pattern_matching(self):
        assert name_suggests_country("node-xy-fj1", "FJ")
        assert name_suggests_country("node-fj", "fj")
        assert name_suggests_country("node_fj0", "FJ")

    def test_embedded_letters_do_not_match(self):
        # The code must be separator-delimited, not buried in a word.
        assert not name_suggests_country("nodefjord1", "FJ")
        assert not name_suggests_country("affjord", "FJ")

    def test_gap_lists_hosts_that_look_right_but_do_not_resolve(self):
        hosts = [
            # Dot separator: the strict parser only accepts - and _, so the
            # country is unreadable even though a human sees it.
            {"host": "node.fj1"},
            # Trailing code with no index — also unreadable strictly.
            {"host": "node-fj"},
            # Resolves correctly -> not a gap.
            {"host": "node-fj1"},
        ]
        gap = country_inventory_gap(hosts, "FJ")
        assert gap == ["node-fj", "node.fj1"]

    def test_no_gap_returns_empty(self):
        assert country_inventory_gap([{"host": "node-ki1"}], "FJ") == []

    def test_note_is_appended_only_when_relevant(self):
        note = hosts_mod._inventory_gap_note([{"host": "node.fj1"}], "FJ")
        assert "look like FJ by name" in note
        assert "inventory gap" in note
        assert hosts_mod._inventory_gap_note([{"host": "node-ki1"}], "FJ") == ""
        assert hosts_mod._inventory_gap_note([{"host": "node.fj1"}], "") == ""


class TestCostFallbackItem:
    """ADR 106 — the cost survives a role that revokes `usermacro.get`.

    The monthly figure is published twice: as `{$COST_MONTH}` and as the
    `Cost_macros_present` item. Reading the item needs only host-group read,
    so a token that cannot call `usermacro.get` can still answer "what does
    this host cost" — the question that surfaced the whole ADR 103 thread.
    """

    def _items(self, cost=None):
        def handler(params):
            if (params.get("filter") or {}).get("key_") == "Cost_macros_present":
                return ([{"itemid": "7", "key_": "Cost_macros_present",
                          "lastvalue": cost}] if cost is not None else [])
            return []
        return handler

    def test_denied_macro_falls_back_to_the_item(self):
        class Denied(RecordingClient):
            async def call(self, method, params):
                if method == "usermacro.get":
                    raise ValueError("Access denied")
                return await super().call(method, params)
        c = Denied({"host.get": [BASE], "item.get": self._items("16")})
        out = run_tool(hosts_mod, "get_host", c, host_id="1")
        assert "**Cost/month:** 16" in out
        assert "via item" in out          # never passed off as the macro
        assert "## Not shown" in out      # the macro failure is still disclosed

    def test_absent_macro_also_falls_back(self):
        c = RecordingClient({"host.get": [BASE], "usermacro.get": [],
                             "item.get": self._items("32.49")})
        out = run_tool(hosts_mod, "get_host", c, host_id="1")
        assert "**Cost/month:** 32.49" in out
        assert "via item" in out

    def test_readable_macro_wins_and_costs_no_extra_call(self):
        # The fallback must not run on the common path, and a value that came
        # from the macro must NOT carry the "via item" caveat.
        c = RecordingClient({
            "host.get": [BASE],
            "usermacro.get": [{"macro": "{$COST_MONTH}", "value": "16"}],
            "item.get": self._items("999"),
        })
        out = run_tool(hosts_mod, "get_host", c, host_id="1")
        assert "**Cost/month:** 16" in out
        assert "via item" not in out
        assert not any(
            (p.get("filter") or {}).get("key_") == "Cost_macros_present"
            for m, p in c.calls if m == "item.get"
        )

    def test_no_cost_anywhere_stays_silent(self):
        c = RecordingClient({"host.get": [BASE], "usermacro.get": [],
                             "item.get": self._items(None)})
        out = run_tool(hosts_mod, "get_host", c, host_id="1")
        assert "Cost/month" not in out

    def test_unparseable_item_value_is_not_guessed(self):
        c = RecordingClient({"host.get": [BASE], "usermacro.get": [],
                             "item.get": self._items("n/a")})
        out = run_tool(hosts_mod, "get_host", c, host_id="1")
        assert "Cost/month" not in out
