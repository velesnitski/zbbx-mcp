"""Property tests on the pure core (Hypothesis).

Each property states what must hold for EVERY input, not for one example:
the budget fitter never overspends and never drops a row from a kept host,
a reading carries a value exactly when it is live, a label filter is exact,
the time and retention parsers invert their own formatting.

Generated strings are drawn from narrow alphabets, and host names are built
only from the synthetic fixture codes and the 9000 band, so no generated
value can look like a real host, address or label.
"""

from __future__ import annotations

import os
import string
from datetime import datetime, timezone
from unittest import mock

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from zbbx_mcp.budget import (
    DEFAULT_RESPONSE_BUDGET,
    OMITTED_NAMES_MAX,
    RENDER_HEADROOM,
    effective_cap,
    fit_host_blocks,
    missing_hosts_note,
    omitted_hosts_line,
    parse_host_list,
    render_budget,
    response_budget,
)
from zbbx_mcp.country import ISO2_CODES, extract_country
from zbbx_mcp.data import label_matches
from zbbx_mcp.fetch import PHYSICAL_IFACE_PREFIXES, is_physical_traffic_in_key
from zbbx_mcp.reading import LIVE_VALUE_MAX_AGE_S, NOT_REPORTING, READING_STATES, read_item
from zbbx_mcp.tools.analysis import looks_like_csv_header, split_ip_list
from zbbx_mcp.tools.dashboards import fit_lines
from zbbx_mcp.tools.problems import retention_seconds
from zbbx_mcp.utils import parse_time

SETTINGS = settings(max_examples=200, deadline=None)

FIXTURE_CODES = ("aq", "bv", "hm", "tf", "gs", "um", "pn", "sj")
NOW = 1_800_000_000  # fixed clock: properties must not depend on wall time

# --- strategies ----------------------------------------------------------------

host_names = st.builds(
    lambda cc, n: f"srv-{cc}{n}",
    st.sampled_from(FIXTURE_CODES),
    st.integers(min_value=9000, max_value=9999),
)
# Rendered rows: lowercase words, digits, spaces and table pipes only.
row_text = st.text(alphabet=string.ascii_lowercase + string.digits + " |.%", min_size=0, max_size=40)
labels = st.text(alphabet=string.ascii_letters + " ", min_size=0, max_size=12)


@st.composite
def host_blocks(draw):
    names = draw(st.lists(host_names, min_size=0, max_size=25, unique=True))
    blocks = []
    for name in names:
        rows = draw(st.lists(row_text, min_size=1, max_size=4))
        blocks.append((name, "\n".join([f"### {name}", *rows])))
    return blocks


# --- budget.fit_host_blocks ------------------------------------------------------


def _min_footer(blocks):
    """Length of the smallest closing line the fitter can fall back to."""
    if not blocks:
        return 0
    longest = max(len(n) for n, _ in blocks)
    return 2 + len(omitted_hosts_line(["x" * longest] * len(blocks), len(blocks), 1))


@SETTINGS
@given(blocks=host_blocks(), header=row_text, budget=st.integers(min_value=-5, max_value=3000))
def test_fit_host_blocks_never_exceeds_budget(blocks, header, budget):
    text, omitted = fit_host_blocks(header, blocks, budget)
    if budget <= 0:
        assert omitted == []
        return
    # A budget below header + the one-name closing line cannot be honoured by
    # construction (the closing line is never dropped); everything above it is.
    assume(budget >= len(header) + _min_footer(blocks))
    assert len(text) <= budget


@SETTINGS
@given(blocks=host_blocks(), header=row_text, budget=st.integers(min_value=-5, max_value=3000))
def test_fit_host_blocks_omits_an_exact_suffix_and_names_it(blocks, header, budget):
    text, omitted = fit_host_blocks(header, blocks, budget)
    names = [n for n, _ in blocks]
    kept = len(names) - len(omitted)
    assert omitted == names[kept:]  # whole hosts, in order, from the tail
    assert text.startswith(header)
    if omitted:
        tail = text.rsplit("\n\n", 1)[1]
        assert tail.startswith(f"{len(omitted)} of {len(names)} hosts not shown: ")
        listed = tail.split("hosts=", 1)[1].split(" or narrow", 1)[0].split(",")
        assert listed == omitted[:len(listed)]  # pasted back, cannot mis-target
        assert 1 <= len(listed) <= OMITTED_NAMES_MAX
        if len(listed) < len(omitted):
            assert f"+{len(omitted) - len(listed)} more" in tail
    else:
        assert "hosts not shown" not in text
        assert text == "\n".join([header, *(b for _, b in blocks)])


