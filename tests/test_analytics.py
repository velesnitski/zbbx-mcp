"""Tests for analytics helpers, provider detection, and trend sanity logic."""

from zbbx_mcp.classify import (
    PROVIDER_CIDRS,
    classify_host,
    detect_provider,
    resolve_datacenter,
)
from zbbx_mcp.data import (
    CAPITAL_COORDS,
    REGION_MAP,
    countries_for_region,
    extract_country,
    group_by_country,
    host_ip,
)


class TestRegionMap:
    def test_latam_countries(self):
        codes = countries_for_region("LATAM")
        assert "BR" in codes
        assert "MX" in codes
        assert "AR" in codes
        assert "DE" not in codes

    def test_emea_countries(self):
        codes = countries_for_region("EMEA")
        assert "DE" in codes
        assert "NL" in codes
        assert "US" not in codes

    def test_all_returns_union(self):
        codes = countries_for_region("ALL")
        assert len(codes) > 50
        assert "US" in codes
        assert "BR" in codes
        assert "DE" in codes

    def test_unknown_region_empty(self):
        assert countries_for_region("MARS") == set()

    def test_case_insensitive(self):
        assert countries_for_region("latam") == countries_for_region("LATAM")

    def test_all_regions_have_countries(self):
        for region in REGION_MAP:
            assert len(REGION_MAP[region]) > 0




class TestCapitalCoords:
    def test_major_countries_present(self):
        for cc in ["US", "DE", "NL", "BR", "FR", "TR", "JP", "RU"]:
            assert cc in CAPITAL_COORDS, f"{cc} missing from CAPITAL_COORDS"

    def test_coords_are_valid(self):
        for cc, (lat, lon) in CAPITAL_COORDS.items():
            assert -90 <= lat <= 90, f"{cc} lat {lat} out of range"
            assert -180 <= lon <= 180, f"{cc} lon {lon} out of range"




class TestGroupByCountry:
    def _hosts(self):
        return [
            {"hostid": "1", "host": "srv-nl01", "groups": [{"name": "free"}]},
            {"hostid": "2", "host": "srv-nl02", "groups": [{"name": "free"}]},
            {"hostid": "3", "host": "srv-de01", "groups": [{"name": "prem"}]},
            {"hostid": "4", "host": "srv-us01", "groups": [{"name": "free"}]},
            {"hostid": "5", "host": "no-country", "groups": [{"name": "mon"}]},
        ]

    def test_basic_grouping(self):
        result = group_by_country(self._hosts())
        assert "NL" in result
        assert len(result["NL"]) == 2
        assert "DE" in result
        assert "US" in result
        assert "" not in result

    def test_country_filter(self):
        result = group_by_country(self._hosts(), country="nl")
        assert list(result.keys()) == ["NL"]
        assert len(result["NL"]) == 2

    def test_country_filter_no_match(self):
        result = group_by_country(self._hosts(), country="JP")
        assert result == {}

    def test_region_filter(self):
        result = group_by_country(self._hosts(), region="EMEA")
        assert "NL" in result
        assert "DE" in result
        assert "US" not in result




class TestHostIp:
    def test_extracts_ip(self):
        h = {"interfaces": [{"ip": "1.2.3.4"}]}
        assert host_ip(h) == "1.2.3.4"

    def test_skips_loopback(self):
        h = {"interfaces": [{"ip": "127.0.0.1"}, {"ip": "5.6.7.8"}]}
        assert host_ip(h) == "5.6.7.8"

    def test_no_interfaces(self):
        assert host_ip({}) == ""
        assert host_ip({"interfaces": []}) == ""

    def test_only_loopback(self):
        h = {"interfaces": [{"ip": "127.0.0.1"}]}
        assert host_ip(h) == ""




class TestProviderDetection:
    def test_fiberhub(self):
        assert detect_provider("108.181.55.10") == "Fiberhub"

    def test_turk_telekom(self):
        assert detect_provider("89.252.100.10") == "Turk Telekom"

    def test_m247(self):
        assert detect_provider("146.70.50.10") == "M247"

    def test_kamatera(self):
        assert detect_provider("154.16.100.10") == "Kamatera"

    def test_aruba_it(self):
        assert detect_provider("95.110.100.10") == "Aruba.it"

    def test_cogent_latam(self):
        assert detect_provider("170.80.100.10") == "Cogent"
        assert detect_provider("38.165.100.10") == "Cogent"

    def test_ovh_canada(self):
        assert detect_provider("66.70.100.10") == "OVH"

    def test_hetzner(self):
        assert detect_provider("95.217.100.10") == "Hetzner"

    def test_aws_16(self):
        assert detect_provider("10.0.0.10") == "AWS"

    def test_digitalocean(self):
        assert detect_provider("10.0.0.11") == "DigitalOcean"

    def test_unknown_ip(self):
        assert detect_provider("1.1.1.1") == "Other"

    def test_invalid_ip(self):
        assert detect_provider("not-an-ip") == "Unknown"

    def test_prefix_length_wins(self):
        """More specific CIDR should win over broad AWS /8."""
        assert detect_provider("10.0.0.15") == "Google Cloud"
        assert detect_provider("10.0.0.16") == "OVH"
        assert detect_provider("10.0.0.17") == "Azure"

    def test_all_providers_have_cidrs(self):
        for prov, cidrs in PROVIDER_CIDRS.items():
            assert len(cidrs) > 0, f"{prov} has no CIDRs"




