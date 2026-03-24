from zbbx_mcp.excel import (
    classify_bandwidth, bandwidth_fill, cpu_fill,
    BW_MAX, BW_RED, BW_ORANGE, BW_GREEN,
    RED_FILL, DARK_RED_FILL, ORANGE_FILL, GREEN_FILL, LIGHT_GREEN_FILL,
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
        assert fill == DARK_RED_FILL
        assert font is not None

    def test_red(self):
        fill, font = bandwidth_fill(700.0)
        assert fill == RED_FILL
        assert font is None

    def test_orange(self):
        fill, _ = bandwidth_fill(550.0)
        assert fill == ORANGE_FILL

    def test_green(self):
        fill, _ = bandwidth_fill(300.0)
        assert fill == GREEN_FILL

    def test_light_green(self):
        fill, _ = bandwidth_fill(50.0)
        assert fill == LIGHT_GREEN_FILL

    def test_none(self):
        fill, font = bandwidth_fill(None)
        assert fill is None
        assert font is None


class TestCpuFill:
    def test_high(self):
        assert cpu_fill(85.0) == RED_FILL

    def test_medium(self):
        assert cpu_fill(60.0) == ORANGE_FILL

    def test_low(self):
        assert cpu_fill(5.0) == GREEN_FILL

    def test_normal(self):
        assert cpu_fill(30.0) is None

    def test_none(self):
        assert cpu_fill(None) is None
