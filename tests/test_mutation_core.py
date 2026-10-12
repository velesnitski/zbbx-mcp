"""Tests that kill surviving mutants in the pure core (scripts/mutation.sh).

Each test pins a boundary or a branch that the behavioural suite reached
but did not distinguish. Fixtures are synthetic.

Equivalent mutants left alive on purpose (no input tells them apart):

- ``reading.read_item``: ``raw_clock is None or raw_clock == ""`` mutated to
  ``and``, or to compare with another literal — ``None`` and ``""`` both fall
  through to ``int()``, which fails, and land in ``missing_clock`` anyway.
- ``budget.response_budget``: the ``os.environ.get`` default mutated from
  ``""`` to any non-numeric literal — both end in ``DEFAULT_RESPONSE_BUDGET``.
- ``budget.fit_host_blocks``: the ``if footer else 0`` term of ``need``
  mutated to add something when there is no closing line. Only the LAST
  block has no closing line, and the loop runs only when everything exceeds
  the budget — so that block's ``need`` (= everything) already exceeds it and
  it is dropped either way.
- ``app_map._parse_generated_at``: the ``"Z"`` suffix mutated — Python 3.11+
  parses a ``Z`` suffix natively (the branch exists for 3.10 only).
- ``app_map.predicate_window_s``: the ``predicate or ""`` fallback mutated to
  a literal without a time figure — both give the default window.
- ``app_map.index_hosts_by_ip``: ``h.get("interfaces", [])`` mutated to a
  ``None`` default — the ``or []`` after it absorbs the difference.
- ``app_map.join_entry``: the ``hostid`` default mutated to another literal —
  the default only groups hosts that carry no id; Zabbix ids are numeric, so
  no real host collides with any literal default.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from unittest import mock

import pytest

from zbbx_mcp.app_map import load_app_map, map_age_s, parse_app_map
from zbbx_mcp.budget import (
    OMITTED_NAMES_MAX,
    _label_list,
    all_names_unknown_message,
    fit_host_blocks,
    missing_hosts_note,
    no_match_message,
    omitted_hosts_line,
    render_budget,
)
from zbbx_mcp.reading import read_item

NOW = 1_800_000_000

# --- reading -----------------------------------------------------------------


class TestReading:
    def test_text_without_a_unit_has_no_trailing_space(self):
        r = read_item({"lastclock": str(NOW - 60), "lastvalue": "12.34"}, now=NOW)
        assert r.text() == "12.3"
        assert r.text("%") == "12.3 %"

    def test_clock_of_one_is_a_stale_reading_not_never(self):
        # Only 0 (and below) is the never-collected sentinel; any real clock,
        # however old, is a reading that went stale.
        r = read_item({"lastclock": "1", "lastvalue": "5"}, now=NOW)
        assert (r.state, r.age_s, r.value) == ("stale", NOW - 1, None)


# --- budget: small helpers ------------------------------------------------------


class TestBudgetHelpers:
    def test_render_budget_of_one_is_floored_not_unlimited(self):
        with mock.patch.dict(os.environ, {"ZABBIX_RESPONSE_BUDGET": "1"}):
            assert render_budget() == 200
        with mock.patch.dict(os.environ, {"ZABBIX_RESPONSE_BUDGET": "0"}):
            assert render_budget() == 0

    def test_nothing_omitted_is_an_empty_line(self):
        assert omitted_hosts_line([], 3) == ""

    def test_one_name_is_one_name(self):
        assert omitted_hosts_line(["srv-aq9001", "srv-aq9002", "srv-aq9003"], 5, 1) == (
            "3 of 5 hosts not shown: srv-aq9001, +2 more. "
            "Request them with hosts=srv-aq9001 or narrow the filter."
        )

    def test_overflow_keeps_the_names_before_the_count(self):
        names = [f"srv-bv{9000 + i}" for i in range(OMITTED_NAMES_MAX + 2)]
        line = omitted_hosts_line(names, 20)
        assert line.startswith(f"{len(names)} of 20 hosts not shown: srv-bv9000, srv-bv9001, ")
        assert ", srv-bv9011, +2 more. Request them with hosts=srv-bv9000," in line

    def test_unknown_names_message_keeps_every_character_of_the_names(self):
        _, note = missing_hosts_note(["srv-aq9001", "srv-aq9002-X"], [])
        assert all_names_unknown_message(note) == (
            "2 of 2 requested host(s) are not enabled hosts in Zabbix: srv-aq9001, srv-aq9002-X."
        )

    def test_an_empty_label_set_reads_none(self):
        assert _label_list(set()) == "none"
        assert _label_list({"Free"}) == "Free"

    def test_a_host_without_groups_is_still_listed(self):
        msg = no_match_message([{"host": "srv-aq9001"}])
        assert "Among 1 enabled hosts, product labels present: Unknown;" in msg


# --- budget: fit_host_blocks, swept over every budget ---------------------------------


def _footer(names, total, k=OMITTED_NAMES_MAX):
    return omitted_hosts_line(names, total, k)


def _expected(header, blocks, budget):
    """The documented contract, stated directly: greedy whole blocks, each kept
    only if it fits with the closing line naming what follows; the closing line
    names as many hosts as fit, never fewer than one."""
    n = len(blocks)
    names = [nm for nm, _ in blocks]
    everything = len(header) + sum(1 + len(t) for _, t in blocks)
    if budget <= 0 or everything <= budget:
        return "\n".join([header, *(t for _, t in blocks)]), []
    k, used = 0, len(header)
    for i, (_, t) in enumerate(blocks):
        rest = names[i + 1:]
        need = used + 1 + len(t) + (2 + len(_footer(rest, n)) if rest else 0)
        if need > budget:
            break
        k, used = k + 1, used + 1 + len(t)
    omitted = names[k:]
    body = "\n".join([header, *(t for _, t in blocks[:k])])
    shown = 1
    for m in range(OMITTED_NAMES_MAX, 0, -1):
        if len(body) + 2 + len(_footer(omitted, n, m)) <= budget:
            shown = m
            break
    return body + "\n\n" + _footer(omitted, n, shown), omitted


def _blocks(sizes, cc="aq"):
    return [(f"srv-{cc}{9000 + i}", f"srv-{cc}{9000 + i} " + "x" * s) for i, s in enumerate(sizes)]


# Blocks both shorter and much longer than the closing line (~100 chars), so
# the sweep reaches every branch: a block kept only because its closing line
# fits, a block kept with no closing line after it, and nothing kept at all.
@pytest.mark.parametrize("blocks", [
    _blocks([5, 40, 12]),
    _blocks([150, 300, 120]),
    _blocks([0, 0, 0, 0]),
    _blocks([3] * (OMITTED_NAMES_MAX + 3), cc="bv"),
    _blocks([140] * (OMITTED_NAMES_MAX + 3), cc="hm"),
    _blocks([60]),
], ids=["three-short", "three-long", "four-empty", "fifteen-short", "fifteen-long", "one-wide"])
def test_fit_host_blocks_matches_the_contract_at_every_budget(blocks):
    header = "## Trends"
    everything = len(header) + sum(1 + len(t) for _, t in blocks)
    for budget in range(-1, everything + 3):
        assert fit_host_blocks(header, blocks, budget) == _expected(header, blocks, budget), budget


# --- app_map -----------------------------------------------------------------


def _map(**over):
    data = {
        "generated_at": "2026-01-01T00:00:00Z",
        "audiences": {"default": {"sections": [
            {"name": "Free", "entries": [
                {"key": "aq_free", "members": [{"ip": "198.51.100.1"}]},
            ]},
        ]}},
    }
    data.update(over)
    return data


def _entry(**fields):
    return _map(audiences={"default": {"sections": [{"name": "Free", "entries": [{"key": "aq_free", **fields}]}]}})


def _section(section):
    return _map(audiences={"default": {"sections": [section]}})


@pytest.mark.parametrize("data, message", [
    ([], "top level must be an object"),
    (_map(audiences={}), "audiences must be a non-empty object"),
    (_map(audiences={"default": []}), "audience default: must be an object"),
    (_map(audiences={"default": {"sections": {}}}), "audience default: sections must be a list"),
    (_section([]), "audience default: section must be an object"),
    (_section({"name": "  "}), "audience default: section without a name"),
    (_section({"name": "Free", "entries": {}}), "audience default/Free: entries must be a list"),
    (_section({"name": "Free", "entries": [[]]}), "audience default/Free: entry must be an object"),
    (_entry(members={}), "audience default/Free/aq_free: members must be a list"),
    (_entry(members=[[]]), "audience default/Free/aq_free: member must be an object"),
    (_entry(members=[{"ip": 7}]), "audience default/Free/aq_free: member ip must be a string, got int"),
    (_entry(members=[{"ip": "198.51.100.1", "load": True}]),
     "audience default/Free/aq_free: member 198.51.100.1 load must be a number"),
    (_entry(members=[{"ip": "198.51.100.1", "load": "high"}]),
     "audience default/Free/aq_free: member 198.51.100.1 load must be a number"),
    (_entry(members=[{"ip": "198.51.100.1", "clients": False}]),
     "audience default/Free/aq_free: member 198.51.100.1 clients must be an integer"),
    (_entry(members=[{"ip": "198.51.100.1", "clients": "many"}]),
     "audience default/Free/aq_free: member 198.51.100.1 clients must be an integer"),
    (_entry(display_load=[1]), "audience default/Free/aq_free: display_load must be a number"),
    (_map(generated_at=None), "generated_at is missing"),
])
def test_app_map_rejections_name_the_exact_place(data, message):
    with pytest.raises(ValueError) as exc:
        parse_app_map(data)
    assert str(exc.value) == message


def test_app_map_optional_fields_default_to_empty():
    m = parse_app_map(_map(audiences={"default": {}, "beta": {"sections": [
        {"name": "Free"},
        {"name": "Paid", "entries": [{"key": "bv_paid"}]},
    ]}}))
    assert m.source == "" and m.predicate == ""
    assert m.audiences["default"].sections == ()
    free, paid = m.audiences["beta"].sections
    assert free.entries == ()
    (entry,) = paid.entries
    assert (entry.title, entry.code, entry.members, entry.without_address) == ("bv_paid", "", (), 0)


def test_generated_at_is_normalised_to_the_utc_singleton():
    for raw in ("2026-01-01T03:00:00+03:00", "2026-01-01T00:00:00z", "2026-01-01T00:00:00Z"):
        dt = parse_app_map(_map(generated_at=raw)).generated_at
        assert dt == datetime(2026, 1, 1, tzinfo=timezone.utc)
        assert dt.tzinfo is timezone.utc


def test_map_age_defaults_to_the_wall_clock():
    m = parse_app_map(_map(generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))
    before = time.time()
    age = map_age_s(m)
    assert 0 <= age <= time.time() - before + 2
    assert map_age_s(m, now=m.generated_at.timestamp() - 50) == 0.0


def test_load_reports_a_wrong_shape_verbatim():
    _, reason = load_app_map('{"audiences": {}}')
    assert reason == "wrong shape: audiences must be a non-empty object"
