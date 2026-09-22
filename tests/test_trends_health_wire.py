"""Wire tests for the health, shutdown and capacity tools (``tools/trends_health.py``).

The three tools share one pipeline: hosts, then ``fetch_trends_batch`` (one
``item.get`` for the metric keys, one ``trend.get`` for the hourly rows), plus
a service-check read. The fixture below builds that wire from per-host
numbers so each test states its expected verdict in Mbps and CPU percent and
pins the rendered line. Fixtures are synthetic.
"""

from __future__ import annotations

import time

import pytest

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp import classify, fetch
from zbbx_mcp.tools import trends_health

NOW = int(time.time())
LIVE = str(NOW)
CPU = "system.cpu.util[,idle]"
NIC = "net.if.in[eth0]"
LOAD = "system.cpu.load[percpu,avg5]"
CHECK = "primary_check.sh[{HOST.IP}]"


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(classify, "_PRODUCT_MAP", {})
    # The service-check key is read from two namespaces: the health tool's
    # own import and fetch_service_status's.
    monkeypatch.setattr(trends_health, "KEY_service_PRIMARY", CHECK)
    monkeypatch.setattr(fetch, "KEY_service_PRIMARY", CHECK)


def _records(itemid, avgs, maxs=None, mins=None):
    """Hourly trend rows ending now; ``avgs``/``maxs``/``mins`` are raw item units."""
    n = len(avgs)
    maxs = maxs if maxs is not None else avgs
    mins = mins if mins is not None else avgs
    return [{"itemid": itemid, "clock": str(NOW - (n - i) * 3600), "num": "60",
             "value_avg": str(a), "value_max": str(x), "value_min": str(m)}
            for i, (a, x, m) in enumerate(zip(avgs, maxs, mins, strict=True))]


class Fleet:
    """Hosts with per-host CPU-used %, traffic Mbps, load and service state."""

    def __init__(self):
        self.hosts: list[dict] = []
        self.items: list[dict] = []
        self.trends: dict[str, list[dict]] = {}
        self.dashboards: list[dict] = []
        self.groups: list[dict] = []

    def add(self, hid, name, *, ip=None, group="app_free", agent="0",
            cpu_used=None, cpu_used_min=None, cpu_used_max=None, cpu_now=None,
            mbps=None, mbps_avgs=None, mbps_peak=None, mbps_now=None,
            load=None, service=None, n=24):
        h = {"hostid": hid, "host": name, "groups": [{"name": group}],
             "interfaces": [{"ip": ip}] if ip else [], "active_available": agent}
        self.hosts.append(h)
        if cpu_used is not None:
            idle_now = 100 - (cpu_now if cpu_now is not None else cpu_used)
            self.items.append({"itemid": f"c{hid}", "hostid": hid, "key_": CPU,
                               "lastvalue": str(idle_now), "lastclock": LIVE, "value_type": "0"})
            self.trends[f"c{hid}"] = _records(
                f"c{hid}", [100 - cpu_used] * n,
                maxs=[100 - (cpu_used_min if cpu_used_min is not None else cpu_used)] * n,
                mins=[100 - (cpu_used_max if cpu_used_max is not None else cpu_used)] * n)
        if mbps is not None or mbps_avgs is not None:
            avgs = mbps_avgs if mbps_avgs is not None else [mbps] * n
            peak = mbps_peak if mbps_peak is not None else max(avgs)
            now_v = mbps_now if mbps_now is not None else avgs[-1]
            self.items.append({"itemid": f"t{hid}", "hostid": hid, "key_": NIC,
                               "lastvalue": str(now_v * 1_000_000), "lastclock": LIVE, "value_type": "3"})
            self.trends[f"t{hid}"] = _records(
                f"t{hid}", [a * 1_000_000 for a in avgs],
                maxs=[peak * 1_000_000] * len(avgs), mins=[min(avgs) * 1_000_000] * len(avgs))
        if load is not None:
            self.items.append({"itemid": f"l{hid}", "hostid": hid, "key_": LOAD,
                               "lastvalue": str(load), "lastclock": LIVE, "value_type": "0"})
            self.trends[f"l{hid}"] = _records(f"l{hid}", [load] * n)
        if service is not None:
            self.items.append({"itemid": f"s{hid}", "hostid": hid, "key_": CHECK,
                               "lastvalue": str(service), "lastclock": LIVE, "state": "0"})
        return self

    def client(self, **overrides):
        def host_get(p):
            ids = p.get("hostids")
            return [dict(h) for h in self.hosts if not ids or h["hostid"] in ids]

        def item_get(p):
            keys = p.get("filter", {}).get("key_")
            keys = [keys] if isinstance(keys, str) else keys
            ids = p.get("hostids")
            return [it for it in self.items
                    if (not keys or it["key_"] in keys) and (not ids or it["hostid"] in ids)]

        def trend_get(p):
            return [r for iid in p["itemids"] for r in self.trends.get(iid, [])]

        return RecordingClient({"host.get": host_get, "item.get": item_get, "trend.get": trend_get,
                                "dashboard.get": self.dashboards, "hostgroup.get": self.groups,
                                **overrides})