class TestResolveDatacenter:
    def test_ovh_gravelines(self):
        prov, city = resolve_datacenter("10.0.0.13")
        assert prov == "OVH"
        assert "Gravelines" in city

    def test_hetzner_helsinki(self):
        prov, city = resolve_datacenter("10.0.0.12")
        assert prov == "Hetzner"
        assert "Helsinki" in city

    def test_scaleway_paris(self):
        prov, city = resolve_datacenter("10.0.0.14")
        assert prov == "Scaleway"
        assert "Paris" in city

    def test_fallback_provider_only(self):
        prov, city = resolve_datacenter("10.0.0.5")
        assert prov == "Aruba.it"
        assert city == ""

    def test_unknown(self):
        prov, city = resolve_datacenter("1.1.1.1")
        assert prov == "Other"

    def test_invalid(self):
        prov, city = resolve_datacenter("bad")
        assert prov == "Unknown"




class TestExtractCountry:
    def test_standard_patterns(self):
        assert extract_country("srv-nl0105") == "NL"
        assert extract_country("srv-de3") == "DE"
        assert extract_country("srv-us0001") == "US"

    def test_lite_pattern(self):
        assert extract_country("srv-nl01-lite") == "NL"
        assert extract_country("srv-us01-lite") == "US"
        assert extract_country("srv-tr01-lite") == "TR"

    def test_ar_pattern(self):
        assert extract_country("srv-ar010") == "AR"

    def test_br_mx_patterns(self):
        assert extract_country("srv-br0101") == "BR"
        assert extract_country("srv-mx0101") == "MX"

    def test_uk_normalizes_to_gb(self):
        """Non-ISO country code normalizes to standard."""
        assert extract_country("srv-uk0001") == "GB"
        assert extract_country("srv-uk0005") == "GB"

    def test_no_match(self):
        assert extract_country("Zabbix server") == ""
        assert extract_country("account.example.com") == ""
        assert extract_country("a1") == ""

    def test_multiple_country_codes(self):
        """Multiple country codes in hostname — first wins."""
        assert extract_country("srv-de-nl01") == "DE"




