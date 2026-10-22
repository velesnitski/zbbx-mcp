"""Wire tests for ``tools/health.py``: connection check, version matrix,
agent reachability, problem summaries. Every fixture is synthetic."""

from __future__ import annotations

import time

import httpx
import pytest

import zbbx_mcp
from tests.wiretest import RecordingClient, live_triggers, run_tool
from zbbx_mcp import classify
from zbbx_mcp.tools import health
from zbbx_mcp.tools.health import source_tree_version, stale_build_warning

NOW = int(time.time())
LIVE = str(NOW - 60)
DAY = 86400


@pytest.fixture(autouse=True)
def _synthetic_env(monkeypatch):
    monkeypatch.setenv("ZABBIX_PRODUCT_MAP", "")
    monkeypatch.setattr(classify, "_PRODUCT_MAP", None)


def _run(tool, c, **kw):
    return run_tool(health, tool, c, **kw)


def _boom(_p):
    raise httpx.ConnectError("unreachable")


# --------------------------------------------------------- check_connection


class TestCheckConnection:
    def test_version_stale_build_and_expiring_tokens(self, monkeypatch):
        monkeypatch.setattr(zbbx_mcp, "__version__", "1.0.0")
        monkeypatch.setattr(health, "source_tree_version", lambda _f: "1.1.0")
        now = int(time.time())   # not the import-time NOW: day floors must not drift
        tokens = [
            {"name": "ci", "expires_at": str(now + 5 * DAY + 3600), "status": "0"},
            {"name": "old", "expires_at": str(now - 2 * DAY + 3600), "status": "0"},
            {"name": "never", "expires_at": "0", "status": "0"},
            {"name": "off", "expires_at": str(now + 60), "status": "1"},
            {"name": "garbled", "expires_at": "soon", "status": "0"},
            {"name": "later", "expires_at": str(now + 90 * DAY), "status": "0"},
        ]
        c = RecordingClient({"apiinfo.version": "7.0.1", "token.get": tokens})
        out = _run("check_connection", c)
        assert out.startswith("Connected. Zabbix version: 7.0.1")
        assert "Running build v1.0.0, but the source tree is v1.1.0" in out
        assert "⚠ 2 API token(s) expire within 30 days:\n- old: EXPIRED 2d ago\n- ci: in 5d" in out
        assert c.sent("token.get") == {"output": ["name", "expires_at", "status"]}
        assert "ZABBIX_PRODUCT_MAP" in out

    def test_token_api_denied_still_connected(self, monkeypatch):
        monkeypatch.setattr(health, "source_tree_version", lambda _f: "")
        c = RecordingClient({"apiinfo.version": "5.0.0", "token.get": _boom})
        out = _run("check_connection", c)
        assert out.startswith("Connected. Zabbix version: 5.0.0")
        assert "API token" not in out and "Running build" not in out

    def test_connection_failure(self):
        c = RecordingClient({"apiinfo.version": _boom})
        assert _run("check_connection", c) == "Connection failed: unreachable"


class TestBuildVersionHelpers:
    def test_source_tree_version(self, tmp_path):
        pkg = tmp_path / "src" / "pkg"
        pkg.mkdir(parents=True)
        init = pkg / "__init__.py"
        init.write_text("")
        assert source_tree_version(str(init)) == ""                  # no pyproject
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "2.3.4"\n')
        assert source_tree_version(str(init)) == "2.3.4"
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n')
        assert source_tree_version(str(init)) == ""

    def test_stale_build_warning_suppressed(self):
        assert stale_build_warning("1.0.0", "1.0.0") == ""
        assert stale_build_warning("", "1.0.0") == ""
        assert stale_build_warning("0.0.0.dev0", "1.0.0") == ""


# ------------------------------------------------------------ version matrix


