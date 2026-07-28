"""Per-host traffic aggregation + the data/fetch import cycle (ADR 098)."""

import importlib
import subprocess
import sys

from zbbx_mcp.anomaly import aggregate_host_windows
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


def _agg(iid_to_hid, baseline, recent, parent_map=None, keys=None):
    """Group per host, then delegate to the SHARED rule.

    Imports aggregate_host_windows rather than restating it, so this file
    cannot silently drift from the implementation it claims to lock (ADR 100).
    """
    parent_map = parent_map or {}
    keys = keys or {}
    by_host: dict = {}
    for iid, hid in iid_to_hid.items():
        canon = parent_map.get(hid, hid)
        by_host.setdefault(canon, []).append(
            (keys.get(iid, ""), baseline.get(iid), recent.get(iid))
        )
    hb, hr = {}, {}
    for canon, entries in by_host.items():
        agg = aggregate_host_windows(entries)
        if agg is None:
            continue
        hb[canon], hr[canon] = agg
    return hb, hr


class TestDisruptionAggregation:
    def test_bond_and_slaves_do_not_double_count(self):
        # bond0 IS eno1+eno2; summing reported 2x the real throughput.
        iid_to_hid = {"bond": "h1", "s1": "h1", "s2": "h1"}
        keys = {"bond": "net.if.in[bond0]", "s1": "net.if.in[eno1]",
                "s2": "net.if.in[eno2]"}
        base = {"bond": 100.0, "s1": 60.0, "s2": 40.0}
        rec = {"bond": 50.0, "s1": 30.0, "s2": 20.0}
        hb, hr = _agg(iid_to_hid, base, rec, keys=keys)
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
        # Independent NICs sum: two real cards genuinely carry both loads.
        assert hb["parent"] == 110.0 and hr["parent"] == 50.0

    def test_total_silence_is_an_outage_not_an_absent_host(self):
        # The regression this file missed: a single-NIC host that goes fully
        # dark has NO recent rows. Skipping it blinded the disruption
        # detector to the mass-outage case it exists to find (ADR 100).
        hb, hr = _agg({"i1": "h1"}, {"i1": 100.0}, {})
        assert hb["h1"] == 100.0 and hr["h1"] == 0.0
        assert (hb["h1"] - hr["h1"]) / hb["h1"] * 100 == 100.0