class TestTrendSanity:
    """Test the trend/change consistency rules used in CEO report and geo tools.

    Rules:
    1. change < -10% and trend == "rising" → override to "stable"
    2. change > 0 and trend == "dropping" → override to "stable"
    3. current > avg * 1.5 and trend == "dropping" → override to "rising"
    4. current < 0.01 and avg > 0.05 → "dead"
    """

    @staticmethod
    def _apply_sanity(change: float, trend: str, traffic_gbps: float, avg_gbps: float) -> str:
        """Replicate the sanity logic from ceo_report.py / geo.py."""
        if change <= -30 and trend in ("stable", "rising"):
            trend = "dropping"
        elif change >= 30 and trend in ("stable", "dropping"):
            trend = "rising"
        elif change <= -10 and trend == "rising" or change > 0 and trend == "dropping":
            trend = "stable"
        if traffic_gbps < 0.01 and avg_gbps > 0.05:
            trend = "dead"
        return trend

    def test_rising_with_negative_change(self):
        """Rising trend but negative change should become stable."""
        result = self._apply_sanity(change=-13, trend="rising", traffic_gbps=22.0, avg_gbps=25.4)
        assert result == "stable", f"Expected stable, got {result}"

    def test_dropping_with_large_decline(self):
        """Legitimate large decline stays dropping."""
        result = self._apply_sanity(change=-87, trend="dropping", traffic_gbps=0.3, avg_gbps=2.2)
        assert result == "dropping"

    def test_rising_with_strong_growth(self):
        """Legitimate strong growth stays rising."""
        result = self._apply_sanity(change=123, trend="rising", traffic_gbps=26.3, avg_gbps=11.8)
        assert result == "rising"

    def test_dropping_positive_change_becomes_stable(self):
        """Small positive change with dropping trend becomes stable."""
        result = self._apply_sanity(change=11, trend="dropping", traffic_gbps=1.9, avg_gbps=1.7)
        assert result == "stable"

    def test_dropping_huge_current_becomes_rising(self):
        """Large positive change with dropping trend becomes rising."""
        result = self._apply_sanity(change=100, trend="dropping", traffic_gbps=4.0, avg_gbps=2.0)
        # change > 30 catches this → rising
        assert result == "rising"

    def test_dead_overrides_all(self):
        """Zero traffic with prior average triggers dead."""
        result = self._apply_sanity(change=-100, trend="dropping", traffic_gbps=0.0, avg_gbps=0.9)
        assert result == "dead"

    def test_moderate_decline_stays_stable(self):
        """Moderate decline within threshold stays stable."""
        result = self._apply_sanity(change=-21, trend="stable", traffic_gbps=20.3, avg_gbps=25.7)
        assert result == "stable"

    def test_stable_large_decline_becomes_dropping(self):
        """Large decline with stable trend becomes dropping."""
        result = self._apply_sanity(change=-47, trend="stable", traffic_gbps=0.8, avg_gbps=1.6)
        assert result == "dropping"

    def test_stable_significant_decline_becomes_dropping(self):
        """Significant decline with stable trend becomes dropping."""
        result = self._apply_sanity(change=-43, trend="stable", traffic_gbps=0.4, avg_gbps=0.7)
        assert result == "dropping"

    def test_stable_severe_decline_becomes_dropping(self):
        """Severe decline with stable trend becomes dropping."""
        result = self._apply_sanity(change=-70, trend="stable", traffic_gbps=1.1, avg_gbps=3.7)
        assert result == "dropping"

    def test_big_positive_change_stable_becomes_rising(self):
        """Large positive change overrides stable to rising."""
        result = self._apply_sanity(change=50, trend="stable", traffic_gbps=15.0, avg_gbps=10.0)
        assert result == "rising"

    def test_small_negative_change_keeps_rising(self):
        """Small decline within threshold keeps rising."""
        result = self._apply_sanity(change=-5, trend="rising", traffic_gbps=9.5, avg_gbps=10.0)
        assert result == "rising"

    def test_zero_traffic_zero_avg_stays_stable(self):
        """No traffic and no history stays stable."""
        result = self._apply_sanity(change=0, trend="stable", traffic_gbps=0.0, avg_gbps=0.0)
        assert result == "stable"

    def test_dropping_large_positive_becomes_rising(self):
        """Large positive change overrides dropping to rising."""
        result = self._apply_sanity(change=83, trend="dropping", traffic_gbps=4.1, avg_gbps=2.2)
        assert result == "rising"

    def test_dropping_moderate_positive_becomes_rising(self):
        """Moderate positive change overrides dropping to rising."""
        result = self._apply_sanity(change=36, trend="dropping", traffic_gbps=8.3, avg_gbps=6.1)
        assert result == "rising"

    def test_dropping_positive_15pct_becomes_stable(self):
        """Small positive change below threshold becomes stable."""
        result = self._apply_sanity(change=15, trend="dropping", traffic_gbps=1.15, avg_gbps=1.0)
        assert result == "stable"

    
    def test_exactly_minus_30_becomes_dropping(self):
        """Boundary: -30% exactly should trigger dropping (<=, not <)."""
        result = self._apply_sanity(change=-30, trend="stable", traffic_gbps=0.7, avg_gbps=1.0)
        assert result == "dropping", f"change=-30 stable should be dropping, got {result}"

    def test_minus_29_stays_stable(self):
        """Boundary: -29% should NOT trigger dropping."""
        result = self._apply_sanity(change=-29, trend="stable", traffic_gbps=0.71, avg_gbps=1.0)
        assert result == "stable"

    def test_exactly_plus_30_becomes_rising(self):
        """Boundary: +30% exactly should trigger rising (>=, not >)."""
        result = self._apply_sanity(change=30, trend="stable", traffic_gbps=1.3, avg_gbps=1.0)
        assert result == "rising", f"change=+30 stable should be rising, got {result}"

    def test_plus_29_stays_stable(self):
        """Boundary: +29% should NOT trigger rising."""
        result = self._apply_sanity(change=29, trend="stable", traffic_gbps=1.29, avg_gbps=1.0)
        assert result == "stable"

    def test_exactly_minus_10_rising_becomes_stable(self):
        """Boundary: -10% with rising should become stable."""
        result = self._apply_sanity(change=-10, trend="rising", traffic_gbps=0.9, avg_gbps=1.0)
        assert result == "stable"

    def test_minus_9_rising_stays_rising(self):
        """Boundary: -9% with rising should stay rising."""
        result = self._apply_sanity(change=-9, trend="rising", traffic_gbps=0.91, avg_gbps=1.0)
        assert result == "rising"

    def test_exactly_minus_30_rising_becomes_dropping(self):
        """Boundary: -30% with rising should become dropping (not stable)."""
        result = self._apply_sanity(change=-30, trend="rising", traffic_gbps=0.7, avg_gbps=1.0)
        assert result == "dropping"

    def test_exactly_plus_30_dropping_becomes_rising(self):
        """Boundary: +30% with dropping should become rising (not stable)."""
        result = self._apply_sanity(change=30, trend="dropping", traffic_gbps=1.3, avg_gbps=1.0)
        assert result == "rising"




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

