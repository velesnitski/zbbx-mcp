"""Fixtures for the end-to-end layer: a real Zabbix 7.0 container, a real MCP.

The stack runs in CI (``.github/workflows/integration.yml``); locally it needs
Docker (see ``tests/integration/README.md``). Without ``ZBBX_IT_URL`` every
test here is skipped with a stated reason, so ``uv run pytest`` stays green on
a machine with no container.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from tests.integration.seed import default_state_path

_HERE = Path(__file__).resolve().parent
_SKIP_REASON = (
    "integration stack not configured: set ZBBX_IT_URL to a seeded Zabbix 7.0 "
    "container (see tests/integration/README.md)"
)
PROTOCOL_VERSION = "2025-06-18"


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "integration: end-to-end test against a live Zabbix container (needs ZBBX_IT_URL)",
    )


def pytest_collection_modifyitems(config, items):
    for item in items:
        if Path(str(item.fspath)).resolve().parent != _HERE:
            continue
        item.add_marker(pytest.mark.integration)
        if not os.environ.get("ZBBX_IT_URL"):
            item.add_marker(pytest.mark.skip(reason=_SKIP_REASON))


class McpError(AssertionError):
    """A JSON-RPC error or an ``isError`` tool result from the MCP."""


class McpProcess:
    """The MCP server as a stdio subprocess, spoken to in raw JSON-RPC."""

    def __init__(self, env: dict[str, str], cwd: Path):
        self.proc = subprocess.Popen(
            _server_cmd(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env, cwd=cwd, text=True, bufsize=1,
        )
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._stderr: list[str] = []
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()
        self._next_id = 0
        self.server_info: dict = {}

    def _pump_stdout(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self._lines.put(line)
        self._lines.put(None)  # EOF: the server exited

    def _pump_stderr(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self._stderr.append(line)

    def _send(self, msg: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: dict | None = None, timeout: float = 180) -> dict:
        """Send one request; return the whole JSON-RPC reply with the same id."""
        self._next_id += 1
        rid = self._next_id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        while True:
            try:
                line = self._lines.get(timeout=timeout)
            except queue.Empty:
                raise AssertionError(f"{method}: no reply in {timeout}s. stderr tail:\n{self.stderr_tail()}") from None
            if line is None:
                raise AssertionError(f"MCP exited during {method}. stderr tail:\n{self.stderr_tail()}")
            line = line.strip()
            if not line:
                continue
            msg = json.loads(line)
            if msg.get("id") == rid:
                return msg

    def initialize(self) -> dict:
        reply = self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "zbbx-it", "version": "0"},
        })
        if "error" in reply:
            raise McpError(f"initialize failed: {reply['error']}")
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.server_info = reply["result"]
        return reply["result"]

    def tool_names(self) -> set[str]:
        names: set[str] = set()
        cursor = None
        while True:
            reply = self.request("tools/list", {"cursor": cursor} if cursor else {})
            if "error" in reply:
                raise McpError(f"tools/list failed: {reply['error']}")
            names.update(t["name"] for t in reply["result"]["tools"])
            cursor = reply["result"].get("nextCursor")
            if not cursor:
                return names

    def call_raw(self, tool: str, /, **arguments) -> dict:
        return self.request("tools/call", {"name": tool, "arguments": arguments})

    def call(self, tool: str, /, **arguments) -> str:
        """Call a tool and return its text; any protocol or tool-level error fails the test.

        ``-32602`` (invalid params) from the MCP layer or a ``Zabbix API
        error`` string from the server both land here — the bug class the
        recorded-fake wire tests cannot see.
        """
        name = tool
        reply = self.call_raw(tool, **arguments)
        if "error" in reply:
            raise McpError(f"{name}: JSON-RPC error {reply['error']}")
        result = reply["result"]
        text = "\n".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
        if result.get("isError"):
            raise McpError(f"{name}: tool error: {text}")
        for marker in ("Zabbix API error", "Invalid params", "Error querying Zabbix", "-32602", "-32500"):
            if marker in text:
                raise McpError(f"{name}: API error surfaced in output: {text[:500]}")
        if text.startswith("Error"):
            raise McpError(f"{name}: {text[:500]}")
        return text

    def stderr_tail(self, n: int = 40) -> str:
        return "".join(self._stderr[-n:])

    def close(self) -> None:
        if self.proc.stdin:
            self.proc.stdin.close()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)


def _server_cmd() -> list[str]:
    """``zbbx-mcp`` from the current environment — what ``uv run zbbx-mcp`` executes.

    ``ZBBX_IT_CMD`` overrides it (whitespace-split), e.g. ``uv run zbbx-mcp``.
    """
    override = os.environ.get("ZBBX_IT_CMD", "").split()
    if override:
        return override
    script = Path(sys.executable).parent / "zbbx-mcp"
    if script.exists():
        return [str(script)]
    found = shutil.which("zbbx-mcp")
    if found:
        return [found]
    return [sys.executable, "-c", "from zbbx_mcp.server import main; main()"]


@pytest.fixture(scope="session")
def seed_state() -> dict:
    path = default_state_path()
    if not path.exists():
        pytest.fail(f"ZBBX_IT_URL is set but no seed state at {path}; run tests/integration/seed.py first")
    state = json.loads(path.read_text())
    token = os.environ.get("ZBBX_IT_TOKEN") or state.get("token")
    if not token:
        pytest.fail("seed state carries no API token")
    state["token"] = token
    state["url"] = os.environ.get("ZBBX_IT_URL") or state["url"]
    return state


@pytest.fixture(scope="session")
def server_env(seed_state, tmp_path_factory) -> dict[str, str]:
    """Environment for the MCP subprocess: the container, and nothing of $HOME."""
    home = tmp_path_factory.mktemp("home")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("ZABBIX_", "SENTRY_", "ZBBX_FILE_ROOTS", "DISABLED_TOOLS"))}
    env.update({
        "ZABBIX_URL": seed_state["url"],
        "ZABBIX_TOKEN": seed_state["token"],
        "ZABBIX_LOG_FILE": str(home / "zbbx-mcp.log"),
        "ZABBIX_ANALYTICS_FILE": str(home / "analytics.log"),
        "ZBBX_FILE_ROOTS": str(home),
        "HOME": str(home),
    })
    return env


def _start(env: dict[str, str]) -> McpProcess:
    p = McpProcess(env, cwd=_HERE.parent.parent)
    p.initialize()
    return p


@pytest.fixture(scope="session")
def mcp(server_env):
    p = _start(server_env)
    yield p
    p.close()


@pytest.fixture
def start_mcp(server_env):
    """Factory for an extra MCP process with environment overrides."""
    started: list[McpProcess] = []

    def _factory(**overrides: str) -> McpProcess:
        p = _start({**server_env, **overrides})
        started.append(p)
        return p

    yield _factory
    for p in started:
        p.close()
