"""Fixture data must be synthetic (ADR 119).

A fixture that carries an address from some real network is a portability bug
waiting to happen: it can collide with a host the test runner can actually
reach, it makes the test depend on where it runs, and it dates the moment that
network is renumbered. Addresses in tests should be *obviously* invented.

Two layers:

1. **Structural, always on.** Addresses must come from the private or
   documentation ranges. A bright line beats a judgement call — "is this
   address real?" invites an argument, "is it from RFC 5737?" does not.

2. **Configurable, per deployment.** Identifiers that are specific to whoever
   runs this server cannot be enumerated in the package itself, so the terms
   come from an environment variable and are enforced whenever it is set.
"""

from __future__ import annotations

import ipaddress
import os
import pathlib
import re

import pytest

from tests.test_guards import TestFleetDataGuard

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Self-exemption is deliberately PARTIAL.
#
# The magnitude layer must skip the guards' own files: each carries a
# deliberately invalid sample — a fake fleet magnitude — because that is how it
# proves it is not vacuous, and scanning them would make every guard fail on
# its own evidence.
#
# The ADDRESS layer does not get that exemption. A blanket skip would make the
# file defining the address rule the one place any address passes unchecked —
# precisely the wrong property for that file. Its own boundary probes are
# non-global or listed in `_doc_placeholders`, so nothing routable can hide
# here either.
_MAGNITUDE_SELF_EXEMPT = {"test_guards.py", "test_fixture_data_guard.py"}
TESTS = [p for p in sorted((ROOT / "tests").glob("*.py"))
         if p.name not in _MAGNITUDE_SELF_EXEMPT]

# Every tracked test file, including this one — used by the address layer.
ALL_TESTS = sorted((ROOT / "tests").glob("*.py"))

# RFC 1918, loopback, link-local, "this network", and the RFC 5737
# documentation ranges.
_ALLOWED_NETS = [
    ipaddress.ip_network(n) for n in (
        "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
        "127.0.0.0/8", "169.254.0.0/16", "0.0.0.0/8",
        "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24",
        # Multicast, reserved and broadcast. These can never be a host's
        # address, and fixtures legitimately use them as invalid-input
        # samples and as netmasks.
        "224.0.0.0/4", "240.0.0.0/4",
    )
]
_IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")

# A guard that scans nothing reads exactly like a guard that found nothing.
# These floors refuse that: if the file list ever breaks — a bad glob, a
# pathspec the shell mangles, a rename — the guard fails loudly instead of
# passing on an empty set. Set well below the real counts so ordinary growth
# and pruning never trip them; they exist to catch zero, not to track size.
_MIN_TRACKED_FILES = 100
_MIN_TEST_FILES = 20


def _assert_scanned(n: int, floor: int, what: str) -> None:
    assert n >= floor, (
        f"{what} guard scanned only {n} file(s), expected at least {floor} — "
        "the file list is broken, so a pass here would mean nothing")


def _is_allowed(text: str) -> bool:
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        return True                      # not an address at all
    return any(addr in net for net in _ALLOWED_NETS)


def test_fixture_addresses_come_from_documentation_ranges():
    violations = []
    for path in TESTS:
        for n, line in enumerate(path.read_text().splitlines(), 1):
            for tok in _IP_RE.findall(line):
                if not _is_allowed(tok):
                    violations.append(
                        f"{path.name}:{n} — {tok!r} sits outside the private and "
                        "documentation ranges. Use 192.0.2.x / 198.51.100.x / "
                        "203.0.113.x (RFC 5737) so a real address can never be "
                        "mistaken for scaffolding"
                    )
    _assert_scanned(len(TESTS), _MIN_TEST_FILES, "fixture address")
    assert not violations, "\n".join(violations)


def test_the_address_rule_would_actually_reject_something():
    # A rule whose allow-list swallows everything is not a rule.
    assert _is_allowed("192.0.2.7")
    assert _is_allowed("10.1.2.3")
    assert _is_allowed("127.0.0.1")
    assert not _is_allowed("172.15.0.1")     # just outside RFC 1918
    assert not _is_allowed("203.0.114.9")    # just outside TEST-NET-3


def test_fixtures_carry_no_fleet_magnitudes():
    # TestFleetDataGuard applies the same rule to docs. A comment in a test
    # file quotes scale exactly as a doc does, and `tests/` was never in that
    # guard's scope.
    violations = []
    for path in TESTS:
        for n, line in enumerate(path.read_text().splitlines(), 1):
            for rx, why in TestFleetDataGuard.PATTERNS:
                if rx.search(line):
                    violations.append(
                        f"{path.name}:{n} — {why}: {line.strip()[:70]}")
    _assert_scanned(len(TESTS), _MIN_TEST_FILES, "fixture magnitude")
    assert not violations, "\n".join(violations)


