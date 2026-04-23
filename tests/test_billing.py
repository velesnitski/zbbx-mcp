"""Tests for billing CSV reconciliation helpers."""

import tempfile
from pathlib import Path

from zbbx_mcp.tools.costs import (
    COST_SRC_CLUSTER_EXTRAS,
    _load_billing_csv,
    _strip_prior_cluster_extras,
)


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


class TestStripPriorClusterExtras:
    def test_no_description(self):
        assert _strip_prior_cluster_extras(100.0, "") == 100.0

    def test_unrelated_description(self):
        assert _strip_prior_cluster_extras(100.0, "src:billing_ip exact match") == 100.0

    def test_single_extra_ip(self):
        desc = f"{COST_SRC_CLUSTER_EXTRAS} base 150.00 + 1 extra IP (50.00)"
        # Current = 200 (= base 150 + extras 50). Expect base = 150.
        assert _strip_prior_cluster_extras(200.0, desc) == 150.0

    def test_multiple_extra_ips(self):
        desc = f"{COST_SRC_CLUSTER_EXTRAS} base 100.00 + 3 extra IPs (75.00)"
        assert _strip_prior_cluster_extras(175.0, desc) == 100.0

    def test_idempotency_recompute(self):
        # Simulate a re-run: existing macro was written by a previous
        # cluster_extras pass, and we're applying the same input again.
        # The true base stays put; new_val will stay put too.
        desc = f"{COST_SRC_CLUSTER_EXTRAS} base 80.50 + 2 extra IPs (40.00)"
        base = _strip_prior_cluster_extras(120.50, desc)
        assert base == 80.50
        # Next call would compute new_val = base + extras = 80.50 + 40 = 120.50.

    def test_malformed_base_falls_back_to_current(self):
        # If the base number is corrupt, we fall back to current (safe)
        desc = f"{COST_SRC_CLUSTER_EXTRAS} base N/A + 1 extra IP (50.00)"
        assert _strip_prior_cluster_extras(200.0, desc) == 200.0
