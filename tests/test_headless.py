"""ADR 163: headless mode — any tool once from the command line.

The command builds the real server (registration, gating, compression,
analytics) and calls through ``FastMCP.call_tool``; only the Zabbix client is
the recording fake. Synthetic data, example.com URL.
"""

from __future__ import annotations

import io
import json
import time

import pytest

from tests.wiretest import RecordingClient
from zbbx_mcp import headless
from zbbx_mcp.headless import (
    EXIT_OK,
    EXIT_TOOL_ERROR,
    EXIT_USAGE,
    UsageError,
    build_args,
    is_headless,
    parse_run,
    result_text,
    typed,
)

HOSTS = [{"hostid": "9001", "host": "srv-aq9001", "name": "srv-aq9001", "status": "0",
          "interfaces": [{"ip": "192.0.2.1"}], "groups": [{"name": "app"}]}]


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("ZABBIX_URL", "https://zabbix.example.com")
    monkeypatch.setenv("ZABBIX_TOKEN", "t")
    monkeypatch.delenv("ZABBIX_READ_ONLY", raising=False)
    monkeypatch.delenv("DISABLED_TOOLS", raising=False)
    monkeypatch.delenv("ZABBIX_TIER", raising=False)
    monkeypatch.delenv("ZABBIX_INSTANCES", raising=False)
    monkeypatch.setenv("ZBBX_FILE_ROOTS", str(tmp_path))
    fakes: list[RecordingClient] = []

    def fake_client(_cfg, responses=None):
        c = RecordingClient(responses or {"host.get": HOSTS})
        fakes.append(c)
        return c

    from zbbx_mcp import server
    monkeypatch.setattr(server, "ZabbixClient", fake_client)
    return fakes


