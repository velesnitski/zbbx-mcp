"""Geo, country, and provider-detection helper tests (split from test_analytics, ADR 074)."""

from zbbx_mcp.classify import (
    PROVIDER_CIDRS,
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

class TestNormalizeCountry:
    """Pure-helper tests for normalize_country (#140)."""

    def test_iso2_passthrough(self):
        from zbbx_mcp.data import normalize_country

        for v in ["RU", "ru", "Ru", " ru "]:
            assert normalize_country(v) == "RU"

    def test_uk_alias_to_gb(self):
        from zbbx_mcp.data import normalize_country

        # The existing extract_country alias ("UK"→"GB") is preserved.
        assert normalize_country("UK") == "GB"
        assert normalize_country("uk") == "GB"

    def test_iso3_recognised(self):
        from zbbx_mcp.data import normalize_country

        assert normalize_country("RUS") == "RU"
        assert normalize_country("usa") == "US"
        assert normalize_country("DEU") == "DE"

    def test_full_name_recognised(self):
        from zbbx_mcp.data import normalize_country

        assert normalize_country("Russia") == "RU"
        assert normalize_country("UNITED STATES") == "US"
        assert normalize_country("Saudi Arabia") == "SA"
        assert normalize_country("Czechia") == "CZ"
        assert normalize_country("Czech Republic") == "CZ"
        assert normalize_country("United Kingdom") == "GB"
        assert normalize_country("UAE") == "AE"

    def test_empty_and_unknown_return_empty(self):
        from zbbx_mcp.data import normalize_country

        assert normalize_country("") == ""
        assert normalize_country("   ") == ""
        assert normalize_country(None) == ""  # type: ignore[arg-type]
        assert normalize_country("Atlantis") == ""
        assert normalize_country("ZZZ") == ""

    def test_two_letter_unknown_still_returns_iso2_like(self):
        from zbbx_mcp.data import normalize_country

        # We don't enumerate the full ISO-2 set; any 2-letter alphabetic
        # input is treated as a code (the downstream filter just won't
        # match any host). UK-alias normalisation still applies.
        assert normalize_country("ZZ") == "ZZ"

class TestResolveCountry:
    """Pure-helper tests for resolve_country chain (#141)."""

    def test_extract_country_takes_precedence(self):
        from zbbx_mcp.data import resolve_country

        h = {
            "host": "edge-de01",
            "inventory": {"country_code": "FR", "country_name": "France"},
        }
        # Hostname says DE; inventory disagreement loses to the name.
        assert resolve_country(h) == "DE"

    def test_inventory_country_code_used_when_hostname_empty(self):
        from zbbx_mcp.data import resolve_country

        h = {"host": "control-plane-01", "inventory": {"country_code": "us"}}
        assert resolve_country(h) == "US"

    def test_inventory_country_name_used_when_code_empty(self):
        from zbbx_mcp.data import resolve_country

        h = {
            "host": "control-plane-01",
            "inventory": {"country_code": "", "country_name": "Russia"},
        }
        assert resolve_country(h) == "RU"

    def test_returns_empty_when_all_sources_empty(self):
        from zbbx_mcp.data import resolve_country

        h = {"host": "control-plane-01", "inventory": {}}
        assert resolve_country(h) == ""

    def test_handles_missing_inventory_field(self):
        from zbbx_mcp.data import resolve_country

        # Older Zabbix host.get without selectInventory — no field at all.
        h = {"host": "control-plane-01"}
        assert resolve_country(h) == ""

    def test_inventory_unknown_name_falls_through(self):
        from zbbx_mcp.data import resolve_country

        h = {"host": "weird", "inventory": {"country_name": "Atlantis"}}
        assert resolve_country(h) == ""

class TestClassifyCountryGroup:
    """Pure-helper tests for service_brief per-country group fold (#154, ADR 045)."""

    def test_real_traffic_is_validated(self):
        from zbbx_mcp.tools.service_brief import _classify_country_group
        # summed box traffic >= floor → validated regardless of checks
        assert _classify_country_group(6.0, [0, 0]) == "validated"

    def test_no_traffic_no_checks_skipped(self):
        from zbbx_mcp.tools.service_brief import _classify_country_group
        assert _classify_country_group(0.0, []) == "skip"

    def test_all_checks_up_is_ok(self):
        from zbbx_mcp.tools.service_brief import _classify_country_group
        assert _classify_country_group(0.0, [1, 1, 1]) == "ok"

    def test_one_failing_vip_check_drops_to_partial(self):
        from zbbx_mcp.tools.service_brief import _classify_country_group
        # worst-wins across merged VIP checks: a single failure → partial
        assert _classify_country_group(0.0, [1, 1, 0]) == "partial"

    def test_all_checks_down_is_down(self):
        from zbbx_mcp.tools.service_brief import _classify_country_group
        assert _classify_country_group(0.0, [0, 0]) == "down"

    def test_summed_subhost_traffic_validates_box(self):
        from zbbx_mcp.tools.service_brief import _classify_country_group
        # two VIPs at 3 Mbps each = 6 summed → above the 5 Mbps floor.
        # (the point of the fold: per-VIP each would be below the floor)
        assert _classify_country_group(3.0 + 3.0, [0]) == "validated"