class TestZabbixVersion:
    def test_matrix(self):
        out = _run("get_zabbix_version", RecordingClient({"apiinfo.version": "6.4.2"}))
        assert "Zabbix API version: **6.4.2**" in out
        assert "Parsed: major=6, minor=4, patch=2" in out
        assert "- ✓ Cause / symptom rank actions (bits 128/256)" in out
        assert "- ✗ Connector API (data streaming)" in out

    def test_malformed(self):
        out = _run("get_zabbix_version", RecordingClient({"apiinfo.version": "dev"}))
        assert "Parsed: major=0, minor=0, patch=0" in out
        assert "✓" not in out

    def test_api_error(self):
        assert _run("get_zabbix_version", RecordingClient({"apiinfo.version": _boom})) == "Error: unreachable"


# ------------------------------------------------------- agent unreachable


def _host(hid, name, ip):
    return {"hostid": hid, "host": name, "name": name, "interfaces": [{"ip": "127.0.0.1"}, {"ip": ip}]}


AGENT_HOSTS = [
    _host("1", "srv-aq9001", "192.0.2.1"),     # ping 1, live → reachable
    _host("2", "srv-aq9002", "192.0.2.2"),     # ping 0, live → DOWN
    _host("3", "srv-bv9001", "198.51.100.1"),  # ping 1, two hours old → STALE
    _host("4", "srv-bv9002", "198.51.100.2"),  # ping 0 but carries traffic → alive
    _host("5", "srv-hm9001", "203.0.113.1"),   # no agent.ping item at all
]
PINGS = [
    {"hostid": "1", "lastvalue": "1", "lastclock": LIVE},
    {"hostid": "2", "lastvalue": "0", "lastclock": LIVE},
    {"hostid": "3", "lastvalue": "1", "lastclock": str(NOW - 7200)},
    {"hostid": "4", "lastvalue": "0", "lastclock": LIVE},
]
TRAFFIC = [{"hostid": "4", "key_": "net.if.in[eth0]", "lastvalue": "50000000", "lastclock": LIVE}]


def _agent_client(pings=PINGS, hosts=AGENT_HOSTS):
    def item_get(p):
        if "search" in p:
            return TRAFFIC
        if (p.get("filter") or {}).get("key_") == "agent.ping":
            return pings
        return []
    return RecordingClient({"host.get": hosts, "item.get": item_get})


class TestAgentUnreachable:
    def test_down_and_stale(self):
        c = _agent_client()
        out = _run("get_agent_unreachable", c)
        ping = next(p for m, p in c.calls if m == "item.get"
                    and (p.get("filter") or {}).get("key_") == "agent.ping")
        assert ping["filter"] == {"key_": "agent.ping", "status": "0"}
        assert "lastclock" in ping["output"]
        assert out.startswith("**2 unreachable agents** (4 total)")
        assert "| srv-aq9002 | 192.0.2.2 | DOWN | 0.0h ago |" in out
        assert "| srv-bv9001 | 198.51.100.1 | STALE | 2.0h ago |" in out
        assert "srv-bv9002" not in out and "srv-aq9001 " not in out

    def test_truncation(self):
        out = _run("get_agent_unreachable", _agent_client(), max_results=1)
        assert "*1 more omitted*" in out

    def test_all_reachable(self):
        out = _run("get_agent_unreachable", _agent_client(pings=PINGS[:1]))
        assert out == "All agents reachable (1 checked)."

    def test_api_error(self):
        assert _run("get_agent_unreachable", RecordingClient({"host.get": _boom})) == "Error: unreachable"


# ---------------------------------------------------------- active problems


def _p(eid, name, sev, trig, clock=NOW - 100, suppressed="0"):
    return {"eventid": eid, "name": name, "severity": str(sev), "clock": str(clock),
            "objectid": trig, "suppressed": suppressed}


