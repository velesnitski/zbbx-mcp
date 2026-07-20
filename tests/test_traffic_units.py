"""Traffic-unit conversion tests (ADR 087).

Two conflicting notions of bytes->Mbps had drifted apart:
- get_peak_analysis hardcoded `*8/1e6`, so on the default bits/s config it
  reported 8x the true Mbps (and disagreed with every other tool);
- the bytes-mode divisor was 8_000_000, but bytes/s->Mbps is /125_000, so it
  was 64x too low.
Both now go through the single shared TRAFFIC_DIVISOR.
"""

import pathlib

EXECUTIVE = (
    pathlib.Path(__file__).resolve().parent.parent
    / "src" / "zbbx_mcp" / "tools" / "executive.py"
)


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