def _mixed_fleet():
    f = Fleet()
    # Chronic overload, near the NIC ceiling, and inefficient against peers.
    f.add("1", "srv-aq9001", ip="192.0.2.10", cpu_used=90, mbps=100, mbps_peak=750, service=1)
    # Traffic collapsed to a fifth of its week: INFO only.
    f.add("2", "srv-aq9002", ip="192.0.2.11", cpu_used=5, mbps=50, mbps_now=10)
    # Carried 20 Mbps once this day, nothing since: recently died.
    f.add("3", "srv-bv9001", ip="198.51.100.20", cpu_used=2, mbps_avgs=[20] + [0] * 23)
    # Busy CPU, no traffic, service down, agent gone: floor score.
    f.add("4", "srv-bv9002", ip="198.51.100.21", cpu_used=70, mbps=0.2, service=0, agent="2")
    # Healthy.
    f.add("5", "srv-hm9001", ip="203.0.113.30", cpu_used=20, mbps=100)
    return f


class TestHealthAssessment:
    def test_scores_and_issue_lines(self):
        c = _mixed_fleet().client()
        out = run_tool(trends_health, "get_health_assessment", c)
        assert out.startswith("**Health Assessment (7d): 5 servers**\n\n"
                              "Healthy: 1 | Warning: 2 | Critical: 1\nAgent unavailable: 1\n"
                              "*1 INFO items omitted (use min_severity='INFO' to see all)*"), out
        body = out.split("omitted (use min_severity='INFO' to see all)*\n", 1)[1]
        assert body == (
            "\n### srv-bv9002 — 0/100 [CRITICAL]\n"
            "app_free | Other | CPU: 70.0% | Traffic: 0.2 Mbps | service: DOWN | Agent: DOWN\n"
            "- High CPU: avg 70.0%\n"
            "- Zombie: CPU 70.0% but traffic 0.2 Mbps\n"
            "- service protocol DOWN\n"
            "- Zabbix agent unavailable\n"
            "\n### srv-aq9001 — 35/100 [WARNING]\n"
            "app_free | Other | CPU: 90.0% | Traffic: 100.0 Mbps | service: OK\n"
            "- Chronic CPU overload: avg 90.0%, never below 90.0%\n"
            "- Inefficient: 90.0% CPU/100Mbps (peer: 20.0%)\n"
            "- Near BW limit: peak 750.0 Mbps\n"
            "\n### srv-bv9001 — 65/100 [WARNING]\n"
            "app_free | Other | CPU: 2.0% | Traffic: 0.8 Mbps\n"
            "- Recently died: peak was 20.0 Mbps, now avg 0.8\n"
        )
        # The service read asks for the clock it needs to judge staleness.
        svc = next(p for m, p in c.calls if m == "item.get" and p.get("filter", {}).get("key_") == CHECK)
        assert svc["filter"] == {"key_": CHECK, "status": "0"}
        assert "lastclock" in svc["output"] and "state" in svc["output"]

    def test_info_severity_shows_the_traffic_drop(self):
        out = run_tool(trends_health, "get_health_assessment", _mixed_fleet().client(), min_severity="INFO")
        assert "omitted" not in out
        assert "### srv-aq9002 — 75/100 [INFO]\napp_free | Other | CPU: 5.0% | Traffic: 50.0 Mbps\n" \
               "- Traffic dropped 80%: 10.0 vs avg 50.0 Mbps" in out, out

    def test_cluster_dead_and_degraded(self):
        f = Fleet()
        for i in (1, 2, 3):
            f.add(str(i), f"srv-bv900{i}", ip=f"198.51.100.{i}", cpu_used=70, mbps=0.2, service=0, agent="2")
        f.groups = [{"groupid": "7"}]
        c = f.client()
        out = run_tool(trends_health, "get_health_assessment", c, group="app_free")
        assert "## Cluster Alerts\n\n**CLUSTER DEAD: BV** — all 3 servers critical" in out, out
        assert c.sent("hostgroup.get")["filter"] == {"name": ["app_free"]}
        assert c.sent("host.get")["groupids"] == ["7"]

        f = Fleet()
        f.add("1", "srv-bv9001", ip="198.51.100.1", cpu_used=70, mbps=0.2, service=0, agent="2")
        f.add("2", "srv-bv9002", ip="198.51.100.2", cpu_used=70, mbps=0.2, service=0, agent="2")
        f.add("3", "srv-bv9003", ip="198.51.100.3", cpu_used=70, mbps=0.2)       # zombie only: 40
        out = run_tool(trends_health, "get_health_assessment", f.client())
        assert "**CLUSTER DEGRADED: BV** — 2/3 servers critical" in out, out

    def test_many_identical_issues_are_grouped(self):
        f = Fleet()
        for i in range(1, 13):
            f.add(str(i), f"srv-aq90{i:02d}", ip=f"192.0.2.{i}", cpu_used=70, mbps=0.2)
        f.add("99", "srv-bv9001", ip="198.51.100.1", cpu_used=90, mbps=100)   # a lone chronic host
        out = run_tool(trends_health, "get_health_assessment", f.client())
        assert "### AQ Other (12 servers) — 40/100 [WARNING]\n- High CPU: avg 70.0%\n" \
               "- Zombie: CPU 70.0% but traffic 0.2 Mbps\n" \
               "  Servers: aq9001, aq9002, aq9003, aq9004, aq9005, aq9006, aq9007, aq9008, +4 more" in out, out
        assert "### srv-bv9001 — 60/100 [WARNING]" in out
        capped = run_tool(trends_health, "get_health_assessment", f.client(), max_results=1)
        assert capped.endswith("*Showing top entries. 13 total issues.*"), capped

    def test_no_match_unknown_group_and_all_healthy(self):
        f = Fleet().add("1", "srv-aq9001", ip="192.0.2.1", cpu_used=20, mbps=100)
        assert run_tool(trends_health, "get_health_assessment", f.client(),
                        country="hm") == "No servers match the filters."
        assert run_tool(trends_health, "get_health_assessment", f.client(),
                        group="nope") == "Group 'nope' not found."
        assert run_tool(trends_health, "get_health_assessment", f.client(),
                        period="1d") == "All 1 servers healthy over 1d."

    def test_a_failed_trend_fetch_degrades_to_no_findings(self):
        # The gather is declared return_exceptions=True: a trend failure must
        # fall back to "no trend rows", not escape as an unpack error.
        def boom(_p):
            raise ValueError("trend store down")
        f = Fleet().add("1", "srv-aq9001", ip="192.0.2.1", cpu_used=90, mbps=100)
        out = run_tool(trends_health, "get_health_assessment", f.client(**{"trend.get": boom}))
        assert out == "All 1 servers healthy over 7d.", out


