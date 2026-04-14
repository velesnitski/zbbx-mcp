import json
import os
import subprocess
import sys


class TestServerStartup:
    def _run_jsonrpc(self, *messages, timeout=15):
        """Send JSON-RPC messages to the server via stdio and return responses."""
        input_data = "\n".join(json.dumps(m) for m in messages) + "\n"

        env = os.environ.copy()
        env["ZABBIX_URL"] = "https://test.zabbix.example.com"
        env["ZABBIX_TOKEN"] = "test-token"

        result = subprocess.run(
            [sys.executable, "-m", "zbbx_mcp.server"],
            input=input_data,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        # Parse responses (one per line)
        responses = []
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if line:
                try:
                    responses.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return responses

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
        assert resp["result"]["serverInfo"]["name"] == "zabbix"

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
        assert len(tool_names) == 121, f"Expected 121 tools, got {len(tool_names)}: {sorted(tool_names)}"
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
