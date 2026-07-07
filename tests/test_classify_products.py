"""Host/product classification tests (split from test_analytics, ADR 074)."""

from zbbx_mcp.classify import (
    classify_host,
)


class TestClassifyHost:
    def test_unknown_groups(self):
        prod, tier = classify_host([{"name": "Templates"}])
        assert prod == "Unknown"

    def test_skip_templates(self):
        prod, tier = classify_host([{"name": "Templates/Applications"}, {"name": "mygroup"}])
        assert prod == "mygroup"

    def test_empty_groups(self):
        prod, tier = classify_host([])
        assert prod == "Unknown"

class TestProductHiding:
    """Test ZABBIX_HIDE_PRODUCTS filtering used in CEO report."""

    def test_is_hidden_with_env(self, monkeypatch):
        """Hidden product detected when env var is set."""
        monkeypatch.setenv("ZABBIX_HIDE_PRODUCTS", "Legacy,OldProduct")
        # Reset cache so it re-reads env
        import zbbx_mcp.data as _data
        assert _data.is_hidden_product("Legacy") is True
        assert _data.is_hidden_product("legacy") is True  # case insensitive
        assert _data.is_hidden_product("OldProduct") is True
        assert _data.is_hidden_product("ActiveProduct") is False
        assert _data.is_hidden_product("AnotherProduct") is False

    def test_is_hidden_empty_env(self, monkeypatch):
        """Nothing hidden when env var is empty."""
        monkeypatch.setenv("ZABBIX_HIDE_PRODUCTS", "")
        import zbbx_mcp.data as _data
        assert _data.is_hidden_product("Legacy") is False
        assert _data.is_hidden_product("anything") is False

    def test_is_hidden_unset_env(self, monkeypatch):
        """Nothing hidden when env var is not set."""
        monkeypatch.delenv("ZABBIX_HIDE_PRODUCTS", raising=False)
        import zbbx_mcp.data as _data
        assert _data.is_hidden_product("Legacy") is False

    def test_group_by_country_excludes_hidden(self, monkeypatch):
        """group_by_country should skip hosts with hidden products."""
        monkeypatch.setenv("ZABBIX_HIDE_PRODUCTS", "Legacy")
        monkeypatch.setenv("ZABBIX_PRODUCT_MAP", "")  # use raw group names
        import zbbx_mcp.data as _data

        hosts = [
            {"hostid": "1", "host": "srv-de01", "groups": [{"name": "good_product"}]},
            {"hostid": "2", "host": "srv-de02", "groups": [{"name": "Legacy"}]},
            {"hostid": "3", "host": "srv-nl01", "groups": [{"name": "good_product"}]},
        ]
        result = _data.group_by_country(hosts)
        # DE should have 1 host (not 2 — Legacy host excluded)
        assert len(result.get("DE", [])) == 1
        assert result["DE"][0]["hostid"] == "1"
        assert "NL" in result

    def test_fleet_composition_filter(self, monkeypatch):
        """Simulate CEO report fleet composition: hidden + non-service excluded."""
        monkeypatch.setenv("ZABBIX_HIDE_PRODUCTS", "Legacy")
        import zbbx_mcp.data as _data

        _NON_service = {"Monitoring", "Infrastructure", "Unknown"}

        hosts_data = [
            ("serviceProduct", "Free"),
            ("serviceProduct", "Premium"),
            ("Legacy", "service"),
            ("Legacy", "service"),
            ("Monitoring", "WHOIS"),
            ("Infrastructure", "Tunnel"),
            ("Unknown", "Unknown"),
        ]

        product_counts: dict[str, int] = {}
        for prod, _tier in hosts_data:
            if prod and prod not in _NON_service and not _data.is_hidden_product(prod):
                product_counts[prod] = product_counts.get(prod, 0) + 1

        assert "serviceProduct" in product_counts
        assert product_counts["serviceProduct"] == 2
        assert "Legacy" not in product_counts, "Legacy should be hidden"
        assert "Monitoring" not in product_counts
        assert "Infrastructure" not in product_counts
        assert "Unknown" not in product_counts

    def test_ceo_report_two_step_filter(self, monkeypatch):
        """Simulate the actual CEO report flow: service_hosts then fleet composition.

        This catches the real bug where fleet composition iterated `hosts`
        instead of `service_hosts`, leaking hidden products into the report.
        """
        monkeypatch.setenv("ZABBIX_HIDE_PRODUCTS", "Legacy")
        monkeypatch.setenv("ZABBIX_PRODUCT_MAP", "")
        import zbbx_mcp.data as _data

        _NON_service = {"Monitoring", "Infrastructure", "Unknown"}

        # Simulate all hosts from Zabbix
        all_hosts = [
            {"hostid": "1", "host": "srv-de01", "groups": [{"name": "Activeservice"}]},
            {"hostid": "2", "host": "srv-de02", "groups": [{"name": "Activeservice"}]},
            {"hostid": "3", "host": "srv-nl01", "groups": [{"name": "Activeservice"}]},
            {"hostid": "4", "host": "srv-us01", "groups": [{"name": "Legacy"}]},
            {"hostid": "5", "host": "srv-us02", "groups": [{"name": "Legacy"}]},
            {"hostid": "6", "host": "monitor-1", "groups": [{"name": "Monitoring"}]},
            {"hostid": "7", "host": "infra-1", "groups": [{"name": "Infrastructure"}]},
        ]

        # Step 1: Build service_hosts (same as CEO report line 144-146)
        service_hosts = [
            h for h in all_hosts
            if not _data.is_hidden_product(classify_host(h.get("groups", []))[0])
            and classify_host(h.get("groups", []))[0] not in _NON_service
        ]

        assert len(service_hosts) == 3, f"Expected 3 service hosts, got {len(service_hosts)}"

        # Step 2: Build fleet composition from service_hosts (NOT all_hosts!)
        product_counts: dict[str, int] = {}
        for h in service_hosts:  # Must be service_hosts, not all_hosts
            prod, _ = classify_host(h.get("groups", []))
            if prod:
                product_counts[prod] = product_counts.get(prod, 0) + 1

        assert "Activeservice" in product_counts
        assert product_counts["Activeservice"] == 3
        assert "Legacy" not in product_counts, "Legacy leaked into fleet composition!"
        assert "Monitoring" not in product_counts
        assert "Infrastructure" not in product_counts

        # Step 3: Verify header KPI matches fleet composition
        total_from_header = len(service_hosts)
        total_from_composition = sum(product_counts.values())
        assert total_from_header == total_from_composition, \
            f"Header says {total_from_header} but composition sums to {total_from_composition}"