def _shutdown_fleet():
    f = Fleet()
    f.add("1", "srv-aq9001", ip="192.0.2.1", cpu_used=2, mbps=0.2)                     # DEAD
    f.add("2", "srv-aq9002", ip="192.0.2.2", cpu_used=60, mbps=0.3)                    # ZOMBIE
    f.add("3", "srv-aq9003", ip="192.0.2.3", cpu_used=10, mbps=3, service=0)           # BROKEN
    f.add("4", "srv-aq9004", ip="192.0.2.4", cpu_used=3, mbps=4)                       # IDLE, on a dashboard
    f.add("5", "srv-aq9005", ip="192.0.2.5", cpu_used=3, mbps=2)                       # IDLE, one machine ...
    f.add("6", "srv-aq9005 aq9006", ip="192.0.2.6", cpu_used=4, mbps=1.5)              # ... with a sub-host
    f.add("7", "srv-aq9007", ip="192.0.2.7", cpu_used=30, mbps=100, mbps_peak=102)     # the only busy peer
    f.add("8", "srv-bv9001", ip="198.51.100.1", cpu_used=3, mbps=4)                    # IDLE, alone in BV
    f.dashboards = [{"dashboardid": "1", "name": "Free - AQ", "pages": [
        {"name": "West", "widgets": [{"fields": [{"type": "3", "value": "4"}]}]}]}]
    return f