@SETTINGS
@given(blocks=host_blocks(), header=row_text, budget=st.integers(min_value=1, max_value=3000))
def test_fit_host_blocks_keeps_every_row_of_a_kept_host(blocks, header, budget):
    text, omitted = fit_host_blocks(header, blocks, budget)
    kept = blocks[:len(blocks) - len(omitted)]
    body = text.rsplit("\n\n", 1)[0] if omitted else text
    assert body == "\n".join([header, *(b for _, b in kept)])
    for name, _ in blocks[len(kept):]:
        assert f"### {name}" not in body


@SETTINGS
@given(raw=st.one_of(st.integers(min_value=-10_000, max_value=100_000).map(str),
                     st.sampled_from(["", "  ", "abc", "1e3", "12.5"])))
def test_render_budget_follows_the_server_setting(raw):
    with mock.patch.dict(os.environ, {"ZABBIX_RESPONSE_BUDGET": raw}):
        budget = response_budget()
        rendered = render_budget()
    try:
        expected = int(raw) if raw.strip() else DEFAULT_RESPONSE_BUDGET
    except ValueError:
        expected = DEFAULT_RESPONSE_BUDGET
    assert budget == expected
    if expected <= 0:
        assert rendered == 0
    else:
        assert rendered == max(expected - RENDER_HEADROOM, RENDER_HEADROOM)
        assert rendered >= RENDER_HEADROOM


@SETTINGS
@given(names=st.lists(host_names, max_size=15), seps=st.lists(st.sampled_from([",", ", ", " ,", ",,"]), max_size=15))
def test_parse_host_list_is_ordered_unique_and_stripped(names, seps):
    joined = "".join(n + (seps[i] if i < len(seps) else ",") for i, n in enumerate(names))
    parsed = parse_host_list(joined)
    assert parsed == list(dict.fromkeys(names))
    assert effective_cap(1, parsed) == max(1, len(parsed))
    missing, note = missing_hosts_note(parsed, parsed[1:])
    assert missing == sorted(set(parsed[:1]) - set(parsed[1:]))
    assert (note == "") == (not missing)


# --- reading.read_item -----------------------------------------------------------

clocks = st.one_of(
    st.integers(min_value=-5, max_value=NOW + 100),
    st.integers(min_value=NOW - LIVE_VALUE_MAX_AGE_S - 2, max_value=NOW - LIVE_VALUE_MAX_AGE_S + 2),
)
values = st.one_of(
    st.none(),
    st.floats(allow_nan=False, allow_infinity=False, width=32).map(str),
    st.integers(min_value=-10**6, max_value=10**6).map(str),
    st.text(alphabet=string.ascii_lowercase, min_size=1, max_size=5),
)


@SETTINGS
@given(clock=clocks, value=values, clock_as=st.sampled_from([str, int]))
def test_read_item_value_iff_live(clock, value, clock_as):
    item = {"lastclock": clock_as(clock)}
    if value is not None:
        item["lastvalue"] = value
    r = read_item(item, now=NOW)
    assert r.state in READING_STATES
    assert (r.value is not None) == (r.state == "live") == r.is_live
    if clock <= 0:
        assert r.state == "never" and r.age_s is None
    elif NOW - clock > LIVE_VALUE_MAX_AGE_S:
        assert r.state == "stale" and r.age_s == NOW - clock
    else:
        try:
            parsed = float(value) if value is not None else None
        except ValueError:
            parsed = None
        assert r.age_s == NOW - clock
        if parsed is None:
            assert r.state == "unparsable"
        else:
            assert r.state == "live" and r.value == parsed
            assert r.text("%") == f"{parsed:.1f} %"
    if not r.is_live:
        assert r.text("%") == NOT_REPORTING


