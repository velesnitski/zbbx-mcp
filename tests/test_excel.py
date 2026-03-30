import zbbx_mcp.excel as _excel
from zbbx_mcp.excel import (
    BW_RED,
    bandwidth_fill,
    classify_bandwidth,
    cpu_fill,
)


class TestClassifyBandwidth:
    def test_critical(self):
        assert classify_bandwidth(700.0) == "CRITICAL"
        assert classify_bandwidth(BW_RED) == "CRITICAL"

    def test_high(self):
        assert classify_bandwidth(550.0) == "HIGH"

    def test_normal(self):
        assert classify_bandwidth(300.0) == "NORMAL"

    def test_low(self):
        assert classify_bandwidth(50.0) == "LOW"

    def test_none(self):
        assert classify_bandwidth(None) == ""


class TestBandwidthFill:
    def test_over_max(self):
        fill, font = bandwidth_fill(850.0)
        assert fill == _excel.DARK_RED_FILL
        assert font is not None

    def test_red(self):
        fill, font = bandwidth_fill(700.0)
        assert fill == _excel.RED_FILL
        assert font is None

    def test_orange(self):
        fill, _ = bandwidth_fill(550.0)
        assert fill == _excel.ORANGE_FILL

    def test_green(self):
        fill, _ = bandwidth_fill(300.0)
        assert fill == _excel.GREEN_FILL

    def test_light_green(self):
        fill, _ = bandwidth_fill(50.0)
        assert fill == _excel.LIGHT_GREEN_FILL

    def test_none(self):
        fill, font = bandwidth_fill(None)
        assert fill is None
        assert font is None


class TestCpuFill:
    def test_critical(self):
        assert cpu_fill(90.0) == _excel.RED_FILL

    def test_high(self):
        assert cpu_fill(80.0) == _excel.RED_FILL

    def test_medium(self):
        assert cpu_fill(50.0) == _excel.ORANGE_FILL

    def test_low(self):
        assert cpu_fill(5.0) == _excel.GREEN_FILL

    def test_normal(self):
        assert cpu_fill(30.0) is None

    def test_none(self):
        assert cpu_fill(None) is None
