"""Host-shaped example names must be on an allow-list (ADR 121).

**Why an allow-list rather than a pattern.**

The obvious guard is a rule: "example hostnames may carry at most N digits", or
"numbers in a reserved band are synthetic". Every such rule was tried and every
one is unsound, because operational numbering occupies the *same shape space* as
legitimate examples. There is no syntactic property that separates a real
machine name from an invented one — the distinguishing fact is not in the
string, it is whether the name exists somewhere else.

What does separate them is knowledge, and knowledge cannot live in a regex. So
this guard inverts the question. Instead of asking *"does this look
forbidden?"* — an infinite set nobody can enumerate — it asks *"is this one of
the names we have agreed to use?"*, which is finite and written down below.

The cost is a deliberate edit: adding an example host name means adding it
here. That edit **is** the control. It is the moment to check the name against
the live system and confirm it resolves to nothing, which is the only check
that actually works.

A token is host-shaped when it ends in a two-letter country code followed by
digits, optionally preceded by hyphenated segments.

Everything in this file is invented. Illustrating the rule with real values
would defeat the rule.
"""

from __future__ import annotations

import pathlib
import re
import subprocess

import pytest

from zbbx_mcp.country import _COUNTRY_ALIASES, CAPITAL_COORDS

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Deliberate host examples. Every entry was checked against the live system and
# resolves to nothing. Adding one means doing that check.
ALLOWED_HOSTS = {
    # Sanctioned two-digit generics — AGENTS.md and REVIEW.md prescribe these.
    "srv-nl01", "srv-nl02", "srv-nl03", "srv-us01", "srv-us02", "srv-us03",
    "srv-de01", "srv-de02", "srv-de3", "srv-tr03", "us02",
    "edge-de01", "edge-de1", "edge-id01", "edge-mx01",
    "edge-us01", "edge-us03", "edge-us04",
    # Reserved synthetic numbering used where a test needs 3-4 digit widths.
    "srv-ar099", "srv-br0101", "srv-mx0101", "srv-nl0999", "srv-us0999",
    "srv-uk0997", "srv-uk0998", "srv-us901", "srv-us905", "us903", "us907",
    # Cross-country parsing cases: the country code deliberately disagrees with
    # a leading segment, proving extract_country reads the right one.
    "host-fj-ki01", "host-ki-fj01", "host-zq-fj1", "node-xy-fj1",
    "node-eu-br3",
    # Generic node fixtures.
    "node-fj1", "node-fj2", "node-ki1", "node-ki2", "node-ki3", "node-ki4",
    "node-ki5",
}

# Tokens that merely collide with the pattern — a two-letter sequence that
# happens to be an ISO code, followed by digits. Not host names at all.
NOT_HOSTNAMES = {
    "ws1", "ws2", "ws3",        # openpyxl worksheet variables
    "bb2", "br3", "ps1",        # spreadsheet cells / shell prompt
    "fj1",                      # bare fixture id
    "ec4899",                   # hex colour
    "g6cj-pr64",                # GHSA advisory fragment
    "py310",                    # Python version tag
}

ALLOWED = ALLOWED_HOSTS | NOT_HOSTNAMES

# ISO codes PLUS the non-ISO aliases this codebase already recognises. At
# least one code in common use is not an ISO code, so a guard built on ISO
# alone silently ignores every name using it. Sourced from country.py so the
# two cannot drift apart.
_CC = ({c.lower() for c in CAPITAL_COORDS if len(c) == 2}
       | {c.lower() for c in _COUNTRY_ALIASES if len(c) == 2})
_HOST_RE = re.compile(r"\b((?:[a-z][a-z0-9]*-)*)([a-z]{2})(\d{1,6})\b")

# The generated provider table is public routing data, not example hostnames.
_SKIP = {"src/zbbx_mcp/data/provider_cidrs.json"}
_SELF = pathlib.Path(__file__).name


def _scan(text: str) -> set[str]:
    return {m.group(0) for m in _HOST_RE.finditer(text) if m.group(2) in _CC}


def _tracked() -> list[pathlib.Path]:
    out = subprocess.run(["git", "ls-files"], capture_output=True, text=True,
                         cwd=ROOT).stdout.split()
    keep = []
    for rel in out:
        if rel in _SKIP or rel.endswith(_SELF):
            continue
        p = ROOT / rel
        if p.suffix in (".py", ".md", ".yml", ".yaml", ".toml", ".txt") and p.is_file():
            keep.append(p)
    return keep


def test_every_host_shaped_token_is_allow_listed():
    violations = []
    for path in _tracked():
        try:
            lines = path.read_text().splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for n, line in enumerate(lines, 1):
            for tok in _scan(line) - ALLOWED:
                violations.append(
                    f"{path.relative_to(ROOT)}:{n} — {tok!r} is host-shaped and "
                    "not allow-listed. If it is an example you invented, verify "
                    "it resolves to nothing in the live system, then add it to "
                    "ALLOWED_HOSTS in tests/test_hostname_guard.py. If it came "
                    "from real infrastructure it must not be here."
                )
    assert not violations, "\n".join(violations)


def test_the_guard_would_actually_reject_a_real_looking_name():
    """A guard whose allow-list swallows everything is not a guard.

    Invented names in the shapes that matter: wide numbering, a multi-segment
    prefix, and a compound parent/child pair. Deliberately not real values —
    a guard illustrated with real values would leak what it exists to stop.
    """
    for sample in ("srv-nl4242", "alpha-beta-us7777", "gamma-de5150",
                   "delta-epsilon-uk3131", "edge-nl4242 us7777"):
        assert _scan(sample) - ALLOWED, f"{sample!r} should have been rejected"


def test_the_guard_accepts_the_sanctioned_generics():
    for sample in ("srv-nl01", "srv-us01", "edge-de01"):
        assert not (_scan(sample) - ALLOWED), f"{sample!r} should be accepted"


def test_the_allow_list_has_no_dead_entries():
    """An allow-list that outlives its entries stops being reviewable.

    A name nobody uses any more is a name nobody re-checks, and it quietly
    widens the exemption. Dead entries must be deleted, not carried.
    """
    seen: set[str] = set()
    for path in _tracked():
        try:
            seen |= _scan(path.read_text())
        except (OSError, UnicodeDecodeError):
            continue
    dead = sorted(ALLOWED - seen)
    assert not dead, (
        "allow-listed but no longer present anywhere — remove from "
        f"tests/test_hostname_guard.py: {dead}")


def test_country_dataset_is_actually_loaded():
    # If CAPITAL_COORDS were empty the regex would match nothing and every
    # test above would pass vacuously.
    assert len(_CC) > 50
    assert {"nl", "us", "de"} <= _CC
    # The non-ISO alias the first version of this guard missed entirely.
    assert _CC - {c.lower() for c in CAPITAL_COORDS}


@pytest.mark.parametrize("shape", ["srv-nl01", "nl4242", "edge-us7777", "node-ki3"])
def test_the_shape_matcher_recognises_host_forms(shape):
    assert _scan(shape), f"{shape!r} should be recognised as host-shaped"
