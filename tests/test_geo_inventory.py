"""ADR 138: a country filter that means where the box is, not what it is named.
Fixtures are neutral; cities are illustrative."""

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp.tools import geo_inventory
from zbbx_mcp.tools.geo_inventory import bucket_hosts, datacenter_cc, is_free_tier


class TestDatacenterCc:
    def test_code_is_the_last_token(self):
        assert datacenter_cc("Base, AQ") == "AQ"
        assert datacenter_cc("Bouvet, BV") == "BV"
        assert datacenter_cc("  Base, aq ") == "AQ"

    def test_no_location_is_empty_not_a_guess(self):
        # "" must read as UNKNOWN downstream, never as "not this country".
        assert datacenter_cc("") == ""
        assert datacenter_cc(None) == ""
        assert datacenter_cc("Base") == ""


class TestFreeTier:
    def test_mirrors_product_summary(self):
        assert is_free_tier("Free")
        assert is_free_tier("Free Plus")
        assert is_free_tier("Relay")
        assert not is_free_tier("Premium")
        assert not is_free_tier(None)


def _host(hid, name, ip, group="app_free"):
    return {"hostid": hid, "host": name, "groups": [{"name": group}],
            "interfaces": [{"ip": ip, "main": "1", "type": "1"}]}


class TestBucketHosts:
    def test_three_buckets_and_none_is_silently_folded(self, monkeypatch):
        # Real geo comes from resolve_datacenter; stub it so the test owns it.
        geo = {"10.0.0.1": ("P", "Base, AQ"), "10.0.0.2": ("P", "Bouvet, BV"),
               "10.0.0.3": ("P", ""), "10.0.0.4": ("P", "Base, AQ")}
        monkeypatch.setattr(geo_inventory, "resolve_datacenter", lambda ip: geo.get(ip, ("Unknown", "")))
        hosts = [
            _host("1", "srv-aq01", "10.0.0.1"),   # named aq, in AQ  -> in_geo
            _host("2", "srv-aq02", "10.0.0.2"),   # named aq, in BV  -> named_elsewhere
            _host("3", "srv-aq03", "10.0.0.3"),   # named aq, no dc  -> unresolved
            _host("4", "srv-hm01", "10.0.0.4"),   # named hm, in AQ  -> in_geo (the case a name filter misses)
        ]
        b = bucket_hosts(hosts, "aq")
        assert [h["host"] for h in b["in_geo"]] == ["srv-aq01", "srv-hm01"]
        assert [h["host"] for h in b["named_elsewhere"]] == ["srv-aq02"]
        assert [h["host"] for h in b["unresolved"]] == ["srv-aq03"]
        assert b["named_elsewhere"][0]["_city"] == "Bouvet, BV"

    def test_a_host_named_elsewhere_and_unresolved_is_ignored(self, monkeypatch):
        monkeypatch.setattr(geo_inventory, "resolve_datacenter", lambda ip: ("Unknown", ""))
        b = bucket_hosts([_host("9", "srv-bv01", "10.9.9.9")], "fr")
        assert all(not v for v in b.values())


class TestGetGeoInventoryWire:
    def _client(self):
        hosts = [
            _host("1", "srv-aq01", "10.0.0.1", "app_free"),
            _host("2", "srv-aq02", "10.0.0.2", "app_free"),
            _host("3", "srv-hm01", "10.0.0.4", "app_free"),
        ]
        traffic = [{"itemid": "t1", "hostid": "1", "key_": "net.if.in[eth0]", "lastvalue": "8000000", "lastclock": "1760000000"},
                   {"itemid": "t3", "hostid": "3", "key_": "net.if.in[eth0]", "lastvalue": "2000000", "lastclock": "1760000000"}]
        cpu = [{"hostid": "1", "lastvalue": "90"}, {"hostid": "3", "lastvalue": "70"}]

        def items(p):
            key = p.get("filter", {}).get("key_")
            if key == "system.cpu.util[,idle]":
                return cpu
            return traffic
        return RecordingClient({"host.get": hosts, "item.get": items})

    def test_counts_by_datacenter_and_names_the_mislabelled(self, monkeypatch):
        geo = {"10.0.0.1": ("P", "Base, AQ"), "10.0.0.2": ("P", "Bouvet, BV"), "10.0.0.4": ("P", "Base, AQ")}
        monkeypatch.setattr(geo_inventory, "resolve_datacenter", lambda ip: geo.get(ip, ("Unknown", "")))
        out = run_tool(geo_inventory, "get_geo_inventory", self._client(), country="aq")
        assert "**2 host(s)** physically in AQ" in out, out
        assert "10 Mbps" in out                       # 8 + 2, carrier NICs summed
        assert "srv-aq02 → Bouvet, BV" in out        # named fr, sits in DE — excluded and said so
        assert "named for another country" in out     # srv-hm01 is in FR but named nl
        assert "`hm`×1" in out

    def test_a_bad_country_code_is_refused(self):
        out = run_tool(geo_inventory, "get_geo_inventory", self._client(), country="France")
        assert out.startswith("country must be")

    def test_asks_for_lastclock(self, monkeypatch):
        monkeypatch.setattr(geo_inventory, "resolve_datacenter", lambda ip: ("P", "Base, AQ"))
        c = self._client()
        run_tool(geo_inventory, "get_geo_inventory", c, country="aq")
        traffic_calls = [p for m, p in c.calls if m == "item.get" and "lastclock" in (p.get("output") or [])]
        assert traffic_calls, "traffic items must be fetched with lastclock (ADR 137)"