def test_no_infrastructure_addresses_anywhere_in_the_repo():
    """The whole repo, not just fixtures.

    Three things are legitimately allowed to be a routable address here:

    1. Non-global space — private, loopback, link-local, multicast, reserved,
       and the RFC 5737 documentation ranges. None can be a server of ours.
    2. A **network address declared by the allocation tables**. Provider
       detection is a feature of this server, and those tables are published
       RIR/provider allocations. The check is deliberately exact: the address
       must be the network address of a block the tables declare, so a host
       address cannot hide among them by sitting inside one.
    3. A tiny set of documentation placeholders used in docstrings to show the
       accepted CIDR syntax.

    Anything else fails. An invented address always matches one of the three;
    an address carried in from somewhere else matches none.
    """
    import subprocess

    from zbbx_mcp.classify import DATACENTER_CIDRS, PROVIDER_CIDRS

    declared: set[str] = set()
    for cidrs in PROVIDER_CIDRS.values():
        declared |= {str(ipaddress.ip_network(c, strict=False).network_address)
                     for c in cidrs}
    for entries in DATACENTER_CIDRS.values():
        declared |= {str(ipaddress.ip_network(c, strict=False).network_address)
                     for c, _ in entries}

    # Classic dummies used in docstrings/ADRs to show CIDR syntax, plus this
    # guard's own boundary probes. Kept explicit and tiny so the exemption
    # cannot quietly grow.
    #
    # The probes are addresses that must be REJECTED for the rule to be worth
    # anything: one just outside RFC 1918, one just outside TEST-NET-3. They
    # are listed here rather than by exempting this whole file, because a
    # blanket skip would make the file defining the address rule the one place
    # any address passes unchecked. Both sit one address outside an allowed
    # range, so neither can be a real host.
    doc_placeholders = {"1.2.3.4", "1.2.3.0", "1.2.0.0",
                        "172.15.0.1", "203.0.114.9"}

    tracked = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, cwd=ROOT
    ).stdout.split()
    violations = []
    scanned = 0
    for rel in tracked:
        path = ROOT / rel
        # No self-exemption here, deliberately: see the note at the top. This
        # file is subject to the address rule like every other.
        if not path.is_file():
            continue
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        scanned += 1
        for n, line in enumerate(text.splitlines(), 1):
            for tok in _IP_RE.findall(line):
                if _is_allowed(tok) or tok in declared or tok in doc_placeholders:
                    continue
                violations.append(
                    f"{rel}:{n} — {tok!r} is a routable address that is neither "
                    "documentation space nor a declared allocation-table "
                    "network. Fixture addresses must be from RFC 5737"
                )
    _assert_scanned(scanned, _MIN_TRACKED_FILES, "repo-wide address")
    assert not violations, "\n".join(violations)


def test_the_repo_wide_rule_is_not_vacuous():
    # An address inside a declared provider block, but with a host part, must
    # still fail — otherwise "somewhere in a known range" would be a loophole
    # wide enough for any real server.
    from zbbx_mcp.classify import PROVIDER_CIDRS
    net = ipaddress.ip_network(next(iter(PROVIDER_CIDRS.values()))[0], strict=False)
    host = str(net.network_address + 7)
    assert not _is_allowed(host)
    assert host != str(net.network_address)


def _deny_terms() -> list[str]:
    raw = os.environ.get("ZBBX_SENSITIVE_STRINGS", "").strip()
    if not raw:
        return []
    candidate = pathlib.Path(raw)
    lines = candidate.read_text().splitlines() if candidate.is_file() else raw.split(",")
    return [t.strip() for t in lines if t.strip() and not t.strip().startswith("#")]


def test_deployment_deny_list_is_enforced_when_configured():
    """Layer two, for identifiers the package itself cannot enumerate.

    Skips *loudly* when unconfigured rather than passing silently — a guard
    that quietly does nothing is worse than no guard, because it reads green.
    """
    terms = _deny_terms()
    if not terms:
        pytest.skip(
            "ZBBX_SENSITIVE_STRINGS not set — the deployment-specific deny-list "
            "is NOT enforced in this run. Set it to a file path or an inline "
            "comma-separated list to enable."
        )
    violations = set()
    for path in TESTS:
        haystack = path.read_text().lower()
        for term in terms:
            if term.lower() in haystack:
                # The term is deliberately not echoed — CI output is not a
                # place to repeat a configured term back.
                violations.add(f"{path.name} — contains a denied term")
    assert not violations, "\n".join(sorted(violations))


def test_deny_list_parsing_handles_both_forms(monkeypatch, tmp_path):
    monkeypatch.setenv("ZBBX_SENSITIVE_STRINGS", "alpha, beta ,# note,")
    assert _deny_terms() == ["alpha", "beta"]
    f = tmp_path / "terms.txt"
    f.write_text("# comment\ngamma\n\n delta \n")
    monkeypatch.setenv("ZBBX_SENSITIVE_STRINGS", str(f))
    assert _deny_terms() == ["gamma", "delta"]
    monkeypatch.delenv("ZBBX_SENSITIVE_STRINGS")
    assert _deny_terms() == []


def test_the_coverage_floors_actually_fire():
    """A guard that scans nothing reads exactly like one that found nothing.

    Every silent-verifier failure this repo has hit looked like a pass: a
    pathspec the shell mangled, a dry-run pattern narrower than the real one, a
    cross-check comparing zero fields. The floors turn "scanned nothing" from a
    green result into a red one, so they have to be shown to fire.
    """
    with pytest.raises(AssertionError, match="scanned only 0"):
        _assert_scanned(0, _MIN_TEST_FILES, "probe")
    with pytest.raises(AssertionError, match="would mean nothing"):
        _assert_scanned(_MIN_TRACKED_FILES - 1, _MIN_TRACKED_FILES, "probe")
    _assert_scanned(_MIN_TRACKED_FILES, _MIN_TRACKED_FILES, "probe")   # boundary


def test_this_file_is_subject_to_the_address_rule():
    """The self-exemption is partial, and that must stay true.

    A blanket skip would make the file defining the address rule the one place
    any address passes unchecked. If someone re-adds this file to an exemption
    set, this fails.
    """
    me = pathlib.Path(__file__).name
    # The magnitude layer skips this file — it must, to carry invalid samples.
    assert me in _MAGNITUDE_SELF_EXEMPT
    assert not any(p.name == me for p in TESTS)
    # The address layer does not: this file is in the list it walks, and its
    # own boundary probes are named explicitly rather than waved through.
    assert any(p.name == me for p in ALL_TESTS)
