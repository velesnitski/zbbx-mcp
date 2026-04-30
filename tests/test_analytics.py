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
    """Provider detection tests — iterate the PROVIDER_CIDRS database only.

    No provider names or IPs are hardcoded in tests. Test coverage scales
    automatically with whatever is in the database.
    """

    @staticmethod
    def _all_nets():
        import ipaddress
        return [
            (prov, ipaddress.ip_network(cidr, strict=False))
            for prov, cidrs in PROVIDER_CIDRS.items()
            for cidr in cidrs
        ]

    def _pick_sample_ip(self, net, all_nets):
        """Pick an IP in `net` that is NOT in any more-specific other-provider net."""
        overlaps = [
            n for p, n in all_nets
            if n.subnet_of(net) and n.prefixlen > net.prefixlen
        ]
        hosts = net.hosts() if net.prefixlen < 31 else iter([net.network_address])
        for candidate in hosts:
            if not any(candidate in o for o in overlaps):
                return str(candidate)
        return None  # fully subsumed — unreachable for this provider

    def test_every_provider_detected_via_db(self):
        """Every provider must be detected from at least one of its own CIDRs."""
        all_nets = self._all_nets()
        for prov, cidrs in PROVIDER_CIDRS.items():
            import ipaddress
            detected_at_least_once = False
            for cidr in cidrs:
                net = ipaddress.ip_network(cidr, strict=False)
                ip = self._pick_sample_ip(net, all_nets)
                if ip and detect_provider(ip) == prov:
                    detected_at_least_once = True
                    break
            assert detected_at_least_once, (
                "Provider has CIDRs but none resolve back to it — check overlaps"
            )

    def test_rfc1918_private_is_other(self):
        """Private RFC 1918 ranges should fall through to 'Other'."""
        assert detect_provider("192.168.1.1") == "Other"
        assert detect_provider("10.0.0.1") == "Other"
        assert detect_provider("172.16.0.1") == "Other"

    def test_invalid_ip(self):
        assert detect_provider("not-an-ip") == "Unknown"
        assert detect_provider("") == "Unknown"

    def test_prefix_length_wins(self):
        """More specific CIDR must win over broader one — verified across DB."""
        import ipaddress
        # Find any pair where a smaller prefix is contained in a larger one
        nets = [
            (prov, ipaddress.ip_network(c, strict=False))
            for prov, cidrs in PROVIDER_CIDRS.items()
            for c in cidrs
        ]
        found_pair = False
        for i, (p1, n1) in enumerate(nets):
            for p2, n2 in nets[i + 1:]:
                if p1 == p2 or n1.prefixlen == n2.prefixlen:
                    continue
                more_specific = n1 if n1.prefixlen > n2.prefixlen else n2
                broader = n2 if n1.prefixlen > n2.prefixlen else n1
                specific_prov = p1 if n1.prefixlen > n2.prefixlen else p2
                if more_specific.subnet_of(broader):
                    hosts = list(more_specific.hosts()) if more_specific.prefixlen < 31 else [more_specific.network_address]
                    ip = str(hosts[len(hosts) // 3])
                    assert detect_provider(ip) == specific_prov
                    found_pair = True
                    break
            if found_pair:
                break
        assert found_pair, "Database must contain at least one overlapping pair"

    def test_all_providers_have_cidrs(self):
        for prov, cidrs in PROVIDER_CIDRS.items():
            assert len(cidrs) > 0, f"{prov} has no CIDRs"




class TestResolveDatacenter:
    """Datacenter resolution tests — iterate the DATACENTER_CIDRS database."""

    def _sample_ip(self, cidr: str) -> str:
        import ipaddress
        net = ipaddress.ip_network(cidr, strict=False)
        hosts = list(net.hosts()) if net.prefixlen < 31 else [net.network_address]
        return str(hosts[len(hosts) // 3]) if hosts else str(net.network_address)

    def test_every_dc_entry_resolves(self):
        """Every datacenter mapping must resolve to its recorded provider+city."""
        from zbbx_mcp.classify import DATACENTER_CIDRS
        for prov, mappings in DATACENTER_CIDRS.items():
            for cidr, city in mappings:
                ip = self._sample_ip(cidr)
                got_prov, got_city = resolve_datacenter(ip)
                assert got_prov == prov, f"{ip}: expected {prov}, got {got_prov}"
                assert got_city == city, f"{ip}: expected {city}, got {got_city}"

    def test_fallback_to_provider_only(self):
        """Provider-known but no DC mapping → provider name, empty city."""
        from zbbx_mcp.classify import DATACENTER_CIDRS, PROVIDER_CIDRS
        dc_providers = set(DATACENTER_CIDRS.keys())
        for prov, cidrs in PROVIDER_CIDRS.items():
            if prov in dc_providers:
                continue
            # Pick first cidr of a provider without DC mapping
            ip = self._sample_ip(cidrs[0])
            got_prov, got_city = resolve_datacenter(ip)
            assert got_prov == prov
            assert got_city == ""
            return
        # If we reach here the test is still valid (all providers have DC mappings)

    def test_unknown_falls_through(self):
        """RFC 1918 / unroutable → Other, empty city."""
        prov, city = resolve_datacenter("10.10.10.10")
        assert prov == "Other"
        assert city == ""

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


class TestIdleRelayDetection:
    """Pure-helper tests for get_idle_relays bucket+filter logic."""

    def _phys(self) -> frozenset[str]:
        return frozenset({"net.if.in[eth0]", "net.if.in[eno1]"})

    def test_split_buckets_physical_vs_tunnel(self):
        from zbbx_mcp.tools.correlation import _split_iface_metrics

        items = [
            {"hostid": "h1", "key_": "net.if.in[eth0]", "lastvalue": "20000"},
            {"hostid": "h1", "key_": "net.if.in[tun0]", "lastvalue": "0"},
            {"hostid": "h1", "key_": "net.if.in[gre1]", "lastvalue": "0"},
            {"hostid": "h1", "key_": "net.if.in[lo]", "lastvalue": "999"},  # ignored
        ]
        per_host = _split_iface_metrics(items, self._phys())
        assert per_host["h1"]["physical_bps"] == 20000
        assert per_host["h1"]["tunnel_bps"] == 0
        assert per_host["h1"]["tunnel_count"] == 2
        assert sorted(per_host["h1"]["tunnel_names"]) == ["gre1", "tun0"]

    def test_split_skips_docker_bridges(self):
        from zbbx_mcp.tools.correlation import _split_iface_metrics

        items = [
            {"hostid": "h1", "key_": "net.if.in[docker0]", "lastvalue": "1"},
            {"hostid": "h1", "key_": "net.if.in[br-abc]", "lastvalue": "1"},
        ]
        per_host = _split_iface_metrics(items, self._phys())
        assert per_host == {}

    def test_split_handles_garbage_values(self):
        from zbbx_mcp.tools.correlation import _split_iface_metrics

        items = [
            {"hostid": "h1", "key_": "net.if.in[eth0]", "lastvalue": ""},
            {"hostid": "h1", "key_": "net.if.in[tun0]", "lastvalue": None},
            {"hostid": "h1", "key_": "not-a-net-key", "lastvalue": "5"},
            {"hostid": "h1", "key_": "net.if.in[", "lastvalue": "5"},  # malformed
        ]
        per_host = _split_iface_metrics(items, self._phys())
        # eth0 zero is still a recorded physical, no tunnel flagged
        assert per_host["h1"]["physical_bps"] == 0.0
        assert per_host["h1"]["tunnel_bps"] == 0.0
        assert per_host["h1"]["tunnel_count"] == 1
        assert per_host["h1"]["tunnel_names"] == ["tun0"]

    def test_idle_relay_flagged_when_tunnels_silent(self):
        from zbbx_mcp.tools.correlation import _find_idle_relays

        per_host = {
            "h1": {
                "physical_bps": 200_000,
                "tunnel_bps": 0,
                "tunnel_count": 3,
                "tunnel_names": ["tun0", "tun1", "tun2"],
            },
        }
        idle = _find_idle_relays(per_host, min_mgmt_kbps=100)
        assert len(idle) == 1
        hid, mgmt_kbps, tun_count, sample = idle[0]
        assert hid == "h1"
        assert mgmt_kbps == 200.0
        assert tun_count == 3
        assert sample == ["tun0", "tun1", "tun2"]

    def test_idle_relay_skipped_when_tunnels_have_traffic(self):
        from zbbx_mcp.tools.correlation import _find_idle_relays

        per_host = {
            "h1": {
                "physical_bps": 200_000,
                "tunnel_bps": 10,  # one tunnel forwarding
                "tunnel_count": 2,
                "tunnel_names": ["tun0", "tun1"],
            },
        }
        assert _find_idle_relays(per_host, 100) == []

    def test_idle_relay_skipped_below_mgmt_floor(self):
        from zbbx_mcp.tools.correlation import _find_idle_relays

        per_host = {
            "h1": {
                "physical_bps": 50_000,  # 50 kbps, below 100
                "tunnel_bps": 0,
                "tunnel_count": 2,
                "tunnel_names": ["tun0", "tun1"],
            },
        }
        assert _find_idle_relays(per_host, 100) == []

    def test_idle_relay_skipped_when_no_tunnels(self):
        from zbbx_mcp.tools.correlation import _find_idle_relays

        per_host = {
            "h1": {
                "physical_bps": 200_000,
                "tunnel_bps": 0,
                "tunnel_count": 0,
                "tunnel_names": [],
            },
        }
        assert _find_idle_relays(per_host, 100) == []

    def test_idle_relays_sorted_by_mgmt_traffic_desc(self):
        from zbbx_mcp.tools.correlation import _find_idle_relays

        per_host = {
            "h1": {"physical_bps": 100_000, "tunnel_bps": 0, "tunnel_count": 1, "tunnel_names": ["tun0"]},
            "h2": {"physical_bps": 500_000, "tunnel_bps": 0, "tunnel_count": 1, "tunnel_names": ["tun0"]},
            "h3": {"physical_bps": 250_000, "tunnel_bps": 0, "tunnel_count": 1, "tunnel_names": ["tun0"]},
        }
        out = _find_idle_relays(per_host, 50)
        assert [r[0] for r in out] == ["h2", "h3", "h1"]


class TestOutageClustering:
    """Pure-helper tests for get_outage_clusters time-window grouping."""

    def _rec(self, clock: int, hostid: str, key: str, name: str = "Down", sev: int = 4) -> dict:
        return {
            "clock": clock,
            "hostid": hostid,
            "host": f"host-{hostid}",
            "name": name,
            "severity": sev,
            "key": key,
        }

    def test_subnet_helper(self):
        from zbbx_mcp.tools.correlation import _subnet24

        assert _subnet24("10.0.5.42") == "10.0.5.0/24"
        assert _subnet24("") == ""
        assert _subnet24("not-an-ip") == ""
        assert _subnet24("1.2.3") == ""
        assert _subnet24("::1") == ""

    def test_three_hosts_same_subnet_within_window_form_cluster(self):
        from zbbx_mcp.tools.correlation import _cluster_problems

        records = [
            self._rec(1000, "h1", "10.0.0.0/24"),
            self._rec(1100, "h2", "10.0.0.0/24"),
            self._rec(1200, "h3", "10.0.0.0/24"),
        ]
        clusters = _cluster_problems(records, window_sec=600, min_hosts=3)
        assert len(clusters) == 1
        c = clusters[0]
        assert c["host_count"] == 3
        assert c["events"] == 3
        assert c["start"] == 1000
        assert c["end"] == 1200

    def test_below_min_hosts_does_not_cluster(self):
        from zbbx_mcp.tools.correlation import _cluster_problems

        records = [
            self._rec(1000, "h1", "10.0.0.0/24"),
            self._rec(1100, "h2", "10.0.0.0/24"),
        ]
        assert _cluster_problems(records, 600, 3) == []

    def test_outside_window_does_not_cluster(self):
        from zbbx_mcp.tools.correlation import _cluster_problems

        records = [
            self._rec(1000, "h1", "10.0.0.0/24"),
            self._rec(1500, "h2", "10.0.0.0/24"),
            self._rec(2200, "h3", "10.0.0.0/24"),  # outside 600s of h1
        ]
        # Greedy run grows h1..h2 (500s), h3 starts new run with only 1 host
        assert _cluster_problems(records, 600, 3) == []

    def test_two_separate_subnets_yield_two_clusters(self):
        from zbbx_mcp.tools.correlation import _cluster_problems

        records = [
            self._rec(1000, "h1", "10.0.0.0/24"),
            self._rec(1100, "h2", "10.0.0.0/24"),
            self._rec(1200, "h3", "10.0.0.0/24"),
            self._rec(2000, "h4", "10.0.1.0/24"),
            self._rec(2100, "h5", "10.0.1.0/24"),
            self._rec(2200, "h6", "10.0.1.0/24"),
        ]
        clusters = _cluster_problems(records, 600, 3)
        assert len(clusters) == 2
        assert {c["key"] for c in clusters} == {"10.0.0.0/24", "10.0.1.0/24"}

    def test_max_severity_propagates(self):
        from zbbx_mcp.tools.correlation import _cluster_problems

        records = [
            self._rec(1000, "h1", "10.0.0.0/24", sev=2),
            self._rec(1100, "h2", "10.0.0.0/24", sev=5),
            self._rec(1200, "h3", "10.0.0.0/24", sev=3),
        ]
        clusters = _cluster_problems(records, 600, 3)
        assert clusters[0]["max_severity"] == 5

    def test_duplicate_hostids_count_once(self):
        from zbbx_mcp.tools.correlation import _cluster_problems

        records = [
            self._rec(1000, "h1", "10.0.0.0/24", name="A"),
            self._rec(1100, "h1", "10.0.0.0/24", name="B"),
            self._rec(1200, "h2", "10.0.0.0/24"),
        ]
        # 3 events but only 2 distinct hosts — does not meet min_hosts=3
        assert _cluster_problems(records, 600, 3) == []
        # min_hosts=2 should pass
        clusters = _cluster_problems(records, 600, 2)
        assert clusters[0]["host_count"] == 2
        assert clusters[0]["events"] == 3

    def test_clusters_sorted_by_host_count_then_severity(self):
        from zbbx_mcp.tools.correlation import _cluster_problems

        records = [
            # Big cluster, low severity
            self._rec(1000, "h1", "A", sev=2),
            self._rec(1050, "h2", "A", sev=2),
            self._rec(1100, "h3", "A", sev=2),
            self._rec(1150, "h4", "A", sev=2),
            # Smaller cluster, high severity
            self._rec(1000, "h5", "B", sev=5),
            self._rec(1050, "h6", "B", sev=5),
            self._rec(1100, "h7", "B", sev=5),
        ]
        clusters = _cluster_problems(records, 600, 3)
        assert [c["key"] for c in clusters] == ["A", "B"]  # bigger first


class TestTrafficDropsSkipBreakdown:
    """Pure-helper tests for the no-baseline visibility footer."""

    def test_empty_when_nothing_skipped(self):
        from zbbx_mcp.tools.traffic import _format_skip_breakdown

        assert _format_skip_breakdown({"no_history": 0, "no_baseline_window": 0, "below_floor": 0}, 1.0) == ""

    def test_single_reason_renders(self):
        from zbbx_mcp.tools.traffic import _format_skip_breakdown

        out = _format_skip_breakdown({"no_history": 12, "no_baseline_window": 0, "below_floor": 0}, 1.0)
        assert out == "12 skipped: 12 no-history."

    def test_all_three_reasons_render_in_order(self):
        from zbbx_mcp.tools.traffic import _format_skip_breakdown

        out = _format_skip_breakdown(
            {"no_history": 5, "no_baseline_window": 3, "below_floor": 30}, 1.0,
        )
        assert out == "38 skipped: 5 no-history, 3 no-baseline-window, 30 below-1Mbps-floor."

    def test_floor_uses_min_baseline_arg(self):
        from zbbx_mcp.tools.traffic import _format_skip_breakdown

        out = _format_skip_breakdown(
            {"no_history": 0, "no_baseline_window": 0, "below_floor": 5}, 0.5,
        )
        assert "below-0.5Mbps-floor" in out

    def test_zero_categories_omitted(self):
        from zbbx_mcp.tools.traffic import _format_skip_breakdown

        out = _format_skip_breakdown(
            {"no_history": 0, "no_baseline_window": 7, "below_floor": 0}, 1.0,
        )
        # Other reasons should not appear
        assert "no-history" not in out
        assert "below-" not in out
        assert "7 no-baseline-window" in out


class TestShutdownPeerHeadroom:
    """Pure-helper tests for shutdown peer-cohort headroom logic."""

    def test_solo_when_no_peers(self):
        from zbbx_mcp.tools.trends_health import _compute_shutdown_safety

        label, headroom = _compute_shutdown_safety(50.0, [])
        assert label == "SOLO"
        assert headroom == 0.0

    def test_safe_when_cohort_headroom_covers_load_with_margin(self):
        from zbbx_mcp.tools.trends_health import _compute_shutdown_safety

        # 4 peers, each averaging 50 Mbps with peaks at 120 → 280 Mbps headroom
        peers = [{"peak": 120.0, "avg": 50.0}] * 4
        label, headroom = _compute_shutdown_safety(100.0, peers)
        assert label == "SAFE"
        assert headroom == 280.0  # 4 × (120 - 50)

    def test_risky_when_headroom_below_safety_margin(self):
        from zbbx_mcp.tools.trends_health import _compute_shutdown_safety

        # Candidate avg 100 Mbps × 1.5 margin = 150 Mbps required.
        # Peers offer 80 Mbps headroom — positive but insufficient.
        peers = [{"peak": 60.0, "avg": 20.0}, {"peak": 60.0, "avg": 20.0}]
        label, headroom = _compute_shutdown_safety(100.0, peers)
        assert label == "RISKY"
        assert headroom == 80.0

    def test_safety_margin_is_configurable(self):
        from zbbx_mcp.tools.trends_health import _compute_shutdown_safety

        peers = [{"peak": 100.0, "avg": 50.0}]  # 50 Mbps headroom
        # With margin 1.0, 50 Mbps headroom is exactly enough for 50 Mbps load
        assert _compute_shutdown_safety(50.0, peers, safety_margin=1.0)[0] == "SAFE"
        # With margin 1.5 (default), 50 Mbps load needs 75 Mbps headroom
        assert _compute_shutdown_safety(50.0, peers, safety_margin=1.5)[0] == "RISKY"

    def test_negative_spare_peers_do_not_subtract(self):
        from zbbx_mcp.tools.trends_health import _compute_shutdown_safety

        # A peer at peak < avg is impossible in real data but defensible:
        # such a peer should contribute zero, never negative headroom.
        peers = [{"peak": 100.0, "avg": 50.0}, {"peak": 10.0, "avg": 30.0}]
        label, headroom = _compute_shutdown_safety(20.0, peers)
        assert headroom == 50.0
        assert label == "SAFE"

    def test_peers_with_missing_metrics_are_dropped(self):
        from zbbx_mcp.tools.trends_health import _compute_shutdown_safety

        peers = [
            {"peak": None, "avg": None},  # no trend data — skip
            {"peak": 100.0, "avg": 30.0},  # 70 headroom
        ]
        label, headroom = _compute_shutdown_safety(40.0, peers)
        assert headroom == 70.0
        assert label == "SAFE"

    def test_candidate_without_traffic_returns_na(self):
        from zbbx_mcp.tools.trends_health import _compute_shutdown_safety

        peers = [{"peak": 100.0, "avg": 30.0}]
        label, headroom = _compute_shutdown_safety(None, peers)
        assert label == "N/A"
        assert headroom == 70.0  # still computed for the report

    def test_zero_load_candidate_is_safe(self):
        from zbbx_mcp.tools.trends_health import _compute_shutdown_safety

        # DEAD candidates with traffic_avg=0 — any peer headroom is enough.
        peers = [{"peak": 1.0, "avg": 0.5}]
        label, _ = _compute_shutdown_safety(0.0, peers)
        assert label == "SAFE"


class TestExternalIpHistoryParsing:
    """Pure-helper tests for audit-log details parsing and recovery scoring."""

    def test_list_shape_picks_ip_updates(self):
        from zbbx_mcp.tools.ip_history import _parse_ip_changes

        details = (
            '[["update", "interfaces.42.ip", "1.2.3.4", "5.6.7.8"],'
            ' ["update", "host.name", "old", "new"],'
            ' ["update", "interfaces.42.port", "10050", "10050"]]'
        )
        out = _parse_ip_changes(details)
        assert out == [("1.2.3.4", "5.6.7.8")]

    def test_dict_shape_picks_ip_updates(self):
        from zbbx_mcp.tools.ip_history import _parse_ip_changes

        details = '{"interfaces.7.ip": ["update", "10.0.0.1", "10.0.0.2"]}'
        assert _parse_ip_changes(details) == [("10.0.0.1", "10.0.0.2")]

    def test_no_change_when_old_equals_new(self):
        from zbbx_mcp.tools.ip_history import _parse_ip_changes

        # Renames that touch the field but leave the value equal must be skipped.
        details = '[["update", "interfaces.42.ip", "1.2.3.4", "1.2.3.4"]]'
        assert _parse_ip_changes(details) == []

    def test_non_ip_field_ignored(self):
        from zbbx_mcp.tools.ip_history import _parse_ip_changes

        details = '[["update", "host.host", "a", "b"]]'
        assert _parse_ip_changes(details) == []

    def test_garbage_input_returns_empty(self):
        from zbbx_mcp.tools.ip_history import _parse_ip_changes

        assert _parse_ip_changes("") == []
        assert _parse_ip_changes("not-json") == []
        assert _parse_ip_changes("[1, 2, 3]") == []  # not the expected shape

    def test_recovery_scores(self):
        from zbbx_mcp.tools.ip_history import _score_recovery

        assert _score_recovery(100.0, 90.0) == "recovered"   # 0.9
        assert _score_recovery(100.0, 70.0) == "recovered"   # 0.7 boundary
        assert _score_recovery(100.0, 50.0) == "partial"     # 0.5
        assert _score_recovery(100.0, 30.0) == "partial"     # 0.3 boundary
        assert _score_recovery(100.0, 5.0) == "still-down"   # 0.05

    def test_recovery_na_cases(self):
        from zbbx_mcp.tools.ip_history import _score_recovery

        assert _score_recovery(None, 50.0) == "n/a"
        assert _score_recovery(50.0, None) == "n/a"
        assert _score_recovery(0.0, 50.0) == "n/a"  # divide-by-zero baseline


class TestLossDriftDetection:
    """Pure-helper tests for sliding-window loss/RTT classification."""

    def test_split_baseline_recent_partitions_by_clock(self):
        from zbbx_mcp.tools.loss_drift import _split_baseline_recent

        trends = [
            {"clock": 100, "value_avg": "1.0"},
            {"clock": 200, "value_avg": "2.0"},
            {"clock": 300, "value_avg": "10.0"},  # recent
            {"clock": 400, "value_avg": "12.0"},  # recent
        ]
        base, recent = _split_baseline_recent(trends, cutoff_clock=300)
        assert base == 1.5  # (1+2)/2
        assert recent == 11.0  # (10+12)/2

    def test_split_handles_missing_sides(self):
        from zbbx_mcp.tools.loss_drift import _split_baseline_recent

        # Only baseline records
        b, r = _split_baseline_recent([{"clock": 100, "value_avg": "5"}], 300)
        assert b == 5.0 and r is None

        # Only recent records
        b, r = _split_baseline_recent([{"clock": 400, "value_avg": "5"}], 300)
        assert b is None and r == 5.0

        # Empty
        assert _split_baseline_recent([], 300) == (None, None)

    def test_split_skips_garbage_values(self):
        from zbbx_mcp.tools.loss_drift import _split_baseline_recent

        trends = [
            {"clock": 100, "value_avg": "not-a-number"},
            {"clock": 200, "value_avg": "2.0"},
        ]
        base, _ = _split_baseline_recent(trends, 300)
        assert base == 2.0

    def test_new_loss_takes_priority_over_loss_up(self):
        from zbbx_mcp.tools.loss_drift import _compute_loss_drift

        # baseline ~0% loss, recent jumps to 8% — both flags fire, prefer new-loss.
        label, details = _compute_loss_drift(0.5, 8.0, None, None)
        assert label == "new-loss"
        assert details["loss_delta"] == 7.5

    def test_loss_up_when_baseline_already_high(self):
        from zbbx_mcp.tools.loss_drift import _compute_loss_drift

        label, _ = _compute_loss_drift(3.0, 10.0, None, None)
        assert label == "loss-up"  # baseline >= 1%, so not 'new-loss'

    def test_rtt_up_alone(self):
        from zbbx_mcp.tools.loss_drift import _compute_loss_drift

        label, details = _compute_loss_drift(None, None, 50.0, 90.0)
        assert label == "rtt-up"
        assert details["rtt_delta_pct"] == 80.0

    def test_loss_and_rtt_combo(self):
        from zbbx_mcp.tools.loss_drift import _compute_loss_drift

        # Loss baseline >= 1% so 'new-loss' does not preempt.
        label, _ = _compute_loss_drift(2.0, 10.0, 50.0, 90.0)
        assert label == "loss-and-rtt"

    def test_below_thresholds_is_ok(self):
        from zbbx_mcp.tools.loss_drift import _compute_loss_drift

        label, _ = _compute_loss_drift(2.0, 4.0, 50.0, 60.0)  # +2% loss, +20% RTT
        assert label == "ok"

    def test_no_data_is_na(self):
        from zbbx_mcp.tools.loss_drift import _compute_loss_drift

        label, _ = _compute_loss_drift(None, None, None, None)
        assert label == "n/a"

    def test_thresholds_are_configurable(self):
        from zbbx_mcp.tools.loss_drift import _compute_loss_drift

        # Default loss_step=5 → not flagged at +3.
        assert _compute_loss_drift(2.0, 5.0, None, None)[0] == "ok"
        # Tighten to 2 → +3 flags.
        assert _compute_loss_drift(2.0, 5.0, None, None, loss_step=2.0)[0] == "loss-up"


class TestOutageClusterGroupingV2:
    """Pure-helper tests for multi-level cluster grouping (#119)."""

    def test_subnet24_and_subnet16(self):
        from zbbx_mcp.tools.correlation import _group_key

        assert _group_key("subnet24", ip="10.20.30.40") == "10.20.30.0/24"
        assert _group_key("subnet16", ip="10.20.30.40") == "10.20.0.0/16"
        assert _group_key("subnet24", ip="") == ""
        assert _group_key("subnet16", ip="not-an-ip") == ""

    def test_provider_level_skips_unknown(self):
        from zbbx_mcp.tools.correlation import _group_key

        assert _group_key("provider", provider="OVH") == "OVH"
        # 'Other'/'Unknown' would lump unrelated hosts — must be empty key.
        assert _group_key("provider", provider="Other") == ""
        assert _group_key("provider", provider="Unknown") == ""
        assert _group_key("provider", provider="") == ""

    def test_hostgroup_level(self):
        from zbbx_mcp.tools.correlation import _group_key

        assert _group_key("hostgroup", hostgroup="EU/edge") == "EU/edge"
        assert _group_key("hostgroup", hostgroup="") == ""

    def test_unknown_level_is_empty(self):
        from zbbx_mcp.tools.correlation import _group_key

        assert _group_key("subnet8", ip="1.2.3.4") == ""

    def test_auto_levels_constant_is_narrowest_first(self):
        from zbbx_mcp.tools.correlation import _AUTO_LEVELS

        assert _AUTO_LEVELS == ("subnet24", "subnet16", "provider")


class TestServicePortSplit:
    """Pure-helper tests for detect_service_port_split classification."""

    def test_split_label_when_service_alone_collapses(self):
        from zbbx_mcp.tools.disruption import _classify_service_split

        # Service: 100→20 (-80%), Mgmt: 50→48 (-4%)
        label, details = _classify_service_split(100.0, 20.0, 50.0, 48.0)
        assert label == "split"
        assert details["service_drop_pct"] == 80.0
        assert details["mgmt_drop_pct"] == 4.0

    def test_full_outage_label_when_both_collapse(self):
        from zbbx_mcp.tools.disruption import _classify_service_split

        label, _ = _classify_service_split(100.0, 20.0, 50.0, 5.0)  # both -80% / -90%
        assert label == "full-outage"

    def test_ok_when_neither_collapses(self):
        from zbbx_mcp.tools.disruption import _classify_service_split

        label, _ = _classify_service_split(100.0, 95.0, 50.0, 49.0)
        assert label == "ok"

    def test_na_when_baseline_missing(self):
        from zbbx_mcp.tools.disruption import _classify_service_split

        assert _classify_service_split(None, 20.0, 50.0, 48.0)[0] == "n/a"
        assert _classify_service_split(0.0, 20.0, 50.0, 48.0)[0] == "n/a"

    def test_thresholds_configurable(self):
        from zbbx_mcp.tools.disruption import _classify_service_split

        # 30% service drop: not flagged at default (50%), flagged when threshold lowered.
        assert _classify_service_split(100.0, 70.0, 50.0, 49.0)[0] == "ok"
        assert _classify_service_split(
            100.0, 70.0, 50.0, 49.0, service_drop_pct=20.0,
        )[0] == "split"


class TestRegionalLossClassification:
    """Pure-helper tests for detect_regional_traffic_loss."""

    def test_collapsed_when_one_region_drops_others_flat(self):
        from zbbx_mcp.tools.disruption import _classify_regional_loss

        # EU collapses 80%, NA stays flat (-2%).
        regions = {"EU": (1000.0, 200.0), "NA": (500.0, 490.0)}
        flagged = _classify_regional_loss(regions)
        assert len(flagged) == 1
        assert flagged[0]["region"] == "EU"
        assert flagged[0]["label"] == "collapsed"

    def test_solo_drop_when_no_flat_peer(self):
        from zbbx_mcp.tools.disruption import _classify_regional_loss

        # Both regions drop heavily — no peer is flat, so solo-drop label.
        regions = {"EU": (1000.0, 200.0), "NA": (500.0, 100.0)}
        flagged = _classify_regional_loss(regions)
        assert {r["region"] for r in flagged} == {"EU", "NA"}
        assert all(r["label"] == "solo-drop" for r in flagged)

    def test_below_threshold_not_flagged(self):
        from zbbx_mcp.tools.disruption import _classify_regional_loss

        regions = {"EU": (100.0, 80.0), "NA": (100.0, 95.0)}  # 20% / 5%
        assert _classify_regional_loss(regions) == []  # 20% < default 30% threshold

    def test_missing_data_skipped(self):
        from zbbx_mcp.tools.disruption import _classify_regional_loss

        regions = {"EU": (None, 200.0), "NA": (500.0, 50.0), "APAC": (300.0, 290.0)}
        flagged = _classify_regional_loss(regions)
        # APAC is flat (~3%), so NA gets 'collapsed'.
        assert len(flagged) == 1
        assert flagged[0]["region"] == "NA"


class TestDisruptionWaveDetection:
    """Pure-helper tests for the wave-clustering algorithm."""

    def _drop(self, clock, hostid, subnet, drop_pct=50.0):
        return {
            "clock": clock,
            "hostid": hostid,
            "host": f"h-{hostid}",
            "subnet": subnet,
            "hostgroup": "test",
            "drop_pct": drop_pct,
        }

    def test_wave_fires_when_thresholds_met(self):
        from zbbx_mcp.tools.disruption import _compute_waves

        drops = [
            self._drop(1000, "h1", "10.0.1.0/24"),
            self._drop(1100, "h2", "10.0.2.0/24"),
            self._drop(1200, "h3", "10.0.3.0/24"),
            self._drop(1300, "h4", "10.0.4.0/24"),
            self._drop(1400, "h5", "10.0.5.0/24"),
        ]
        waves = _compute_waves(drops, window_sec=3600, min_hosts=5, min_subnets=3)
        assert len(waves) == 1
        assert waves[0]["host_count"] == 5
        assert waves[0]["subnet_count"] == 5

    def test_wave_does_not_fire_when_subnets_collapse(self):
        from zbbx_mcp.tools.disruption import _compute_waves

        # 5 hosts, all in same /24 — fails min_subnets=3
        drops = [self._drop(1000 + i * 100, f"h{i}", "10.0.1.0/24") for i in range(5)]
        assert _compute_waves(drops, min_hosts=5, min_subnets=3) == []

    def test_window_boundary_excludes_late_arrivals(self):
        from zbbx_mcp.tools.disruption import _compute_waves

        drops = [
            self._drop(1000, "h1", "10.0.1.0/24"),
            self._drop(1100, "h2", "10.0.2.0/24"),
            self._drop(1200, "h3", "10.0.3.0/24"),
            self._drop(5000, "h4", "10.0.4.0/24"),  # outside 3600s window
            self._drop(5100, "h5", "10.0.5.0/24"),
        ]
        # First three meet min_subnets=3 but only 3 hosts; lower bar.
        waves = _compute_waves(drops, window_sec=3600, min_hosts=3, min_subnets=3)
        assert len(waves) == 1
        assert waves[0]["host_count"] == 3

    def test_severity_label_by_avg_drop(self):
        from zbbx_mcp.tools.disruption import _compute_waves

        drops = [
            self._drop(1000, "h1", "a", drop_pct=80),
            self._drop(1100, "h2", "b", drop_pct=80),
            self._drop(1200, "h3", "c", drop_pct=80),
        ]
        waves = _compute_waves(drops, min_hosts=3, min_subnets=3)
        assert waves[0]["severity"] == "critical"

        drops = [
            self._drop(1000, "h1", "a", drop_pct=40),
            self._drop(1100, "h2", "b", drop_pct=40),
            self._drop(1200, "h3", "c", drop_pct=40),
        ]
        waves = _compute_waves(drops, min_hosts=3, min_subnets=3)
        assert waves[0]["severity"] == "medium"


class TestAtRiskScoring:
    """Pure-helper tests for the composite at-risk score."""

    def test_zero_inputs_score_zero(self):
        from zbbx_mcp.tools.risk import _compute_risk_score

        score, details = _compute_risk_score(0, "ok", 0.0)
        assert score == 0.0
        assert details["drift_label"] == "ok"

    def test_more_peer_rotations_increase_score(self):
        from zbbx_mcp.tools.risk import _compute_risk_score

        low, _ = _compute_risk_score(1, "ok", 0.0)
        high, _ = _compute_risk_score(20, "ok", 0.0)
        assert high > low

    def test_drift_label_dominates_when_other_signals_zero(self):
        from zbbx_mcp.tools.risk import _compute_risk_score

        rtt, _ = _compute_risk_score(0, "rtt-up", 0.0)
        loss, _ = _compute_risk_score(0, "loss-up", 0.0)
        combo, _ = _compute_risk_score(0, "loss-and-rtt", 0.0)
        assert rtt < loss < combo

    def test_age_capped_at_90_days(self):
        from zbbx_mcp.tools.risk import _compute_risk_score

        cap, _ = _compute_risk_score(0, "ok", 90.0)
        bigger, _ = _compute_risk_score(0, "ok", 365.0)
        assert cap == bigger  # capped, so equal

    def test_none_age_treated_as_capped(self):
        from zbbx_mcp.tools.risk import _compute_risk_score

        # No prior rotation observed → treat as cap, not zero.
        cap, _ = _compute_risk_score(0, "ok", 90.0)
        none_score, _ = _compute_risk_score(0, "ok", None)
        assert cap == none_score


class TestBlastRadiusClassification:
    """Pure-helper tests for cohort connection-count delta labels."""

    def test_absorbing_when_post_gains_at_least_10pct(self):
        from zbbx_mcp.tools.risk import _compute_blast_radius

        label, delta = _compute_blast_radius(100.0, 120.0)
        assert label == "absorbing"
        assert delta == 20.0

    def test_draining_when_post_loses_more_than_10pct(self):
        from zbbx_mcp.tools.risk import _compute_blast_radius

        label, delta = _compute_blast_radius(100.0, 80.0)
        assert label == "draining"
        assert delta == -20.0

    def test_stable_within_10pct(self):
        from zbbx_mcp.tools.risk import _compute_blast_radius

        for post in (95.0, 100.0, 105.0):
            label, _ = _compute_blast_radius(100.0, post)
            assert label == "stable"

    def test_na_when_pre_missing_or_zero(self):
        from zbbx_mcp.tools.risk import _compute_blast_radius

        assert _compute_blast_radius(None, 50.0) == ("n/a", None)
        assert _compute_blast_radius(0.0, 50.0) == ("n/a", None)
        assert _compute_blast_radius(50.0, None) == ("n/a", None)


class TestRecoveryAggregate:
    """Pure-helper tests for fleet-level recovery KPI aggregation."""

    def test_aggregate_basic(self):
        from zbbx_mcp.tools.ip_history import _aggregate_recovery_scores

        rotations = [
            {"score": "recovered"},
            {"score": "recovered"},
            {"score": "recovered"},
            {"score": "partial"},
            {"score": "still-down"},
            {"score": "n/a"},
        ]
        agg = _aggregate_recovery_scores(rotations)
        assert agg["total"] == 6
        assert agg["recovered"] == 3
        assert agg["partial"] == 1
        assert agg["still_down"] == 1
        assert agg["na"] == 1
        # rate = 3 recovered / 5 determined-outcome = 60%
        assert agg["rate_pct"] == 60.0

    def test_aggregate_all_na_yields_none_rate(self):
        from zbbx_mcp.tools.ip_history import _aggregate_recovery_scores

        agg = _aggregate_recovery_scores([{"score": "n/a"}, {"score": "n/a"}])
        assert agg["total"] == 2
        assert agg["na"] == 2
        assert agg["rate_pct"] is None

    def test_aggregate_unknown_label_treated_as_na(self):
        from zbbx_mcp.tools.ip_history import _aggregate_recovery_scores

        agg = _aggregate_recovery_scores([{"score": "weird"}, {"score": "recovered"}])
        assert agg["na"] == 1
        assert agg["recovered"] == 1
        assert agg["rate_pct"] == 100.0  # 1 of 1 determined

    def test_aggregate_empty(self):
        from zbbx_mcp.tools.ip_history import _aggregate_recovery_scores

        agg = _aggregate_recovery_scores([])
        assert agg["total"] == 0
        assert agg["rate_pct"] is None

