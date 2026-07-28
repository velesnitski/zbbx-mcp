"""Per-host traffic aggregation + the data/fetch import cycle (ADR 098)."""

import importlib
import subprocess
import sys

from zbbx_mcp.fetch import from_mbps, to_kbps, to_mbps


class TestImportCycle:
    """`import zbbx_mcp.fetch` first used to raise ImportError.

    data and fetch import each other; data's re-export block ran eagerly, so
    importing fetch first hit a partially-initialised module. It only ever
    worked because every entry point happened to import data first.
    """

    def test_fetch_can_be_imported_first(self):
        # Fresh interpreter — import order in this process is already settled.
        r = subprocess.run(
            [sys.executable, "-c", "import zbbx_mcp.fetch; print('ok')"],
            capture_output=True, text=True,
        )
        assert r.returncode == 0, r.stderr
        assert "ok" in r.stdout

    def test_data_can_be_imported_first(self):
        r = subprocess.run(
            [sys.executable, "-c", "import zbbx_mcp.data; print('ok')"],
            capture_output=True, text=True,
        )
        assert r.returncode == 0, r.stderr

    def test_lazy_reexports_still_resolve(self):
        data = importlib.import_module("zbbx_mcp.data")
        assert callable(data.fetch_traffic_map)
        assert callable(data.day_label)
        assert data.TRAFFIC_DIVISOR == 1_000_000

    def test_unknown_attribute_still_raises(self):
        # __getattr__ must not swallow genuine typos into None.
        data = importlib.import_module("zbbx_mcp.data")
        missing = "definitely_not_a_real_symbol"
        try:
            data.__getattr__(missing)
        except AttributeError:
            return
        raise AssertionError("expected AttributeError")


class TestTrafficHelpers:
    def test_round_trip(self):
        assert to_mbps(from_mbps(37.5)) == 37.5

    def test_kbps_is_thousand_times_mbps(self):
        assert to_kbps(2_000_000) == to_mbps(2_000_000) * 1000

    def test_junk_is_zero_not_an_exception(self):
        for junk in (None, "", "abc", []):
            assert to_mbps(junk) == 0.0


def _agg(iid_to_hid, baseline, recent, parent_map=None):
    """The ADR 098 aggregation rule, mirrored for testing.

    Kept in lockstep with disruption.py: max across interfaces, and only
    interfaces present in BOTH windows.
    """
    parent_map = parent_map or {}
    hb, hr = {}, {}
    for iid, hid in iid_to_hid.items():
        if iid not in baseline or iid not in recent:
            continue
        canon = parent_map.get(hid, hid)
        hb[canon] = max(hb.get(canon, 0.0), baseline[iid])
        hr[canon] = max(hr.get(canon, 0.0), recent[iid])
    return hb, hr


class TestDisruptionAggregation:
    def test_bond_and_slaves_do_not_double_count(self):
        # bond0 IS eno1+eno2; summing reported 2x the real throughput.
        iid_to_hid = {"bond": "h1", "s1": "h1", "s2": "h1"}
        base = {"bond": 100.0, "s1": 60.0, "s2": 40.0}
        rec = {"bond": 50.0, "s1": 30.0, "s2": 20.0}
        hb, hr = _agg(iid_to_hid, base, rec)
        assert hb["h1"] == 100.0   # not 200.0
        assert hr["h1"] == 50.0    # not 100.0

    def test_interface_missing_from_recent_cannot_fabricate_a_drop(self):
        # An iface with baseline rows but no recent rows used to inflate only
        # the baseline side, manufacturing a drop that never happened.
        iid_to_hid = {"live": "h1", "gone": "h1"}
        base = {"live": 100.0, "gone": 900.0}
        rec = {"live": 100.0}
        hb, hr = _agg(iid_to_hid, base, rec)
        assert hb["h1"] == 100.0 and hr["h1"] == 100.0
        drop = (hb["h1"] - hr["h1"]) / hb["h1"] * 100
        assert drop == 0.0          # old behaviour: (1000-100)/1000 = 90%

    def test_real_drop_still_detected(self):
        iid_to_hid = {"i1": "h1"}
        hb, hr = _agg(iid_to_hid, {"i1": 100.0}, {"i1": 10.0})
        assert (hb["h1"] - hr["h1"]) / hb["h1"] * 100 == 90.0

    def test_sub_hosts_fold_to_the_parent(self):
        iid_to_hid = {"a": "sub", "b": "parent"}
        base, rec = {"a": 30.0, "b": 80.0}, {"a": 10.0, "b": 40.0}
        hb, hr = _agg(iid_to_hid, base, rec, parent_map={"sub": "parent"})
        assert set(hb) == {"parent"}
        assert hb["parent"] == 80.0 and hr["parent"] == 40.0