@SETTINGS
@given(offset=st.integers(min_value=-3, max_value=3))
def test_read_item_live_boundary_is_inclusive(offset):
    age = LIVE_VALUE_MAX_AGE_S + offset
    r = read_item({"lastclock": str(NOW - age), "lastvalue": "7"}, now=NOW)
    assert r.state == ("live" if age <= LIVE_VALUE_MAX_AGE_S else "stale")
    assert r.age_s == age


@SETTINGS
@given(raw=st.one_of(st.none(), st.just(""), st.text(alphabet="xyz.-", min_size=1, max_size=4),
                     st.sampled_from([[], {}, "1.5", " "])))
def test_read_item_without_a_readable_clock_fails_closed(raw):
    item = {"lastvalue": "42"}
    if raw is not None:
        item["lastclock"] = raw
    try:
        int(raw)  # type: ignore[arg-type]
        assume(False)  # a readable clock is covered above
    except (TypeError, ValueError):
        pass
    r = read_item(item, now=NOW)
    assert (r.value, r.age_s, r.state) == (None, None, "missing_clock")


# --- data.label_matches ----------------------------------------------------------


@SETTINGS
@given(actual=st.one_of(st.none(), labels), wanted=st.one_of(st.none(), labels))
def test_label_matches_is_exact_case_insensitive_and_empty_matches_all(actual, wanted):
    result = label_matches(actual, wanted)
    if not wanted:
        assert result is True
    else:
        assert result == ((actual or "").strip().lower() == wanted.strip().lower())
        assert label_matches(wanted.upper(), wanted.lower()) is True
    # A prefix is not a match: "Free" must not select "Free Plus".
    if wanted and wanted.strip():
        assert label_matches(wanted + " Plus", wanted) is False


# --- country.extract_country -----------------------------------------------------


@SETTINGS
@given(cc=st.sampled_from(FIXTURE_CODES), n=st.integers(min_value=9000, max_value=9999),
       upper=st.booleans(), suffix=st.sampled_from(["", "-lite", "-x", "_b"]))
def test_extract_country_reads_the_indexed_code(cc, n, upper, suffix):
    code = cc.upper() if upper else cc
    assert extract_country(f"srv-{code}{n}{suffix}") == cc.upper()


@SETTINGS
@given(name=st.text(alphabet="abghmnpqstuvz019-_", max_size=20))
def test_extract_country_returns_only_iso_codes(name):
    cc = extract_country(name)
    assert cc == "" or cc in ISO2_CODES
    if "-" not in name and "_" not in name:
        assert cc == ""  # a code is only ever read after a separator


# --- fetch.is_physical_traffic_in_key --------------------------------------------


@SETTINGS
@given(prefix=st.sampled_from(PHYSICAL_IFACE_PREFIXES + ("docker", "veth", "lo", "tun", "br")),
       tail=st.text(alphabet=string.ascii_lowercase + string.digits, max_size=5),
       quote=st.sampled_from(['"', "'"]))
def test_traffic_key_quoted_equals_unquoted(prefix, tail, quote):
    iface = prefix + tail
    plain = is_physical_traffic_in_key(f"net.if.in[{iface}]")
    assert is_physical_traffic_in_key(f"net.if.in[{quote}{iface}{quote}]") == plain
    assert plain == iface.startswith(PHYSICAL_IFACE_PREFIXES)
    assert is_physical_traffic_in_key(f"net.if.out[{iface}]") is False


# --- utils.parse_time ------------------------------------------------------------


@SETTINGS
@given(epoch=st.integers(min_value=0, max_value=4_000_000_000))
def test_parse_time_epoch_round_trips(epoch):
    assert parse_time(str(epoch)) == epoch
    assert parse_time(epoch) == epoch
    assert parse_time(float(epoch)) == epoch


@SETTINGS
@given(dt=st.datetimes(min_value=datetime(1971, 1, 1), max_value=datetime(2100, 1, 1)),
       sep=st.sampled_from(["T", " "]))