STORM = [_p(f"e{10 + i}", f"Ping loss on srv-hm{9010 + i}", 3, f"tp{i}", NOW - 200) for i in range(10)]
PROBLEMS = [
    _p("e1", "Agent down on srv-aq9001", 4, "t1"),
    _p("e2", "Service check failed on srv-aq9001", 3, "t2"),   # depends on t1 → symptom
    _p("e3", "Disk low", 2, "t3", NOW - 300),
    _p("e4", "Planned work", 4, "t4", suppressed="1"),          # maintenance
    _p("e5", "Ghost", 5, "t5"),                                 # host deleted
    *STORM,
]
EVENT_HOSTS = {"e1": "srv-aq9001", "e2": "srv-aq9001", "e3": "srv-bv9001", "e4": "srv-aq9002",
               **{f"e{10 + i}": f"srv-hm{9010 + i}" for i in range(10)}}


def _problem_client(problems=PROBLEMS):
    def event_get(p):
        return [{"eventid": e, "hosts": [{"host": EVENT_HOSTS[e]}] if e in EVENT_HOSTS else []}
                for e in p["eventids"]]
    return RecordingClient({
        "problem.get": problems,
        "trigger.get": live_triggers([{"triggerid": "t2", "dependencies": [{"triggerid": "t1"}]},
                                      {"triggerid": "t1", "dependencies": []}]),
        "event.get": event_get,
    })


class TestActiveProblems:
    def test_summary_collapse_storm_and_clusters(self):
        c = _problem_client()
        out = _run("get_active_problems", c)
        pg = c.sent("problem.get")
        assert pg["severities"] == [2, 3, 4, 5] and pg["limit"] == 500 and pg["recent"] is True
        assert "tags" not in pg
        tg = next(p for m, p in c.calls if m == "trigger.get" and "selectDependencies" in p)
        assert "t4" not in tg["triggerids"] and {"t1", "t2", "t3", "t5"} <= set(tg["triggerids"])
        assert tg["selectDependencies"] == ["triggerid"]
        assert c.sent("event.get")["selectHosts"] == ["host"]
        assert out.startswith("**12 active problems** (1 dependent symptom(s) collapsed)")
        assert "- **High:** 1\n- **Average:** 10\n- **Warning:** 1" in out
        assert "**Alert storm detected:** 10 simultaneous 'Ping loss' alerts (all within 200s)." in out
        rows = [ln for ln in out.splitlines() if ln.startswith("| ") and "Severity" not in ln]
        assert rows == [
            "| High | srv-aq9001 | Agent down |",
            "| Average | 10 hosts | Ping loss — srv-hm9010, srv-hm9011, srv-hm9012... |",
            "| Warning | srv-bv9001 | Disk low |",
        ]
        assert "*1 cluster incidents collapsed from 9 individual problems*" in out
        assert "Ghost" not in out and "Planned work" not in out

    def test_options(self):
        c = _problem_client()
        out = _run("get_active_problems", c, tags="role:edge", include_suppressed=True,
                   collapse_dependent=False, max_results=1, min_severity=4)
        # The fake returns every problem regardless of severity; the filter is Zabbix's.
        pg = c.sent("problem.get")
        assert pg["tags"] == [{"tag": "role", "value": "edge", "operator": 0}]
        assert pg["evaltype"] == 0 and pg["severities"] == [4, 5]
        assert not any(m == "trigger.get" and "selectDependencies" in _p for m, _p in c.calls)
        assert out.startswith("**14 active problems**\n")            # e2 and e4 kept
        assert "- **High:** 2\n- **Average:** 11\n- **Warning:** 1" in out
        assert "| High | srv-aq9001 | Agent down |" in out
        assert "Planned work" not in out                            # cut by max_results=1
        assert "*4 more omitted*" in out

    def test_none(self):
        c = _problem_client(problems=[])
        assert _run("get_active_problems", c, min_severity=3) == "No active problems (severity >= 3)."
        assert not any(m in ("trigger.get", "event.get") for m, _p in c.calls)

    def test_api_error(self):
        assert _run("get_active_problems", RecordingClient({"problem.get": _boom})) == "Error: unreachable"


# ------------------------------------------------------------ stale servers