class TestCostSummaryRedactPartial:
    """Pure-helper tests for the redact_partial flag on get_cost_summary (#150)."""

    def _fixture(self):
        # Two products: ProdA fully priced, ProdB partial (4 of 5 priced).
        # Two providers: ProvX fully priced, ProvY partial (3 of 4 priced).
        prod_costs = {
            "ProdA / t1": {"count": 3, "total": 300.0},
            "ProdB / t1": {"count": 4, "total": 800.0},
        }
        prov_costs = {
            "ProvX": {"count": 3, "total": 300.0},
            "ProvY": {"count": 3, "total": 600.0},
        }
        prod_totals = {"ProdA / t1": 3, "ProdB / t1": 5}
        prov_totals = {"ProvX": 3, "ProvY": 4}
        return dict(
            prod_costs=prod_costs, prov_costs=prov_costs,
            prod_totals=prod_totals, prov_totals=prov_totals,
            costed=7, total_hosts=8,
        )

    def test_default_preserves_full_output(self):
        from zbbx_mcp.tools.costs_summary import _render_cost_summary
        out = _render_cost_summary(**self._fixture(), redact_partial=False)
        # Grand total includes both products
        assert "$1,100.00/month" in out
        # The "Servers with cost" line is present
        assert "Servers with cost: 7 | Without: 1" in out
        # Both product rows present
        assert "ProdA / t1" in out
        assert "ProdB / t1" in out
        # Both provider rows present
        assert "ProvX" in out
        assert "ProvY" in out
        # No footer
        assert "Filtered to fully-attributed lines" not in out

    def test_redact_drops_partial_product_row(self):
        from zbbx_mcp.tools.costs_summary import _render_cost_summary
        out = _render_cost_summary(**self._fixture(), redact_partial=True)
        # Partial product gone, fully-priced kept
        assert "ProdA / t1" in out
        assert "ProdB / t1" not in out

    def test_redact_drops_partial_provider_row(self):
        from zbbx_mcp.tools.costs_summary import _render_cost_summary
        out = _render_cost_summary(**self._fixture(), redact_partial=True)
        assert "ProvX" in out
        assert "ProvY" not in out

    def test_redact_recomputes_grand_total_from_kept_rows(self):
        from zbbx_mcp.tools.costs_summary import _render_cost_summary
        out = _render_cost_summary(**self._fixture(), redact_partial=True)
        # Only ProdA (the kept row) contributes: $300/mo, $3,600/yr
        assert "$300.00/month" in out
        assert "$3,600.00/year" in out
        # The stale $1,100 from default mode must not appear
        assert "$1,100.00" not in out

    def test_redact_suppresses_without_line(self):
        from zbbx_mcp.tools.costs_summary import _render_cost_summary
        out = _render_cost_summary(**self._fixture(), redact_partial=True)
        assert "Servers with cost" not in out
        assert "Without:" not in out

    def test_redact_appends_footer(self):
        from zbbx_mcp.tools.costs_summary import _render_cost_summary
        out = _render_cost_summary(**self._fixture(), redact_partial=True)
        assert "Filtered to fully-attributed lines" in out

    def test_redact_with_no_priced_data_yields_zero_total(self):
        from zbbx_mcp.tools.costs_summary import _render_cost_summary
        # All groups partial — nothing survives.
        out = _render_cost_summary(
            prod_costs={"P / t": {"count": 1, "total": 50.0}},
            prov_costs={"Q": {"count": 1, "total": 50.0}},
            prod_totals={"P / t": 2},  # 2 total, 1 priced → partial
            prov_totals={"Q": 2},
            costed=1, total_hosts=2,
            redact_partial=True,
        )
        assert "$0.00/month" in out
        # The footer still goes on so the caller knows redaction was active.
        assert "Filtered to fully-attributed lines" in out

    def test_unknown_key_in_totals_defaults_to_kept(self):
        from zbbx_mcp.tools.costs_summary import _render_cost_summary
        # A product appears in costs but not in totals (defensive: classify
        # drift between the two passes). Default behaviour: keep the row
        # rather than silently drop it.
        out = _render_cost_summary(
            prod_costs={"Orphan / t": {"count": 1, "total": 100.0}},
            prov_costs={"P": {"count": 1, "total": 100.0}},
            prod_totals={},
            prov_totals={"P": 1},
            costed=1, total_hosts=1,
            redact_partial=True,
        )
        assert "Orphan / t" in out

class TestUnmappedGroupCounts:
    """Pure-helper tests for unmapped_group_counts (ADR 058)."""

    def test_counts_sorted_desc_then_name(self):
        from zbbx_mcp.classify import unmapped_group_counts
        sets = [["GrpA"], ["GrpA", "GrpB"], ["GrpB"], ["GrpB"]]
        out = unmapped_group_counts(sets, {})
        assert out == [("GrpB", 3), ("GrpA", 2)]

    def test_mapped_and_skip_mapped_excluded(self):
        from zbbx_mcp.classify import unmapped_group_counts
        pmap = {"Mapped": ("Prod", "Tier"), "Skipped": (None, None)}
        sets = [["Mapped", "Gap"], ["Skipped", "Gap"]]
        assert unmapped_group_counts(sets, pmap) == [("Gap", 2)]

    def test_groupless_host_counted(self):
        from zbbx_mcp.classify import unmapped_group_counts
        assert unmapped_group_counts([[]], {}) == [("(no groups)", 1)]

    def test_empty_input(self):
        from zbbx_mcp.classify import unmapped_group_counts
        assert unmapped_group_counts([], {"X": ("P", "T")}) == []
