import pytest

from zbbx_mcp.data import build_max_map, build_value_map, extract_country
from zbbx_mcp.utils import ROLLBACK_STRIP_FIELDS, format_results, parse_time


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
        assert extract_country("srv-nl9999") == "NL"

    def test_de(self):
        assert extract_country("srv-de9003") == "DE"

    def test_us(self):
        assert extract_country("srv-us9999") == "US"

    def test_nl_lite(self):
        assert extract_country("srv-nl9003") == "NL"

    def test_us_lite(self):
        assert extract_country("srv-us9003") == "US"

    def test_tr_lite(self):
        assert extract_country("srv-tr9003") == "TR"

    def test_no_country(self):
        assert extract_country("Zabbix server") == ""

    def test_short_name(self):
        assert extract_country("he13") == ""


# The map builders read through ``read_item`` (ADR 141): a value is a reading
# only while the item reports, so every fixture carries a fresh clock.
NOW = 1_800_000_000


def _it(hid, value, clock=NOW):
    return {"hostid": hid, "lastvalue": value, "lastclock": str(clock)}


class TestBuildValueMap:
    def test_basic(self):
        assert build_value_map([_it("1", "42.5")], now=NOW)["1"] == 42.5

    def test_transform(self):
        result = build_value_map([_it("1", "95")], lambda v: round(100 - float(v), 1), now=NOW)
        assert result["1"] == 5.0

    def test_skips_invalid(self):
        assert "1" not in build_value_map([_it("1", "not_a_number")], now=NOW)

    def test_missing_clock_fails_closed(self):
        # A caller that did not request lastclock gets nothing, not stale numbers.
        assert build_value_map([{"hostid": "1", "lastvalue": "42.5"}], now=NOW) == {}

    def test_stale_and_never_collected_are_absent(self):
        items = [_it("1", "42.5", clock=NOW - 86400), _it("2", "0", clock=0), _it("3", "7")]
        assert build_value_map(items, now=NOW) == {"3": 7.0}

    def test_empty(self):
        assert build_value_map([]) == {}


class TestBuildMaxMap:
    def test_picks_max(self):
        items = [_it("1", "100"), _it("1", "500"), _it("1", "200")]
        assert build_max_map(items, now=NOW)["1"] == 500.0

    def test_multiple_hosts(self):
        result = build_max_map([_it("1", "100"), _it("2", "200")], now=NOW)
        assert result["1"] == 100.0
        assert result["2"] == 200.0

    def test_a_stale_interface_does_not_win(self):
        # The busiest interface a day ago is not the busiest one now.
        items = [_it("1", "900", clock=NOW - 86400), _it("1", "100")]
        assert build_max_map(items, now=NOW) == {"1": 100.0}


class TestParseTime:
    def test_epoch_int(self):
        assert parse_time(1715000000) == 1715000000

    def test_epoch_string(self):
        assert parse_time("1715000000") == 1715000000

    def test_iso_date(self):
        # UTC midnight of 2026-04-19
        assert parse_time("2026-04-19") == 1776556800

    def test_iso_datetime_space(self):
        # 2026-04-19T10:30:00 UTC
        assert parse_time("2026-04-19 10:30:00") == 1776594600

    def test_iso_datetime_t(self):
        assert parse_time("2026-04-19T10:30:00") == 1776594600

    def test_relative_hours(self):
        result = parse_time("24h", now=1_000_000)
        assert result == 1_000_000 - 24 * 3600

    def test_relative_days(self):
        result = parse_time("7d", now=1_000_000)
        assert result == 1_000_000 - 7 * 86400

    def test_relative_minutes(self):
        result = parse_time("30m", now=1_000_000)
        assert result == 1_000_000 - 30 * 60

    def test_relative_seconds(self):
        result = parse_time("90s", now=1_000_000)
        assert result == 1_000_000 - 90

    def test_relative_weeks(self):
        result = parse_time("2w", now=1_000_000)
        assert result == 1_000_000 - 2 * 604800

    def test_relative_case_insensitive(self):
        result = parse_time("1H", now=1_000_000)
        assert result == 1_000_000 - 3600

    def test_invalid_empty(self):
        with pytest.raises(ValueError):
            parse_time("")

    def test_invalid_garbage(self):
        with pytest.raises(ValueError):
            parse_time("not-a-time")


class TestParseDelaySeconds:
    def test_bare_seconds(self):
        from zbbx_mcp.tools.items import _parse_delay_seconds
        assert _parse_delay_seconds("60") == 60

    def test_suffix_s(self):
        from zbbx_mcp.tools.items import _parse_delay_seconds
        assert _parse_delay_seconds("30s") == 30

    def test_minutes(self):
        from zbbx_mcp.tools.items import _parse_delay_seconds
        assert _parse_delay_seconds("5m") == 300

    def test_hours(self):
        from zbbx_mcp.tools.items import _parse_delay_seconds
        assert _parse_delay_seconds("2h") == 7200

    def test_complex_schedule_falls_back(self):
        from zbbx_mcp.tools.items import _parse_delay_seconds
        # Scheduled interval — use the plain-interval head if parseable, else default
        assert _parse_delay_seconds("30s;wd1-5,9:00-18:00/1m") == 30

    def test_empty(self):
        from zbbx_mcp.tools.items import _parse_delay_seconds
        assert _parse_delay_seconds("") == 300

    def test_unparseable(self):
        from zbbx_mcp.tools.items import _parse_delay_seconds
        assert _parse_delay_seconds("weird-expression") == 300


class TestRollbackStripFields:
    def test_contains_required(self):
        assert "lastchange" in ROLLBACK_STRIP_FIELDS
        assert "flags" in ROLLBACK_STRIP_FIELDS
        assert "lastvalue" in ROLLBACK_STRIP_FIELDS

    def test_is_frozenset(self):
        assert isinstance(ROLLBACK_STRIP_FIELDS, frozenset)