class TestShutdownCandidates:
    def test_categories_folding_and_peer_headroom(self):
        c = _shutdown_fleet().client()
        out = run_tool(trends_health, "get_shutdown_candidates", c)
        assert out == (
            "**Shutdown Candidates (7d): 6 of 8 servers**\n\n"
            "DEAD: 1 | ZOMBIE: 1 | BROKEN: 1 | IDLE: 3\n"
            "\n**DEAD (1):** srv-aq9001\n"
            "\n**ZOMBIE (1):** srv-aq9002 (0.3 Mbps) SAFE (2Mbps headroom)\n"
            "\n**BROKEN (1):** srv-aq9003 (3.0 Mbps service DOWN) RISKY (2Mbps headroom)\n"
            "\n**IDLE (3):** srv-aq9005 (+1 sub) (3.5 Mbps) RISKY (2Mbps headroom), "
            "srv-aq9004 (4.0 Mbps [AQ - West]) RISKY (2Mbps headroom), "
            "srv-bv9001 (4.0 Mbps) SOLO (no peers)\n"
            "\n*Peer-headroom: 1 SOLO (no peers), 3 RISKY (insufficient cohort capacity).*"
        ), out
        assert c.sent("dashboard.get") == {"output": ["dashboardid", "name"], "selectPages": "extend"}

    def test_filters_and_orphans(self):
        f = _shutdown_fleet()
        f.dashboards = []
        out = run_tool(trends_health, "get_shutdown_candidates", f.client(),
                       country="aq", tier="Default", product="app_free")
        assert out.startswith("**Shutdown Candidates (7d): 5 of 7 servers**"), out
        assert "All orphaned (not on any dashboard)" in out
        assert "srv-bv9001" not in out
        assert run_tool(trends_health, "get_shutdown_candidates", f.client(),
                        product="app_paid") == "No servers match the filters."

    def test_a_busy_fleet_has_no_candidates(self):
        f = Fleet()
        f.add("1", "srv-aq9001", ip="192.0.2.1", cpu_used=30, mbps=100)
        f.add("2", "srv-aq9001 aq9002", ip="192.0.2.2", cpu_used=30, mbps=100)
        assert run_tool(trends_health, "get_shutdown_candidates", f.client()) == \
            "No shutdown candidates among 1 servers."


