"""Wire tests for ``get_predictive_alerts`` (``tools/predictive.py``).

Each fixture host carries a 14-point linear series, so the regression slope
is exact and the days-to-threshold figure can be stated by hand. The tests
pin the tier each host lands in, the rendered row, the cluster collapse and
the parameters of the two API reads. Fixtures are synthetic.
"""

from __future__ import annotations

import time

import pytest

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp import classify
from zbbx_mcp.tools import predictive

NOW = int(time.time())
LIVE = str(NOW)
STALE = str(NOW - 7200)
DISK_FREE = "vfs.fs.size[/,pfree]"
DISK_USED = "vfs.fs.size[/,pused]"
CPU = "system.cpu.util[,idle]"
MEM = "vm.memory.size[available]"
GB = 1_000_000_000


@pytest.fixture(autouse=True)
def _no_product_map(monkeypatch):
    monkeypatch.setattr(classify, "_PRODUCT_MAP", {})


def _series(start, step, n=14):
    return [start + step * i for i in range(n)]


class Fleet:
    def __init__(self):
        self.hosts: list[dict] = []
        self.items: list[dict] = []
        self.trends: dict[str, list[dict]] = {}

    def add(self, hid, name, key=None, values=None, current=None, clock=LIVE):
        self.hosts.append({"hostid": hid, "host": name, "name": name, "groups": [{"name": "app_free"}]})
        if key:
            iid = f"{hid}:{key}"
            self.items.append({"itemid": iid, "hostid": hid, "key_": key, "status": "0",
                               "lastvalue": str(current), "lastclock": clock})
            n = len(values)
            self.trends[iid] = [{"itemid": iid, "clock": str(NOW - (n - 1 - i) * 86400),
                                 "value_avg": str(v)} for i, v in enumerate(values)]
        return self

    def client(self):
        def item_get(p):
            ids = p.get("hostids")
            key = p.get("filter", {}).get("key_")
            needle = (p.get("search") or {}).get("key_", "").strip("*")
            return [it for it in self.items
                    if (not ids or it["hostid"] in ids)
                    and (not key or it["key_"] == key)
                    and (not needle or needle in it["key_"])]

        def trend_get(p):
            return [r for iid in p["itemids"] for r in self.trends.get(iid, [])]

        return RecordingClient({"host.get": self.hosts, "item.get": item_get, "trend.get": trend_get})


def _fleet():
    f = Fleet()
    # Free disk losing one point a day, 26% left: eleven days out.
    f.add("1", "srv-aq9001", DISK_FREE, _series(40, -1), current=26)
    # Reported as used-%: the series must be flipped to free-% before the slope is read.
    f.add("2", "srv-aq9002", DISK_USED, _series(60, 1), current=84)
    f.add("3", "srv-aq9002 aq9003", DISK_USED, _series(60, 1), current=84)    # same machine
    # CPU idle falling one point a day, 25% idle left: five days out.
    f.add("4", "srv-bv9001", CPU, _series(40, -1), current=25)
    # Memory losing 100 MB a day, 1.6 GB left: eleven days out.
    f.add("5", "srv-bv9002", MEM, _series(3 * GB, -100_000_000), current=1_600_000_000)
    f.add("6", "srv-hm9001", DISK_FREE, _series(50, 0), current=50)           # flat: no projection
    f.add("7", "srv-hm9002", DISK_FREE, _series(40, -1, n=5), current=35)     # too new to trust
    f.add("8", "srv-tf9001", DISK_FREE, _series(40, -1), current=26, clock=STALE)  # not reporting
    # Four points a day from 41%: six and a half days, current above the CRITICAL floor.
    f.add("9", "srv-hm9003", DISK_FREE, _series(93, -4), current=41)
    f.add("10", "srv-tf9002", MEM, _series(1 * GB, 100_000_000), current=2 * GB)   # freeing: ignored
    f.add("11", "srv-gs9001", DISK_FREE, _series(61.3, -0.1), current=60)         # beyond the horizon
    f.add("12", "srv-gs9002", DISK_FREE, _series(48, -1), current=35)             # twenty days: INFO
    return f


class TestPredictiveAlerts:
    def test_tiers_rows_and_cluster_collapse(self):
        c = _fleet().client()
        out = run_tool(predictive, "get_predictive_alerts", c)
        assert out.startswith("**6 predicted issues** (1 cluster duplicates collapsed) (next 30 days)\n"), out
        rows = [ln for ln in out.splitlines() if ln.startswith("| ") and "Severity" not in ln and "---" not in ln]
        assert rows == [
            "| CRITICAL | Disk Full | srv-aq9002 (+1: aq9003) | 16.0% | 1.00%/day | 1.0 |",
            "| CRITICAL | CPU Saturation | srv-bv9001 | 75.0% used | 1.00%/day | 5.0 |",
            "| HIGH | Disk Full | srv-hm9003 | 41.0% | 4.00%/day | 6.5 |",
            "| WARNING | Disk Full | srv-aq9001 | 26.0% | 1.00%/day | 11.0 |",
            "| WARNING | Memory Exhaustion | srv-bv9002 | 1.6 GB | 100 MB/day | 11.0 |",
            "| INFO | Disk Full | srv-gs9002 | 35.0% | 1.00%/day | 20.0 |",
        ]
        assert out.endswith("\n**2 CRITICAL** — act now (≤3 days)\n"
                            "**1 HIGH** — act this week (≤7 days)\n"
                            "**2 WARNING** — within 2 weeks"), out
        for name in ("srv-hm9001", "srv-hm9002", "srv-tf9001", "srv-tf9002", "srv-gs9001"):
            assert name not in out

        disk = next(p for m, p in c.calls if m == "item.get" and "search" in p)
        assert disk["search"] == {"key_": "*vfs.fs.size[*"} and disk["searchWildcardsEnabled"] is True
        assert "lastclock" in disk["output"] and disk["filter"] == {"status": "0"}
        trend = c.sent("trend.get")
        # Fourteen days back from the moment of the call, not from import time.
        assert 0 <= (int(time.time()) - 14 * 86400) - trend["time_from"] < 300
        assert trend["output"] == ["itemid", "clock", "value_avg"]
        assert trend["limit"] == len(trend["itemids"]) * 24 * 14

    def test_single_metric_horizon_and_cap(self):
        f = _fleet()
        out = run_tool(predictive, "get_predictive_alerts", f.client(), metric="cpu")
        assert out.startswith("**1 predicted issues** (next 30 days)\n") and "srv-bv9001" in out, out
        assert "Disk Full" not in out
        out = run_tool(predictive, "get_predictive_alerts", f.client(), days_ahead=3)
        assert out.startswith("**1 predicted issues** (1 cluster duplicates collapsed) (next 3 days)\n"), out
        out = run_tool(predictive, "get_predictive_alerts", f.client(), max_results=2)
        assert out.count("\n| ") == 3 and out.endswith("*4 more omitted*"), out

    def test_unknown_metric_and_quiet_fleet(self):
        f = _fleet()
        assert run_tool(predictive, "get_predictive_alerts", f.client(),
                        metric="nope") == "Unknown metric 'nope'. Use: disk, cpu, memory, or all."
        quiet = Fleet().add("6", "srv-hm9001", DISK_FREE, _series(50, 0), current=50)
        assert run_tool(predictive, "get_predictive_alerts", quiet.client()) == \
            "No predicted issues within 30 days."
