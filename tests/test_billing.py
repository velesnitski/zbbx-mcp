"""Tests for billing CSV reconciliation helpers."""

import tempfile
from pathlib import Path

from zbbx_mcp.tools.costs import _load_billing_csv


def _write_csv(content: str) -> str:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as tmp:
        tmp.write(content)
        return tmp.name


class TestLoadBillingCsv:
    def test_standard_headers(self):
        path = _write_csv(
            "ip,billing_name,price_monthly\n"
            "1.2.3.4,host-a,50.00\n"
            "5.6.7.8,host-b,75.50\n"
        )
        rows = _load_billing_csv(path)
        assert len(rows) == 2
        assert rows[0] == {"ip": "1.2.3.4", "name": "host-a", "price": 50.0}
        assert rows[1] == {"ip": "5.6.7.8", "name": "host-b", "price": 75.5}
        Path(path).unlink()

    def test_header_aliases(self):
        path = _write_csv(
            "ipaddress,hostname,cost\n"
            "1.2.3.4,host-a,42\n"
        )
        rows = _load_billing_csv(path)
        assert len(rows) == 1
        assert rows[0]["price"] == 42.0
        Path(path).unlink()

    def test_skips_zero_price(self):
        path = _write_csv("ip,billing_name,price_monthly\n1.2.3.4,a,0\n2.2.2.2,b,10\n")
        rows = _load_billing_csv(path)
        assert len(rows) == 1
        assert rows[0]["ip"] == "2.2.2.2"
        Path(path).unlink()

    def test_skips_empty_ip(self):
        path = _write_csv("ip,billing_name,price_monthly\n,a,10\n1.2.3.4,b,20\n")
        rows = _load_billing_csv(path)
        assert len(rows) == 1
        assert rows[0]["ip"] == "1.2.3.4"
        Path(path).unlink()

    def test_skips_non_numeric_price(self):
        path = _write_csv("ip,billing_name,price_monthly\n1.2.3.4,a,abc\n")
        rows = _load_billing_csv(path)
        assert rows == []
        Path(path).unlink()

    def test_skips_reserved_ip_ranges(self):
        path = _write_csv(
            "ip,billing_name,price_monthly\n"
            "0.0.0.0,a,10\n"
            "127.0.0.1,b,10\n"
            "224.0.0.1,c,10\n"
            "255.255.255.0,d,10\n"
            "8.8.8.8,e,10\n"
        )
        rows = _load_billing_csv(path)
        ips = [r["ip"] for r in rows]
        assert ips == ["8.8.8.8"]
        Path(path).unlink()

    def test_handles_whitespace(self):
        path = _write_csv(
            "ip, billing_name , price_monthly\n"
            " 1.2.3.4 , host-a , 50.00 \n"
        )
        rows = _load_billing_csv(path)
        assert len(rows) == 1
        assert rows[0]["ip"] == "1.2.3.4"
        assert rows[0]["name"] == "host-a"
        Path(path).unlink()

    def test_empty_csv(self):
        path = _write_csv("ip,billing_name,price_monthly\n")
        assert _load_billing_csv(path) == []
        Path(path).unlink()

    def test_malformed_ip_skipped(self):
        path = _write_csv(
            "ip,billing_name,price_monthly\n"
            "not-an-ip,a,10\n"
            "1.2.3,b,10\n"
            "1.2.3.4,c,10\n"
        )
        rows = _load_billing_csv(path)
        assert len(rows) == 1
        assert rows[0]["ip"] == "1.2.3.4"
        Path(path).unlink()