def test_parse_time_iso_round_trips_as_utc(dt, sep):
    dt = dt.replace(microsecond=0)
    expected = int(dt.replace(tzinfo=timezone.utc).timestamp())
    assert parse_time(dt.isoformat(sep=sep)) == expected
    aware = dt.replace(tzinfo=timezone.utc)
    assert parse_time(aware.isoformat()) == expected


@SETTINGS
@given(qty=st.integers(min_value=0, max_value=10_000), unit=st.sampled_from("smhdwSMHDW"),
       pad=st.sampled_from(["", " "]))
def test_parse_time_relative_subtracts_from_now(qty, unit, pad):
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit.lower()]
    assert parse_time(f"{pad}{qty}{pad}{unit}{pad}", now=NOW) == NOW - qty * mult


# --- problems.retention_seconds --------------------------------------------------


@SETTINGS
@given(n=st.integers(min_value=0, max_value=100_000), unit=st.sampled_from("smhdwSMHDW"),
       pad=st.sampled_from(["", " "]))
def test_retention_seconds_reads_every_unit(n, unit, pad):
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit.lower()]
    assert retention_seconds(f"{pad}{n}{unit}{pad}") == n * mult
    assert retention_seconds(str(n)) == n
    assert retention_seconds(n) == n


@SETTINGS
@given(junk=st.text(alphabet="xyz-.+ ", max_size=5), unit=st.sampled_from("smhdwq"))
def test_retention_seconds_unreadable_is_none_not_forever(junk, unit):
    assume(not junk.strip() or not junk.strip().isdigit())
    assert retention_seconds(junk + unit if junk.strip() else junk) is None


# --- analysis.split_ip_list / looks_like_csv_header ------------------------------

doc_ips = st.builds(
    lambda net, host: f"{net}.{host}",
    st.sampled_from(["192.0.2", "198.51.100", "203.0.113"]),
    st.integers(min_value=1, max_value=254),
)


@SETTINGS
@given(ips=st.lists(doc_ips, max_size=12),
       seps=st.lists(st.sampled_from([",", ";", " ", "\n", "\t", ", ", " ; "]), min_size=12, max_size=12))
def test_split_ip_list_recovers_tokens(ips, seps):
    text = "".join(ip + seps[i] for i, ip in enumerate(ips))
    assert split_ip_list(text) == ips
    assert split_ip_list(seps[0] + text) == ips


@SETTINGS
@given(words=st.lists(st.text(alphabet=string.ascii_lowercase + "_", min_size=1, max_size=8), min_size=1, max_size=6),
       ip=doc_ips, at=st.integers(min_value=0, max_value=6))
def test_looks_like_csv_header(words, ip, at):
    line = ",".join(words)
    assert looks_like_csv_header(line) == ("," in line)
    with_ip = words[:at] + [ip] + words[at:]
    assert looks_like_csv_header(",".join(with_ip)) is False
    assert looks_like_csv_header(" ".join(words)) is False


# --- dashboards.fit_lines --------------------------------------------------------


@SETTINGS
@given(lines=st.lists(row_text, max_size=30), used=st.integers(min_value=0, max_value=500),
       budget=st.integers(min_value=-5, max_value=2000))
def test_fit_lines_keeps_a_prefix_and_counts_the_rest(lines, used, budget):
    out = fit_lines(lines, used, budget)
    if budget <= 0 or not lines:
        assert out == lines
        return
    if out == lines:
        return
    *kept, closing = out
    assert kept == lines[:len(kept)]
    assert closing.startswith(f"- … and {len(lines) - len(kept)} more host(s)")
    # Room is reserved for the closing line's template ("{n}"), which is never
    # shorter than the rendered line while fewer than 1000 rows are cut.
    template = closing.replace(str(len(lines) - len(kept)), "{n}", 1)
    room = budget - used - len(template) - 2
    size = sum(len(ln) + 1 for ln in kept)
    assert size <= max(room, 0)
    if room >= 0:
        assert used + size + len(closing) + 2 <= budget  # the whole answer fits
    # The first dropped line really did not fit.
    assert size + len(lines[len(kept)]) + 1 > room
