from zbbx_mcp.data import build_max_map, build_value_map, extract_country
from zbbx_mcp.utils import ROLLBACK_STRIP_FIELDS, format_results


class TestFormatResults:
    def test_empty_data(self):
        result = format_results([], lambda x: "No items.", "items", 50)
        assert result == "No items."

    def test_with_data(self):
        data = [{"name": "a"}, {"name": "b"}]
        result = format_results(data, lambda x: "formatted", "items", 50)
        assert "Found: 2 items" in result
        assert "formatted" in result

    def test_truncation_notice(self):
        data = [{"name": str(i)} for i in range(50)]
        result = format_results(data, lambda x: "list", "items", 50)
        assert "showing first 50" in result

    def test_no_truncation(self):
        data = [{"name": "a"}]
        result = format_results(data, lambda x: "one", "items", 50)
        assert "showing first" not in result


class TestExtractCountry:
    def test_nl(self):
        assert extract_country("srv-nl0105") == "NL"

    def test_de(self):
        assert extract_country("srv-de3") == "DE"

    def test_us(self):
        assert extract_country("srv-us0001") == "US"

    def test_nl_lite(self):
        assert extract_country("srv-nl01-lite") == "NL"

    def test_us_lite(self):
        assert extract_country("srv-us01-lite") == "US"

    def test_tr_lite(self):
        assert extract_country("srv-tr01-lite") == "TR"

    def test_no_country(self):
        assert extract_country("Zabbix server") == ""

    def test_short_name(self):
        assert extract_country("he13") == ""


class TestBuildValueMap:
    def test_basic(self):
        items = [{"hostid": "1", "lastvalue": "42.5"}]
        result = build_value_map(items)
        assert result["1"] == 42.5

    def test_transform(self):
        items = [{"hostid": "1", "lastvalue": "95"}]
        result = build_value_map(items, lambda v: round(100 - float(v), 1))
        assert result["1"] == 5.0

    def test_skips_invalid(self):
        items = [{"hostid": "1", "lastvalue": "not_a_number"}]
        result = build_value_map(items)
        assert "1" not in result

    def test_empty(self):
        assert build_value_map([]) == {}


class TestBuildMaxMap:
    def test_picks_max(self):
        items = [
            {"hostid": "1", "lastvalue": "100"},
            {"hostid": "1", "lastvalue": "500"},
            {"hostid": "1", "lastvalue": "200"},
        ]
        result = build_max_map(items)
        assert result["1"] == 500.0

    def test_multiple_hosts(self):
        items = [
            {"hostid": "1", "lastvalue": "100"},
            {"hostid": "2", "lastvalue": "200"},
        ]
        result = build_max_map(items)
        assert result["1"] == 100.0
        assert result["2"] == 200.0


class TestRollbackStripFields:
    def test_contains_required(self):
        assert "lastchange" in ROLLBACK_STRIP_FIELDS
        assert "flags" in ROLLBACK_STRIP_FIELDS
        assert "lastvalue" in ROLLBACK_STRIP_FIELDS

    def test_is_frozenset(self):
        assert isinstance(ROLLBACK_STRIP_FIELDS, frozenset)
