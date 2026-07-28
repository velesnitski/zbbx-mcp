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
TOOLS = ROOT / "src" / "zbbx_mcp" / "tools"
EXECUTIVE = TOOLS / "executive.py"

# Literals that can only be a raw<->Mbps traffic conversion. 1e9 (bytes->GB)
# and 1000 (Mbps->Gbps, applied to already-converted values) are legitimate
# and deliberately absent.
_TRAFFIC_LITERALS = {1e6, 1_000_000, 125_000}


def iter_traffic_literals():
    """Yield (path, lineno, value) for traffic-divisor literals in tools/.

    Comments are invisible to the AST, so the historical notes explaining the
    old constants do not trip this.
    """
    for path in sorted(TOOLS.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, (int, float))
                and not isinstance(node.value, bool)
                and node.value in _TRAFFIC_LITERALS
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
        # The scanner must actually parse the tool tree.
        assert len(list(TOOLS.rglob("*.py"))) > 30

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
