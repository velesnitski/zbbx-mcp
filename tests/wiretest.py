"""Shared wire-contract test scaffolding (ADR 072).

Wire-contract tests drive a *real* tool function through a recording fake
client and assert the exact Zabbix API calls it makes — the layer where the
live -32602 ``selectHosts`` bugs lived (ADR 068, ADR 070), invisible to
pure-core tests. Three test suites grew identical private copies of this
scaffolding; this module is the single shared implementation.
"""

import asyncio

__all__ = ["RecordingClient", "CaptureMCP", "StubResolver", "run_tool"]


class RecordingClient:
    """Records every (method, params) and returns canned per-method results.

    ``responses`` maps an API method to either a static result or a callable
    ``params -> result``. Unlisted methods return ``[]``.
    """

    def __init__(self, responses=None):
        self.calls = []
        self._responses = responses or {}

    async def call(self, method, params):
        self.calls.append((method, params))
        r = self._responses.get(method, [])
        return r(params) if callable(r) else r

    def sent(self, method):
        """Params of the first call to ``method`` (raises if never called)."""
        return next(p for m, p in self.calls if m == method)


class CaptureMCP:
    """Captures registered tool functions by name (a module may register many)."""

    def __init__(self):
        self.fns = {}

    def tool(self):
        def deco(f):
            self.fns[f.__name__] = f
            return f
        return deco


class StubResolver:
    def __init__(self, client):
        self._client = client

    def resolve(self, instance):
        return self._client


def run_tool(module, tool_name, client, **kwargs):
    """Register ``module`` against a capture MCP and invoke one tool."""
    mcp = CaptureMCP()
    module.register(mcp, StubResolver(client))
    return asyncio.run(mcp.fns[tool_name](**kwargs))
