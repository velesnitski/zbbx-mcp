"""Test fixtures are published data too (ADR 119).

Fixture data must be synthetic. The recurring mistake is not
carelessness in prose — it is *carrying data in from elsewhere*: a hostname, an address
or a check key gets pasted out of an investigation into a fixture, where it
looks like scaffolding and reads like infrastructure. It has happened more than
once, and the existing fleet-data guard never covered `tests/` at all.

Two layers, because the two halves are not equally expressible in a public repo:

1. **Structural, always on.** Addresses must come from the private or
   documentation ranges. A bright line beats a judgement call — "is this IP
   real?" invites an argument, "is it from RFC 5737?" does not, and an address
   carried in from elsewhere fails instantly either way.

2. **Deployment-specific, configured outside the repo.** Real hostnames,
   product names and protocol names cannot be listed in a public guard: the
   package cannot enumerate them. They live in an environment variable and are
   enforced whenever it is present.
"""

from __future__ import annotations

import ipaddress
import os
import pathlib
import re

import pytest

from tests.test_guards import TestFleetDataGuard

ROOT = pathlib.Path(__file__).resolve().parent.parent

# The guards' own files are exempt, and must be. Both carry deliberately
# invalid samples — a fake fleet magnitude, an address that has to be
# rejected — because that is how each proves it is not vacuous. Scanning them
# would make every guard fail on its own evidence.
_SELF_EXEMPT = {"test_guards.py", "test_fixture_data_guard.py"}
TESTS = [p for p in sorted((ROOT / "tests").glob("*.py"))
         if p.name not in _SELF_EXEMPT]

# RFC 1918, loopback, link-local, "this network", and the RFC 5737
# documentation ranges.
_ALLOWED_NETS = [
    ipaddress.ip_network(n) for n in (
        "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
        "127.0.0.0/8", "169.254.0.0/16", "0.0.0.0/8",
        "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24",
        # Multicast, reserved and broadcast. These can never be a host's
        # address, so they cannot be a leak — and fixtures legitimately use
        # them as invalid-input samples and as netmasks.
        "224.0.0.0/4", "240.0.0.0/4",
    )
]
_IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")


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
    assert not violations, "\n".join(violations)


def test_the_address_rule_would_actually_reject_something():
    # A rule whose allow-list swallows everything is not a rule.
    assert _is_allowed("192.0.2.7")
    assert _is_allowed("10.1.2.3")
    assert _is_allowed("127.0.0.1")
    assert not _is_allowed("172.15.0.1")     # just outside RFC 1918
    assert not _is_allowed("203.0.114.9")    # just outside TEST-NET-3


def test_fixtures_carry_no_fleet_magnitudes():
    # The class TestFleetDataGuard covers for docs. A comment quoting an
    # estate's scale leaks identically from a test file, and `tests/` was
    # never in that guard's scope.
    violations = []
    for path in TESTS:
        for n, line in enumerate(path.read_text().splitlines(), 1):
            for rx, why in TestFleetDataGuard.PATTERNS:
                if rx.search(line):
                    violations.append(
                        f"{path.name}:{n} — {why}: {line.strip()[:70]}")
    assert not violations, "\n".join(violations)


def test_no_infrastructure_addresses_anywhere_in_the_repo():
    """The whole repo, not just fixtures — an audit answers today, a guard
    answers every day after.

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

    Anything else fails. That is the point: an address copied out of live
    output matches none of the three.
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

    # Classic dummies used in docstrings/ADRs to show CIDR syntax. Kept
    # explicit and tiny so the exemption cannot quietly grow.
    doc_placeholders = {"1.2.3.4", "1.2.3.0", "1.2.0.0"}

    tracked = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, cwd=ROOT
    ).stdout.split()
    violations = []
    for rel in tracked:
        path = ROOT / rel
        if not path.is_file() or path.name == pathlib.Path(__file__).name:
            continue
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for tok in _IP_RE.findall(line):
                if _is_allowed(tok) or tok in declared or tok in doc_placeholders:
                    continue
                violations.append(
                    f"{rel}:{n} — {tok!r} is a routable address that is neither "
                    "documentation space nor a declared allocation-table "
                    "network. If it came from live output it must not be here"
                )
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
    """Layer two, for the strings a public guard cannot name.

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
                # The term is deliberately not echoed: this output can land in
                # a public CI log, which would republish exactly what the
                # guard exists to keep out.
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
