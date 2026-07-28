"""Traffic-unit conversion tests (ADR 087).

Two conflicting notions of bytes->Mbps had drifted apart:
- get_peak_analysis hardcoded `*8/1e6`, so on the default bits/s config it
  reported 8x the true Mbps (and disagreed with every other tool);
- the bytes-mode divisor was 8_000_000, but bytes/s->Mbps is /125_000, so it
  was 64x too low.
Both now go through the single shared TRAFFIC_DIVISOR.
"""

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "zbbx_mcp"
TOOLS = SRC / "tools"
EXECUTIVE = TOOLS / "executive.py"

# Literals that can only be a raw<->Mbps traffic conversion. 1e9 (bytes->GB)
# and 1000 (Mbps->Gbps, applied to already-converted values) are legitimate
# and deliberately absent.
# `1e6` alone covers the int form too: 1_000_000 == 1e6, so set membership
# matches both spellings (asserted below so this is not taken on faith).
_TRAFFIC_LITERALS = {1e6, 125_000}


def _named_constant_literals(tree):
    """Constant nodes that ARE the definition of a named constant.

    ``MB_DECIMAL = 1_000_000`` and ``_TRAFFIC_DIVISOR = 125_000 if ... else
    1_000_000`` are the canonical definitions — naming the value is exactly
    the fix this guard exists to enforce, so the definitions must not be
    reported as violations of it.
    """
    exempt = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(t, ast.Name) and t.id.lstrip("_").isupper()
            for t in node.targets
        ):
            continue
        for sub in ast.walk(node.value):
            if isinstance(sub, ast.Constant):
                exempt.add(id(sub))
    return exempt


def iter_traffic_literals():
    """Yield (path, lineno, value) for traffic-divisor literals in src/.

    Comments are invisible to the AST, so the historical notes explaining the
    old constants do not trip this.
    """
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        exempt = _named_constant_literals(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, (int, float))
                and not isinstance(node.value, bool)
                and node.value in _TRAFFIC_LITERALS
                and id(node) not in exempt
            ):
                yield path, node.lineno, node.value


class TestNoHardcodedTrafficDivisor:
    """ADR 098 — every raw<->Mbps conversion goes through the shared helpers.

    ADR 087 established one divisor, but ~24 sites across 9 modules kept
    dividing by a literal `1e6`. That is correct only for the default bits/s
    config; under a bytes/s deployment each reads 8x low. Ratios survive
    (both sides share the error) but absolute floors do not — a host at 30
    real Mbps reads 3.75 and falls under a 5.0 Mbps baseline gate, dropping
    out of the analysis with no trace. This guard pins the whole tree, not
    just the one module ADR 087 happened to fix.
    """

    def test_no_traffic_literal_in_tools(self):
        violations = [
            f"{p.relative_to(ROOT)}:{ln} — literal {v!r} is a traffic "
            "conversion; use to_mbps()/to_kbps()/from_mbps() from "
            "zbbx_mcp.fetch so ZABBIX_TRAFFIC_UNIT is honoured (ADR 098)"
            for p, ln, v in iter_traffic_literals()
        ]
        assert not violations, "\n".join(violations)

    def test_guard_is_not_vacuous(self):
        # Must cover the WHOLE package, not just tools/ — the shared helpers
        # in data/uptime/anomaly/fetch convert traffic too (ADR 100).
        scanned = list(SRC.rglob("*.py"))
        assert len(scanned) > 40
        assert any(p.name == "fetch.py" for p in scanned)
        assert any(p.parent.name == "tools" for p in scanned)

    def test_literal_set_matches_both_spellings(self):
        # The guard stores 1e6 only; both spellings must still be caught.
        assert 1_000_000 in _TRAFFIC_LITERALS
        assert 1e6 in _TRAFFIC_LITERALS
        assert 125_000 in _TRAFFIC_LITERALS
        assert 1_000_000_000 not in _TRAFFIC_LITERALS   # bytes->GB is legitimate
        assert 1000 not in _TRAFFIC_LITERALS            # Mbps->Gbps is legitimate

    def test_helpers_honour_the_unit_setting(self):
        from zbbx_mcp.fetch import from_mbps, to_kbps, to_mbps
        assert to_mbps(100_000_000) == 100.0        # bits/s default
        assert to_kbps(1_000_000) == 1000.0
        assert from_mbps(to_mbps(42_000_000)) == 42_000_000
        # Robust to the junk Zabbix actually returns.
        assert to_mbps(None) == 0.0
        assert to_mbps("") == 0.0
        assert to_mbps("5000000") == 5.0


class TestTrafficDivisor:
    def test_default_is_bits_per_sec(self):
        # Default config is bits/s -> Mbps is /1e6. 100 Mbps of bits reads 100.
        from zbbx_mcp.fetch import TRAFFIC_DIVISOR
        assert TRAFFIC_DIVISOR == 1_000_000
        assert 100_000_000 / TRAFFIC_DIVISOR == 100.0

    def test_bytes_divisor_math_is_125k(self):
        # bytes/s -> Mbps: x*8/1e6 == x/125_000. The source encodes 125_000
        # for bytes mode; verify that is the arithmetically correct divisor.
        assert 1_000_000 / 8 == 125_000
        # 12.5 MB/s == 100 Mbps
        assert 12_500_000 / 125_000 == 100.0


class TestPeakAnalysisUsesSharedDivisor:
    def test_no_hardcoded_times_eight_conversion(self):
        # Regression lock: get_peak_analysis must not reintroduce `*8/1e6`.
        src = EXECUTIVE.read_text()
        assert "* 8 / 1_000_000" not in src
        assert "* 8 / 1000000" not in src

    def test_routes_through_traffic_divisor(self):
        src = EXECUTIVE.read_text()
        assert "TRAFFIC_DIVISOR" in src
