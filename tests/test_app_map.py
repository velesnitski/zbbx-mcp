"""ADR 143: the catalogue is a map the tool is given — loader, join, age.
Fixtures are synthetic: documentation prefixes, reserved-band example names."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from zbbx_mcp.app_map import (
    APP_MAP_ENV,
    DEFAULT_PREDICATE_WINDOW_S,
    AppMap,
    Entry,
    Member,
    index_hosts_by_ip,
    join_entry,
    load_app_map,
    map_age_s,
    parse_app_map,
    predicate_window_s,
)

GOOD = {
    "generated_at": "2026-01-01T00:00:00Z",
    "source": "db",
    "predicate": "active and heartbeat within 300 s",
    "audiences": {
        "default": {
            "sections": [
                {"name": "Free", "entries": [
                    {"key": "aq_free", "title": "Base – Free", "code": "AQ",
                     "members": [{"ip": "198.51.100.1", "load": 0.21, "clients": 34},
                                 {"ip": "198.51.100.2"}],
                     "display_load": 0.04},
                ]},
                {"name": "Paid", "entries": [
                    {"key": "bv_plus", "title": "Base – Plus", "code": "auto",
                     "members": [{"ip": "203.0.113.1"}]},
                ]},
            ]
        }
    },
}


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.delenv(APP_MAP_ENV, raising=False)


class TestLoader:
    def test_unset_env_names_the_variable(self):
        m, why = load_app_map()
        assert m is None
        assert why == f"{APP_MAP_ENV} is not set"

    def test_missing_file_is_named(self, tmp_path):
        path = tmp_path / "missing.local.json"
        m, why = load_app_map(str(path))
        assert m is None
        assert why == f"file not found: {path}"

    def test_malformed_json_file(self, tmp_path):
        path = tmp_path / "bad.local.json"
        path.write_text("{not json")
        m, why = load_app_map(str(path))
        assert m is None
        assert why.startswith(f"invalid JSON in {path}"), why

    def test_malformed_inline_json(self):
        m, why = load_app_map("{not json")
        assert m is None
        assert why.startswith("invalid JSON in inline value"), why

    @pytest.mark.parametrize("data, needle", [
        ([], "top level must be an object"),
        ({"generated_at": "2026-01-01T00:00:00Z"}, "audiences must be a non-empty object"),
        ({"generated_at": "2026-01-01T00:00:00Z", "audiences": {}}, "audiences must be a non-empty object"),
        ({"audiences": {"default": {"sections": []}}}, "generated_at is missing"),
        ({"generated_at": "yesterday", "audiences": {"default": {"sections": []}}}, "generated_at is not ISO-8601"),
        ({"generated_at": "2026-01-01T00:00:00Z",
          "audiences": {"default": {"sections": [{"name": "Free", "entries": [{"title": "x"}]}]}}},
         "entry without a key"),
        ({"generated_at": "2026-01-01T00:00:00Z",
          "audiences": {"default": {"sections": [{"name": "Free", "entries": [
              {"key": "aq_free", "members": [{"ip": "198.51.100.1"}, {"ip": 7}]}]}]}}},
         "member ip must be a string"),
        ({"generated_at": "2026-01-01T00:00:00Z",
          "audiences": {"default": {"sections": [{"name": "Free", "entries": [
              {"key": "aq_free", "members": [{"ip": "198.51.100.1", "clients": "many"}]}]}]}}},
         "clients must be an integer"),
    ])
    def test_wrong_shape_rejects_the_whole_map(self, data, needle):
        # Fail CLOSED: one bad member is enough — no partial map (ADR 120/143).
        m, why = load_app_map(json.dumps(data))
        assert m is None
        assert why.startswith("wrong shape: "), why
        assert needle in why, why

    def test_good_file(self, tmp_path):
        path = tmp_path / "map.local.json"
        path.write_text(json.dumps(GOOD))
        m, why = load_app_map(str(path))
        assert why == ""
        assert isinstance(m, AppMap)
        assert m.generated_at == datetime(2026, 1, 1, tzinfo=timezone.utc)
        assert m.source == "db"
        assert m.predicate == "active and heartbeat within 300 s"
        assert list(m.audiences) == ["default"]
        aud = m.audiences["default"]
        assert [s.name for s in aud.sections] == ["Free", "Paid"]
        e = aud.sections[0].entries[0]
        assert (e.key, e.title, e.code, e.display_load) == ("aq_free", "Base – Free", "AQ", 0.04)
        assert e.members == (Member("198.51.100.1", 0.21, 34), Member("198.51.100.2", None, None))
        assert aud.sections[1].entries[0].display_load is None

    def test_env_var_is_read_when_no_source_given(self, monkeypatch):
        monkeypatch.setenv(APP_MAP_ENV, json.dumps(GOOD))
        m, why = load_app_map()
        assert m is not None and why == ""

    def test_naive_timestamp_is_taken_as_utc(self):
        m = parse_app_map({**GOOD, "generated_at": "2026-01-01T00:00:00"})
        assert m.generated_at == datetime(2026, 1, 1, tzinfo=timezone.utc)

    def test_no_caching_between_calls(self, tmp_path):
        # The exporter rewrites the file; a held copy would defeat the age check.
        path = tmp_path / "map.local.json"
        path.write_text(json.dumps(GOOD))
        first, _ = load_app_map(str(path))
        path.write_text(json.dumps({**GOOD, "generated_at": "2026-02-01T00:00:00Z"}))
        second, _ = load_app_map(str(path))
        assert first.generated_at != second.generated_at


def _host(hid, name, *ips):
    return {"hostid": hid, "host": name, "interfaces": [{"ip": ip} for ip in ips]}


class TestJoin:
    def test_index_covers_every_interface_and_skips_loopback(self):
        hosts = [_host("1", "srv-aq9001", "198.51.100.1", "203.0.113.1", "127.0.0.1", "")]
        by_ip = index_hosts_by_ip(hosts)
        assert set(by_ip) == {"198.51.100.1", "203.0.113.1"}
        assert by_ip["203.0.113.1"]["host"] == "srv-aq9001"

    def test_first_host_keeps_a_shared_address(self):
        hosts = [_host("1", "srv-aq9001", "198.51.100.1"), _host("2", "srv-aq9002", "198.51.100.1")]
        assert index_hosts_by_ip(hosts)["198.51.100.1"]["host"] == "srv-aq9001"

    def test_join_names_the_unmatched_and_dedups_by_host(self):
        hosts = [_host("1", "srv-aq9001", "198.51.100.1", "203.0.113.1"), _host("2", "srv-aq9002", "198.51.100.2")]
        by_ip = index_hosts_by_ip(hosts)
        e = Entry(key="aq_free", title="Base – Free", code="AQ", members=(
            Member("198.51.100.9"),      # nobody
            Member("198.51.100.2"),
            Member("203.0.113.1"),       # second interface of host 1
            Member("198.51.100.1"),      # host 1 again — one server, not two
            Member("198.51.100.8"),      # nobody
        ))
        matched, unmatched = join_entry(e, by_ip)
        assert [h["host"] for h in matched] == ["srv-aq9002", "srv-aq9001"]
        assert unmatched == ["198.51.100.9", "198.51.100.8"]

    def test_empty_entry_joins_to_nothing(self):
        assert join_entry(Entry(key="k", title="k", code="auto"), {}) == ([], [])


class TestAge:
    def test_age_from_now(self):
        m = parse_app_map(GOOD)
        now = (m.generated_at + timedelta(minutes=4)).timestamp()
        assert map_age_s(m, now) == 240.0

    def test_age_is_never_negative(self):
        m = parse_app_map(GOOD)
        assert map_age_s(m, m.generated_at.timestamp() - 30) == 0.0

    @pytest.mark.parametrize("predicate, want", [
        ("active and heartbeat within 300 s", 300),
        ("heartbeat within 120 seconds", 120),
        ("seen in the last 5 min", 300),
        ("active in the last 2 minutes", 120),
        ("active", DEFAULT_PREDICATE_WINDOW_S),
        ("", DEFAULT_PREDICATE_WINDOW_S),
    ])
    def test_window_is_parsed_from_the_predicate(self, predicate, want):
        assert predicate_window_s(predicate) == want


class TestMembersWithoutAddress:
    """The exporter keeps a member whose address column is NULL (it is still
    something the product offers) and writes ``"ip": null``. That is a count
    here, not a rejection — and not a silent drop either."""

    def test_null_and_empty_ip_are_counted_not_rejected(self):
        m, why = load_app_map(json.dumps({
            "generated_at": "2026-01-01T00:00:00Z",
            "audiences": {"default": {"sections": [{"name": "Free", "entries": [
                {"key": "aq_free", "members": [
                    {"ip": "198.51.100.1", "load": 0.2},
                    {"ip": None, "load": 0.5},        # exporter: address column NULL
                    {"ip": "  "},                     # blank is the same thing
                ]},
            ]}]}},
        }))
        assert m is not None, why
        e = m.audiences["default"].sections[0].entries[0]
        assert [x.ip for x in e.members] == ["198.51.100.1"]
        assert e.without_address == 2