def _capacity_fleet():
    f = Fleet()
    # Chronic CPU, saturated and rising NIC, high load: every signal at once.
    f.add("1", "srv-aq9001", ip="192.0.2.1", cpu_used=85, load=4,
          mbps_avgs=[620] * 12 + [720] * 12, mbps_peak=780, mbps_now=720)
    # Frequent CPU spikes, and far less traffic per CPU than its peers.
    f.add("2", "srv-aq9002", ip="192.0.2.2", cpu_used=75, cpu_used_min=40, mbps=100)
    # Saturated NIC with 100 Mbps of headroom; no CPU item at all.
    f.add("3", "srv-bv9001", ip="198.51.100.1", mbps=650, mbps_peak=700)
    f.add("9", "srv-bv9001 bv9003", ip="198.51.100.3")        # sub-host: inherits the parent
    f.add("4", "srv-hm9001", ip="203.0.113.1", cpu_used=20, mbps=100)   # healthy peer
    return f


class TestCapacityPlanning:
    def test_signals_severity_and_actions(self):
        out = run_tool(trends_health, "get_capacity_planning", _capacity_fleet().client())
        assert out.startswith("**Capacity Planning (7d): 4 servers need attention**\n\n"
                              "CRITICAL: 1 | HIGH: 1 | MEDIUM: 2\n"), out
        rows = [ln for ln in out.splitlines() if ln.startswith("| ") and "Priority" not in ln and "---" not in ln]
        assert rows == [
            "| CRITICAL | srv-aq9001 | AQ | app_free/Default | 85.0% | 670 Mbps | 780 Mbps | rising | Add replicas or upgrade hardware |",
            "| HIGH | srv-aq9002 | AQ | app_free/Default | 75.0% | 100 Mbps | 100 Mbps | stable | Upgrade to faster hardware |",
            "| MEDIUM | srv-bv9001 | BV | app_free/Default | N/A | 650 Mbps | 700 Mbps | stable | Upgrade NIC or add load balancer |",
            "| MEDIUM | srv-bv9001 bv9003 | BV | app_free/Default | N/A | 650 Mbps | 700 Mbps | stable | Upgrade NIC or add load balancer |",
        ]
        assert out.split("### Top Issues\n\n", 1)[1].startswith(
            "**srv-aq9001** (Other):\n"
            "  - CPU chronic: avg 85.0%, min 85.0%\n"
            "  - BW saturated: avg 670.0, peak 780.0 Mbps (headroom: 20)\n"
            "  - Traffic rising: 720.0 vs avg 670.0 Mbps\n"
            "  - High load: avg 4.0\n"
            "**srv-aq9002** (Other):\n"
            "  - CPU frequent: avg 75.0%, min 40.0%\n"
            "  - Inefficient: 75.0% CPU/100Mbps (peer: 20.0%)\n"
        )

    def test_priority_floor_and_cap(self):
        out = run_tool(trends_health, "get_capacity_planning", _capacity_fleet().client(),
                       min_priority="HIGH", max_results=1)
        assert "CRITICAL: 1 | HIGH: 1 | MEDIUM: 2" in out          # totals are pre-filter
        rows = [ln for ln in out.splitlines() if ln.startswith("| ") and "Priority" not in ln]
        assert [r.split(" | ")[1] for r in rows] == ["srv-aq9001"]
        assert "*1 more servers omitted (use max_results to see all)*" in out, out
        assert "srv-bv9001" not in out

    def test_nothing_overloaded(self):
        f = Fleet().add("4", "srv-hm9001", ip="203.0.113.1", cpu_used=20, mbps=100)
        assert run_tool(trends_health, "get_capacity_planning", f.client()) == "No overloaded servers among 1."
        assert run_tool(trends_health, "get_capacity_planning", f.client(),
                        country="aq") == "No servers match the filters."