def run(*argv, stdin=""):
    out, err = io.StringIO(), io.StringIO()
    code = headless.main(list(argv), stdin=io.StringIO(stdin), stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


class TestCommandLine:
    def test_only_run_and_tools_are_headless(self):
        assert is_headless(["run", "x"]) and is_headless(["tools"])
        assert not is_headless([]) and not is_headless(["--transport", "stdio"])

    def test_flags_may_sit_between_pairs(self):
        ra = parse_run(["get_host", "host_id=1", "--json", "--timeout", "30", "x=y", "--args={}"])
        assert (ra.tool, ra.pairs, ra.as_json, ra.timeout, ra.args_json) == \
            ("get_host", ["host_id=1", "x=y"], True, 30.0, "{}")

    @pytest.mark.parametrize("argv", [[], ["--json"], ["t", "--timeout"], ["t", "--timeout", "x"],
                                      ["t", "--timeout", "0"], ["t", "--bogus"]])
    def test_bad_command_lines_are_usage_errors(self, argv):
        with pytest.raises(UsageError):
            parse_run(argv)


class TestTyping:
    SCHEMA = {"properties": {
        "n": {"type": "integer"}, "x": {"type": "number"}, "on": {"type": "boolean"},
        "ids": {"anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "null"}]},
        "q": {"type": "string"}}, "required": ["q"]}

    def test_pairs_are_typed_by_the_schema(self):
        args = build_args("t", self.SCHEMA, "", ["n=5", "x=0.5", "on=yes", 'ids=["a"]', "q=5"])
        assert args == {"n": 5, "x": 0.5, "on": True, "ids": ["a"], "q": "5"}

    def test_pairs_override_the_args_object_and_stdin_works(self):
        args = build_args("t", self.SCHEMA, "-", ["n=2"], io.StringIO('{"q": "a", "n": 1}'))
        assert args == {"q": "a", "n": 2}

    @pytest.mark.parametrize("pairs,needle", [
        (["q=a", "nope=1"], "takes no argument 'nope'; it takes: ids, n, on, q, x"),
        (["q=a", "n=1.5"], "not a whole number"),
        (["q=a", "on=maybe"], "not true or false"),
        (["q=a", "ids=[oops"], "takes JSON"),
        (["n=1"], "needs q="),
        (["q"], "not key=value"),
    ])
    def test_mistakes_say_what_was_expected(self, pairs, needle):
        with pytest.raises(UsageError, match=needle.replace("[", r"\[").replace("(", r"\(")):
            build_args("t", self.SCHEMA, "", pairs)

    def test_an_args_object_with_an_unknown_key_is_refused(self):
        with pytest.raises(UsageError, match="takes no argument 'zz'"):
            build_args("t", self.SCHEMA, '{"q": "a", "zz": 1}', [])
        with pytest.raises(UsageError, match="JSON object"):
            build_args("t", self.SCHEMA, "[1]", [])

    def test_untyped_property_stays_a_string(self):
        assert typed("k", "007", {}) == "007"

    def test_result_text_reads_every_shape(self):
        class Block:
            text = "a"
        assert result_text(([Block(), Block()], {"result": "a\na"})) == "a\na"
        assert result_text({"k": 1}) == '{\n  "k": 1\n}'


class TestEndToEnd:
    def test_tools_lists_registered_tools(self, env):
        code, out, _ = run("tools")
        assert code == EXIT_OK and out.splitlines()[0].split()[0] < out.splitlines()[-1].split()[0]
        assert any(line.startswith("search_hosts ") for line in out.splitlines())
        code, out, _ = run("tools", "--json")
        defs = {d["name"]: d for d in json.loads(out)}
        assert "query" in defs["search_hosts"]["inputSchema"]["properties"]

    def test_run_goes_through_the_real_pipeline(self, env):
        code, out, err = run("run", "search_hosts", "query=srv-aq9001", "--json")
        assert code == EXIT_OK, err
        res = json.loads(out)
        assert res["tool"] == "search_hosts" and res["ok"] is True
        assert res["args"] == {"query": "srv-aq9001"}
        assert "srv-aq9001" in res["text"]
        assert env[0].sent("host.get") is not None
        assert "files" not in res

    def test_plain_output_is_the_text_only(self, env):
        code, out, _ = run("run", "search_hosts", "query=srv")
        assert code == EXIT_OK and "srv-aq9001" in out and not out.lstrip().startswith("{")

    def test_a_tool_error_exits_1(self, env, monkeypatch):
        from zbbx_mcp import server

        def broken(_cfg):
            def boom(_p):
                raise ValueError("Zabbix API error (-32602): nope")
            return RecordingClient({"host.get": boom})
        monkeypatch.setattr(server, "ZabbixClient", broken)
        code, out, _ = run("run", "search_hosts", "--json")
        assert code == EXIT_TOOL_ERROR and json.loads(out)["ok"] is False

    def test_read_only_hides_write_tools(self, env, monkeypatch):
        monkeypatch.setenv("ZABBIX_READ_ONLY", "true")
        code, _, err = run("run", "delete_host", "host_id=9001")
        assert code == EXIT_USAGE and "may be hidden by ZABBIX_READ_ONLY" in err

    def test_unknown_tool_and_bad_args_are_usage_errors(self, env):
        assert run("run", "no_such_tool")[0] == EXIT_USAGE
        code, _, err = run("run", "search_hosts", "qeury=x")
        assert code == EXIT_USAGE and "takes no argument 'qeury'" in err

    def test_missing_configuration_is_a_usage_error(self, monkeypatch):
        monkeypatch.delenv("ZABBIX_URL", raising=False)
        monkeypatch.delenv("ZABBIX_INSTANCES", raising=False)
        code, _, err = run("tools")
        assert code == EXIT_USAGE and "ZABBIX_URL" in err

    def test_timeout_is_a_tool_error(self, env, monkeypatch):
        import asyncio

        from zbbx_mcp import server

        def slow(_cfg):
            class Slow(RecordingClient):
                async def call(self, method, params=None):
                    await asyncio.sleep(5)
                    return []
            return Slow({})
        monkeypatch.setattr(server, "ZabbixClient", slow)
        code, out, _ = run("run", "search_hosts", "--timeout", "0.2", "--json")
        assert code == EXIT_TOOL_ERROR and "did not finish within 0.2s" in json.loads(out)["text"]


class TestOutputs:
    def test_files_written_through_the_gate_are_listed(self, tmp_path):
        from zbbx_mcp.utils import (
            safe_output_path,
            stop_tracking_outputs,
            track_outputs,
            written_since,
        )
        paths, token = track_outputs()
        start = time.time()
        try:
            written = safe_output_path(str(tmp_path), "a.csv")
            safe_output_path(str(tmp_path), "never-written.csv")
            with open(written, "w") as f:
                f.write("x")
        finally:
            stop_tracking_outputs(token)
        assert written_since(paths, start) == [written]
        safe_output_path(str(tmp_path), "outside.csv")      # not tracking: no-op
        assert len(paths) == 2

    def test_every_tool_that_writes_a_file_goes_through_a_gate(self):
        """The ``files`` field is only complete if no tool writes around the
        two confinement gates."""
        import pathlib
        import re
        root = pathlib.Path(__file__).resolve().parents[1] / "src" / "zbbx_mcp" / "tools"
        writes = re.compile(r"""open\([^)]*["'][wa]b?["']|\.save\(|write_text\(|to_excel\(""")
        offenders = [p.name for p in root.glob("*.py")
                     if writes.search(p.read_text())
                     and "safe_output_path" not in p.read_text()
                     and "confined_output_path" not in p.read_text()]
        assert not offenders, offenders


def test_the_call_is_logged_like_a_client_call_and_marked_cli(env):
    """The analytics wrapper sees the call (so it ran through the same
    registration and wrapping as a client's), tagged ``via: cli``."""
    import logging

    from zbbx_mcp import logging as zlog

    lines: list[dict] = []

    class Capture(logging.Handler):
        # Formats at emit time, as the real file handler does.
        def emit(self, record):
            lines.append(json.loads(zlog.AnalyticsFormatter().format(record)))

    log = logging.getLogger("zbbx_mcp.analytics.headless-test")
    log.setLevel(logging.INFO)
    log.propagate = False
    log.addHandler(Capture())
    old = zlog._analytics_logger
    zlog._analytics_logger = log
    try:
        assert run("run", "search_hosts", "query=srv")[0] == EXIT_OK
        (entry,) = [e for e in lines if e["tool"] == "search_hosts"]
        assert entry["via"] == "cli" and entry["status"] == "ok"
        # ...and the command left the process as it found it.
        assert zlog._via == "" and logging.getLogger("zbbx_mcp").propagate is True
    finally:
        zlog._analytics_logger = old
