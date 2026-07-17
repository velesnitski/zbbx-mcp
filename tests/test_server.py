import json
import os
import subprocess
import sys
import threading
import time


class TestServerStartup:
    def _run_jsonrpc(self, *messages, timeout=15):
        """Send JSON-RPC messages over stdio and return the parsed responses.

        stdin is held **open** until every request (a message carrying an
        ``id``) has a matching response, and only then closed. The old approach
        batch-wrote the messages and closed stdin immediately
        (``subprocess.run(input=...)``), which raced the stdio server's
        EOF-triggered shutdown: under the hardened MCP SDK the session could
        tear down before answering the last queued request, so `tools/list`
        went unanswered — deterministically on a slow/loaded CI runner while
        passing on a fast local machine.
        """
        env = os.environ.copy()
        env["ZABBIX_URL"] = "https://test.zabbix.example.com"
        env["ZABBIX_TOKEN"] = "test-token"

        proc = subprocess.Popen(
            [sys.executable, "-m", "zbbx_mcp.server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,  # avoid a full-pipe deadlock; tests read stdout only
            text=True,
            env=env,
        )

        responses: list[dict] = []
        lock = threading.Lock()

        def _reader():
            for line in proc.stdout:  # ends when stdout closes on process exit
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                with lock:
                    responses.append(obj)

        reader = threading.Thread(target=_reader, daemon=True)
        reader.start()

        want_ids = {m["id"] for m in messages if "id" in m}
        try:
            for m in messages:
                proc.stdin.write(json.dumps(m) + "\n")
            proc.stdin.flush()

            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                with lock:
                    if want_ids.issubset({r.get("id") for r in responses}):
                        break
                time.sleep(0.02)
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            reader.join(timeout=2)

        with lock:
            return list(responses)

    def test_initialize(self):
        responses = self._run_jsonrpc({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1.0.0"},
                "protocolVersion": "2024-11-05",
            },
        })
        assert len(responses) >= 1
        resp = responses[0]
        assert resp.get("id") == 1
        assert "result" in resp
        # serverInfo.name now includes the version (e.g. "zabbix v1.9.4") so
        # Claude Code's /mcp UI shows it next to the connection status.
        # See server.py create_server() comment.
        assert resp["result"]["serverInfo"]["name"].startswith("zabbix v")

    def test_tools_list(self):
        responses = self._run_jsonrpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1.0.0"},
                    "protocolVersion": "2024-11-05",
                },
            },
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
            },
        )
        tools_resp = None
        for r in responses:
            if r.get("id") == 2:
                tools_resp = r
                break
        assert tools_resp is not None, f"No tools/list response found in: {responses}"
        tools = tools_resp["result"]["tools"]
        tool_names = {t["name"] for t in tools}
        assert len(tool_names) == 163, f"Expected 163 tools, got {len(tool_names)}: {sorted(tool_names)}"
        # Spot check core tools
        assert "search_hosts" in tool_names
        assert "get_problems" in tool_names
        assert "check_connection" in tool_names
        assert "get_server_map" in tool_names
        assert "generate_server_report" in tool_names
        assert "rollback_last" in tool_names

    def test_tool_has_description(self):
        responses = self._run_jsonrpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1.0.0"},
                    "protocolVersion": "2024-11-05",
                },
            },
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
            },
        )
        tools_resp = None
        for r in responses:
            if r.get("id") == 2:
                tools_resp = r
                break
        assert tools_resp is not None
        for tool in tools_resp["result"]["tools"]:
            assert tool.get("description"), f"Tool '{tool['name']}' has no description"
