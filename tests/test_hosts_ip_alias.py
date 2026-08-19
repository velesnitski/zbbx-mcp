"""`search_hosts_by_ip` accepts the spelling its own name suggests.

The tool is named "by_ip" and its parameter was `query`, so `ip=...` raised a
validation error. A tool that rejects the obvious spelling of its own subject
answers worse than it could — the alias costs nothing and removes an entire
error class.
"""

from __future__ import annotations

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp.tools import hosts


def _client():
    return RecordingClient({"host.get": []})


def test_query_still_works():
    out = run_tool(hosts, "search_hosts_by_ip", _client(), query="192.0.2.10")
    assert "Provide an IP" not in out


def test_ip_alias_is_accepted():
    out = run_tool(hosts, "search_hosts_by_ip", _client(), ip="192.0.2.10")
    assert "Provide an IP" not in out


def test_both_spellings_agree():
    a = run_tool(hosts, "search_hosts_by_ip", _client(), query="192.0.2.10")
    b = run_tool(hosts, "search_hosts_by_ip", _client(), ip="192.0.2.10")
    assert a == b


def test_neither_given_explains_rather_than_raising():
    # Previously a pydantic ValidationError surfaced as a ToolError, which a
    # caller cannot act on as readily as a sentence naming the parameter.
    out = run_tool(hosts, "search_hosts_by_ip", _client())
    assert "Provide an IP" in out
