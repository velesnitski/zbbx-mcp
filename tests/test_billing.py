"""Tests for billing CSV reconciliation helpers."""

import tempfile
from pathlib import Path

from zbbx_mcp.tools.costs import (
    COST_SRC_CLUSTER_EXTRAS,
    _cluster_new_val,
    _dedup_name_from_ip_entries,
    _load_billing_csv,
    _prefix_name_match,
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


class TestClusterNewVal:
    def test_fresh_host_no_prior_description(self):
        base, new_val = _cluster_new_val(current=100.0, existing_desc="", extras=25.0)
        assert (base, new_val) == (100.0, 125.0)

    def test_idempotent_rerun_with_prior_cluster_extras(self):
        # Simulate calling again with the same extras: new_val should match
        # what the prior run already wrote (120.50).
        desc = f"{COST_SRC_CLUSTER_EXTRAS} base 80.50 + 2 extra IPs (40.00)"
        base, new_val = _cluster_new_val(current=120.50, existing_desc=desc, extras=40.0)
        assert (base, new_val) == (80.50, 120.50)

    def test_rerun_with_changed_extras_uses_prior_base(self):
        # New extras amount comes in; we still start from the recorded base.
        desc = f"{COST_SRC_CLUSTER_EXTRAS} base 80.00 + 2 extra IPs (40.00)"
        base, new_val = _cluster_new_val(current=120.0, existing_desc=desc, extras=55.0)
        assert (base, new_val) == (80.0, 135.0)

    def test_overwrite_base_replaces_current(self):
        # overwrite_base wins even when the macro already has a billing-backed
        # description. This is the "reset a stale base" escape hatch.
        desc = "src:billing_ip exact-match"
        base, new_val = _cluster_new_val(
            current=450.0, existing_desc=desc, extras=20.0, overwrite_base=100.0,
        )
        assert (base, new_val) == (100.0, 120.0)

    def test_overwrite_base_zero_is_honoured(self):
        # overwrite_base=0 is a legal request (reset to zero then add extras)
        base, new_val = _cluster_new_val(
            current=999.0, existing_desc="anything", extras=42.0, overwrite_base=0.0,
        )
        assert (base, new_val) == (0.0, 42.0)

    def test_overwrite_negative_skipped(self):
        # overwrite_base=-1 is the default sentinel; behaviour must match the
        # no-override path.
        desc = f"{COST_SRC_CLUSTER_EXTRAS} base 50.00 + 1 extra IP (10.00)"
        base, new_val = _cluster_new_val(
            current=60.0, existing_desc=desc, extras=10.0, overwrite_base=-1.0,
        )
        assert (base, new_val) == (50.0, 60.0)


class TestPrefixNameMatch:
    """Regression tests for the Pass-5 prefix matcher.

    The legacy body was ``zname.startswith(name_lower) or name_lower.startswith(zname)``
    with first-match-wins. That bound densely-numbered host families incorrectly
    (e.g. ``srv10`` to ``srv100``, ``web1`` to ``web14``) and silently overwrote
    correct billing-backed prices. The replacement adds two guards: skip when the
    delta is digits only, skip when multiple Zabbix names satisfy the relation.
    """

    def _build(self, names):
        name_list = list(names)
        name_to_host = {n: {"host": n, "hostid": str(i)} for i, n in enumerate(names)}
        return name_list, name_to_host

    def test_digit_extension_rejected_short_to_long(self):
        # Sheet uses ``srv100`` (4-digit), Zabbix uses ``srv10`` (3-digit).
        # startswith would bind them; we must not.
        name_list, name_to_host = self._build(["srv10"])
        assert _prefix_name_match("srv100", name_list, name_to_host) is None

    def test_digit_extension_rejected_long_to_short(self):
        # Mirror case: Zabbix has the longer name.
        name_list, name_to_host = self._build(["srv100"])
        assert _prefix_name_match("srv10", name_list, name_to_host) is None

    def test_non_digit_extension_matches(self):
        # Legitimate truncation: sheet has ``app-eu1``, Zabbix has ``app-eu1-retired``.
        # The character after the shared prefix is a separator, not a digit.
        name_list, name_to_host = self._build(["app-eu1-retired"])
        match = _prefix_name_match("app-eu1", name_list, name_to_host)
        assert match is not None
        assert match["host"] == "app-eu1-retired"

    def test_ambiguous_multiple_candidates_skipped(self):
        # Two Zabbix names both satisfy the prefix relation — skip rather than
        # pick one arbitrarily.
        name_list, name_to_host = self._build(["db-1-shard-a", "db-1-shard-b"])
        assert _prefix_name_match("db-1", name_list, name_to_host) is None

    def test_exact_name_not_a_prefix_match(self):
        # If the sheet name equals a Zabbix name, Pass 2 (exact) handles it.
        # Pass 5 must not also claim it as a prefix match.
        name_list, name_to_host = self._build(["exact-host"])
        assert _prefix_name_match("exact-host", name_list, name_to_host) is None

    def test_name_too_short_skipped(self):
        # Names under 4 chars are too generic — skip.
        name_list, name_to_host = self._build(["abcdef"])
        assert _prefix_name_match("abc", name_list, name_to_host) is None

    def test_no_candidates_returns_none(self):
        name_list, name_to_host = self._build(["unrelated-host"])
        assert _prefix_name_match("different", name_list, name_to_host) is None

    def test_digit_extension_coexists_with_valid_match(self):
        # Sheet ``db-primary`` matches Zabbix ``db-primary-v2`` (valid) but not
        # ``db-primary10`` (digit-extended). Only the valid one survives.
        name_list, name_to_host = self._build(["db-primary-v2", "db-primary10"])
        match = _prefix_name_match("db-primary", name_list, name_to_host)
        assert match is not None
        assert match["host"] == "db-primary-v2"


class TestDedupNameFromIpEntries:
    """Regression tests for duplicate-name detection (ADR 009).

    When the billing source has multiple IP rows that share a name but disagree
    on the price (e.g. the same host listed three times at $72, $84, $164
    because it moved SKU over time and the sheet was never pruned), silently
    picking the first-inserted price masks a data-quality problem upstream.
    The helper returns duplicates separately so the caller can surface them.
    """

    @staticmethod
    def _in_range(v):
        p = v.get("price") if isinstance(v, dict) else v
        try:
            return 1 <= float(p) <= 5000
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _extract_price(v):
        return v.get("price") if isinstance(v, dict) else v

    def _run(self, ip_costs):
        return _dedup_name_from_ip_entries(ip_costs, self._in_range, self._extract_price)

    def test_single_entry_per_name_is_unique(self):
        unique, dup = self._run({
            "10.0.0.1": {"name": "host-a", "price": 50.0},
            "10.0.0.2": {"name": "host-b", "price": 75.0},
        })
        assert unique == {"host-a": 50.0, "host-b": 75.0}
        assert dup == {}

    def test_duplicate_same_price_stays_unique(self):
        # Two IPs, same name, same price → not a conflict.
        unique, dup = self._run({
            "10.0.0.1": {"name": "host-a", "price": 50.0},
            "10.0.0.2": {"name": "host-a", "price": 50.0},
        })
        assert unique == {"host-a": 50.0}
        assert dup == {}

    def test_duplicate_conflicting_prices_reported(self):
        # Same name across three IPs at three distinct prices — drop from
        # unique, list all prices in duplicates.
        unique, dup = self._run({
            "10.0.0.1": {"name": "host-a", "price": 50.0},
            "10.0.0.2": {"name": "host-a", "price": 75.0},
            "10.0.0.3": {"name": "host-a", "price": 100.0},
        })
        assert "host-a" not in unique
        assert dup == {"host-a": [50.0, 75.0, 100.0]}

    def test_rounding_to_two_decimals(self):
        # 50.001 and 50.004 should collapse to a single 50.00; 50.009 rounds
        # to 50.01 and would count as a second distinct price.
        unique, dup = self._run({
            "10.0.0.1": {"name": "host-a", "price": 50.001},
            "10.0.0.2": {"name": "host-a", "price": 50.004},
        })
        assert unique == {"host-a": 50.0}
        assert dup == {}

    def test_out_of_range_entries_ignored(self):
        # An out-of-range entry must not contribute to the dup check.
        unique, dup = self._run({
            "10.0.0.1": {"name": "host-a", "price": 50.0},
            "10.0.0.2": {"name": "host-a", "price": 999999.0},  # out of range
        })
        assert unique == {"host-a": 50.0}
        assert dup == {}

    def test_missing_or_empty_name_ignored(self):
        unique, dup = self._run({
            "10.0.0.1": {"name": "", "price": 50.0},
            "10.0.0.2": {"price": 75.0},
            "10.0.0.3": {"name": "real-host", "price": 100.0},
        })
        assert unique == {"real-host": 100.0}
        assert dup == {}

    def test_non_dict_entries_ignored(self):
        # Flat IP→cost form doesn't contribute to name-derived map.
        unique, dup = self._run({
            "10.0.0.1": 50.0,
            "10.0.0.2": {"name": "host-a", "price": 75.0},
        })
        assert unique == {"host-a": 75.0}
        assert dup == {}

    def test_mixed_unique_and_duplicate(self):
        unique, dup = self._run({
            "10.0.0.1": {"name": "ok", "price": 50.0},
            "10.0.0.2": {"name": "conflict", "price": 75.0},
            "10.0.0.3": {"name": "conflict", "price": 90.0},
        })
        assert unique == {"ok": 50.0}
        assert dup == {"conflict": [75.0, 90.0]}