STALE_HOSTS = [
    {"hostid": "1", "host": "srv-aq9001", "interfaces": [{"ip": "192.0.2.1"}]},
    {"hostid": "2", "host": "srv-aq9002", "interfaces": [{"ip": "192.0.2.2"}]},
    {"hostid": "3", "host": "srv-bv9001", "interfaces": [{"ip": "198.51.100.1"}]},
    {"hostid": "4", "host": "srv-bv9001 9002", "interfaces": []},
    {"hostid": "5", "host": "srv-hm9001", "interfaces": [{"ip": "203.0.113.1"}]},
    {"hostid": "6", "host": "srv-tf9001", "interfaces": []},
]
STALE_PINGS = [
    {"hostid": "1", "lastclock": LIVE},
    {"hostid": "2", "lastclock": str(NOW - 30 * 3600)},
    {"hostid": "3", "lastclock": str(NOW - 3 * DAY)},
    {"hostid": "4", "lastclock": str(NOW - 5 * DAY)},   # sub-host: older, wins the fold
    {"hostid": "5", "lastclock": "0"},                  # never collected → skipped
]


class TestStaleServers:
    def test_folded_oldest_first(self):
        c = RecordingClient({"host.get": STALE_HOSTS, "item.get": STALE_PINGS})
        out = _run("get_stale_servers", c)
        assert c.sent("item.get")["filter"] == {"key_": "agent.ping", "status": "0"}
        assert out.startswith("**2 stale servers** (data > 24h old)")
        rows = [ln for ln in out.splitlines() if ln.startswith("| srv")]
        assert rows == ["| srv-bv9001 9002 |  | 5d ago |", "| srv-aq9002 | 192.0.2.2 | 30h ago |"]

    def test_truncation_and_threshold(self):
        c = RecordingClient({"host.get": STALE_HOSTS, "item.get": STALE_PINGS})
        assert "*1 more omitted*" in _run("get_stale_servers", c, max_results=1)
        c = RecordingClient({"host.get": STALE_HOSTS, "item.get": STALE_PINGS})
        out = _run("get_stale_servers", c, hours=24 * 10)
        assert out == "All agents reported within 240h (5 checked)."

    def test_api_error(self):
        assert _run("get_stale_servers", RecordingClient({"host.get": _boom})) == "Error: unreachable"


# ------------------------------------------------------------- age buckets


class TestProblemAgeBuckets:
    def test_histogram(self):
        problems = [
            {"eventid": "1", "severity": "4", "clock": str(NOW - 3600)},
            {"eventid": "2", "severity": "4", "clock": str(NOW - 2 * DAY)},
            {"eventid": "3", "severity": "2", "clock": str(NOW - 5 * DAY)},
            {"eventid": "4", "severity": "2", "clock": str(NOW - 10 * DAY)},
            {"eventid": "5", "severity": "3", "clock": "0"},           # no clock → not bucketed
            {"eventid": "6", "severity": "x", "clock": str(NOW)},      # malformed → skipped
        ]
        c = RecordingClient({"problem.get": problems})
        out = _run("get_problem_age_buckets", c, min_severity=1)
        sent = c.sent("problem.get")
        assert sent["severities"] == [1, 2, 3, 4, 5] and sent["limit"] == 5000
        assert out.startswith("**Problem age distribution** (6 active, severity ≥ 1)")
        assert "| High | 1 | 1 | 0 | 0 | 2 |" in out
        assert "| Warning | 0 | 0 | 1 | 1 | 2 |" in out
        assert "| **Total** | 1 | 1 | 1 | 1 | 4 |" in out
        assert "*2 problems aged 1-7d (50% of total).*" in out

    def test_none_aged(self):
        c = RecordingClient({"problem.get": [{"eventid": "1", "severity": "5", "clock": str(NOW)}]})
        out = _run("get_problem_age_buckets", c)
        assert "| Disaster | 1 | 0 | 0 | 0 | 1 |" in out and "aged" not in out

    def test_empty_and_error(self):
        assert _run("get_problem_age_buckets", RecordingClient()) == "No active problems (severity >= 0)."
        assert _run("get_problem_age_buckets", RecordingClient({"problem.get": _boom})) == "Error: unreachable"
