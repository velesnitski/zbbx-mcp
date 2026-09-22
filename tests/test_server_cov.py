"""create_server() and its helpers, in-process and without a Zabbix server.

test_server.py drives the stdio transport in a subprocess; this file covers
the wrapper itself — tool wrapping, description compaction, response
compression, resources, read-only / disabled-tool policy, the shutdown hook
and the CLI — where a subprocess cannot reach.
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import sys
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp import FastMCP

from zbbx_mcp import __version__
from zbbx_mcp import logging as logging_mod
from zbbx_mcp import server as server_mod
from zbbx_mcp.data import REGION_MAP
from zbbx_mcp.server import (
    _compact_descriptions,
    _compress_response,
    _iter_registered_tools,
    _register_resources,
    create_server,
    main,
)
from zbbx_mcp.tools import WRITE_TOOLS

_POLICY_VARS = (
    "ZABBIX_INSTANCES", "ZABBIX_READ_ONLY", "DISABLED_TOOLS", "ZABBIX_TIER",
    "ZABBIX_COMPACT_TOOLS", "ZABBIX_COMPACT", "ZABBIX_RESPONSE_BUDGET",
    "ZABBIX_HIDE_PRODUCTS", "ZABBIX_ALLOW_HTTP",
)


@pytest.fixture
def env(monkeypatch):
    """One default instance, no policy overrides, no atexit side effect.

    Yields the list of shutdown hooks create_server() registered, so a test
    can run one by hand instead of leaving it in the interpreter's table.
    """
    monkeypatch.setenv("ZABBIX_URL", "https://zabbix.example.com")
    monkeypatch.setenv("ZABBIX_TOKEN", "test")
    for name in _POLICY_VARS:
        monkeypatch.delenv(name, raising=False)
    hooks: list = []
    real_register = atexit.register

    def register(fn, *args, **kwargs):
        # Only the server's hook is captured; anything else (certifi's
        # cacert context, say) still reaches the real table.
        if fn.__name__ == "_cleanup":
            hooks.append(fn)
            return fn
        return real_register(fn, *args, **kwargs)

    monkeypatch.setattr(atexit, "register", register)
    # Keep the analytics line a wrapped tool would write out of the home dir.
    monkeypatch.setattr(logging_mod, "_analytics_logger", None)
    return hooks


def _tool_names(mcp: FastMCP) -> set[str]:
    return {t.name for t in _iter_registered_tools(mcp)}


async def _read(mcp: FastMCP, uri: str) -> str:
    contents = list(await mcp.read_resource(uri))
    assert len(contents) == 1
    return contents[0].content


class TestIterRegisteredTools:
    def test_yields_every_registered_tool(self):
        mcp = FastMCP("t")

        @mcp.tool()
        async def alpha() -> str:
            return "a"

        @mcp.tool()
        async def beta() -> str:
            return "b"

        assert {t.name for t in _iter_registered_tools(mcp)} == {"alpha", "beta"}

    def test_missing_tool_manager_warns_and_yields_nothing(self, caplog):
        with caplog.at_level(logging.WARNING, logger="zbbx_mcp"):
            assert list(_iter_registered_tools(object())) == []
        assert "tool wrapping disabled" in caplog.text

    def test_tools_not_a_dict_warns_and_yields_nothing(self, caplog):
        broken = SimpleNamespace(_tool_manager=SimpleNamespace(_tools=[1, 2]))
        with caplog.at_level(logging.WARNING, logger="zbbx_mcp"):
            assert list(_iter_registered_tools(broken)) == []
        assert "not a dict" in caplog.text


class TestCompactDescriptions:
    def _mcp(self):
        mcp = FastMCP("t")

        @mcp.tool()
        async def search_things(query: str = "", max_results: int = 50) -> str:
            """Search things by name.

            Args:
                query: Name substring
                max_results: Cap on the result count
            """
            return query

        @mcp.tool()
        async def plain() -> str:
            """No arguments here."""
            return ""

        return mcp

    def test_args_section_and_param_titles_are_stripped(self):
        mcp = self._mcp()
        tool = mcp._tool_manager._tools["search_things"]
        before = tool.description
        props = tool.parameters["properties"]
        titles = [spec["title"] for spec in props.values() if "title" in spec]
        assert "Args:" in before

        saved = _compact_descriptions(mcp)

        assert tool.description == "Search things by name."
        assert not any("title" in spec for spec in tool.parameters["properties"].values())
        # Exactly the chars that left the definition, no more and no less.
        expected = (len(before) - len("Search things by name.")) + sum(len(t) + 11 for t in titles)
        assert saved == expected

    def test_description_without_args_is_untouched(self):
        mcp = self._mcp()
        _compact_descriptions(mcp)
        assert mcp._tool_manager._tools["plain"].description == "No arguments here."

    def test_second_pass_saves_nothing(self):
        mcp = self._mcp()
        _compact_descriptions(mcp)
        assert _compact_descriptions(mcp) == 0


class TestCompressResponse:
    """Complements test_output_budget.py, which pins the budget plumbing."""

    LONG = ("## Head\n**bold** text\n---\n|a|b|\n|---|---|\n|1|2|\n\n\n\nline   \n"
            + "filler line\n" * 30)

    def test_short_text_is_returned_verbatim(self, monkeypatch):
        monkeypatch.setenv("ZABBIX_COMPACT", "1")
        assert _compress_response("**short** ## text") == "**short** ## text"
        assert _compress_response("") == ""

    def test_compact_strips_markdown_but_keeps_table_separators(self, monkeypatch):
        monkeypatch.setenv("ZABBIX_COMPACT", "true")
        monkeypatch.setenv("ZABBIX_RESPONSE_BUDGET", "0")
        out = _compress_response(self.LONG)
        assert "**" not in out
        assert "## " not in out
        assert out.startswith("Head\nbold text\n")
        assert "|---|---|" in out           # the table stays renderable
        assert "\n---\n" not in out         # the bare rule is gone
        assert "\n\n\n" not in out
        assert "line   \n" not in out and "line\n" in out

    def test_without_compact_markdown_is_kept(self, monkeypatch):
        monkeypatch.delenv("ZABBIX_COMPACT", raising=False)
        monkeypatch.setenv("ZABBIX_RESPONSE_BUDGET", "0")
        assert _compress_response(self.LONG) == self.LONG

    def test_truncation_cuts_at_a_line_break_and_states_the_size(self, monkeypatch):
        monkeypatch.delenv("ZABBIX_COMPACT", raising=False)
        monkeypatch.setenv("ZABBIX_RESPONSE_BUDGET", "300")
        text = "line\n" * 100
        out = _compress_response(text)
        body, marker = out.rsplit("\n\n", 1)
        assert marker == f"[truncated {len(text)} chars]"
        assert body == text[:body.rfind("\n") + 1].rstrip("\n") or body.endswith("line")
        assert len(body) <= 300
        assert len(body) >= 300 * 0.7

    def test_truncation_falls_back_to_a_hard_cut_without_a_newline(self, monkeypatch):
        monkeypatch.delenv("ZABBIX_COMPACT", raising=False)
        monkeypatch.setenv("ZABBIX_RESPONSE_BUDGET", "300")
        out = _compress_response("x" * 1000)
        assert out == "x" * 300 + "\n\n[truncated 1000 chars]"


class TestResources:
    async def test_tools_catalog_splits_read_and_write(self):
        mcp = FastMCP("t")
        _register_resources(mcp)
        text = await _read(mcp, "zabbix://tools")
        assert "## Hosts" in text
        assert "Read: search_hosts" in text
        assert "create_host" in WRITE_TOOLS
        hosts_block = text.split("## Hosts\n", 1)[1].split("\n\n", 1)[0]
        assert "Write: create_host, update_host, delete_host" in hosts_block
        assert "search_hosts" not in hosts_block.split("Write:", 1)[1]

    async def test_regions_lists_every_region_sorted(self):
        mcp = FastMCP("t")
        _register_resources(mcp)
        text = await _read(mcp, "zabbix://regions")
        lines = text.splitlines()
        assert [ln.split(":", 1)[0] for ln in lines] == sorted(REGION_MAP)
        for region, codes in REGION_MAP.items():
            assert f"{region}: {', '.join(sorted(codes))}" in lines

    async def test_env_resource_shows_defaults_when_nothing_is_set(self, monkeypatch):
        for name in _POLICY_VARS:
            monkeypatch.delenv(name, raising=False)
        mcp = FastMCP("t")
        _register_resources(mcp)
        assert await _read(mcp, "zabbix://env") == "All defaults (no overrides set)"

    async def test_env_resource_lists_only_safe_overrides(self, monkeypatch):
        for name in _POLICY_VARS:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("ZABBIX_READ_ONLY", "1")
        monkeypatch.setenv("DISABLED_TOOLS", "get_host")
        monkeypatch.setenv("ZABBIX_TOKEN", "test")
        mcp = FastMCP("t")
        _register_resources(mcp)
        text = await _read(mcp, "zabbix://env")
        assert text.splitlines() == ["ZABBIX_READ_ONLY=1", "DISABLED_TOOLS=get_host"]
        assert "ZABBIX_TOKEN" not in text

    async def test_three_resources_are_registered(self):
        mcp = FastMCP("t")
        _register_resources(mcp)
        uris = {str(r.uri) for r in await mcp.list_resources()}
        assert uris == {"zabbix://tools", "zabbix://regions", "zabbix://env"}


class TestCreateServer:
    def test_registers_tools_resources_and_the_default_client(self, env):
        mcp, clients = create_server()
        assert set(clients) == {"default"}
        assert mcp.name == f"zabbix v{__version__}"
        names = _tool_names(mcp)
        assert {"search_hosts", "check_connection", "create_host", "rollback_last"} <= names
        assert names & WRITE_TOOLS == WRITE_TOOLS
        assert len(env) == 1                       # one shutdown hook
        for tool in _iter_registered_tools(mcp):
            # logged() + the compression wrapper, both name-preserving.
            assert tool.fn.__name__ == tool.name
            assert hasattr(tool.fn, "__wrapped__")
            assert "\nArgs:" not in (tool.description or "")

    def test_read_only_mode_registers_no_write_tool(self, env, monkeypatch):
        monkeypatch.setenv("ZABBIX_READ_ONLY", "true")
        mcp, _ = create_server()
        names = _tool_names(mcp)
        assert names & WRITE_TOOLS == set()
        assert "search_hosts" in names

    def test_disabled_tools_are_skipped(self, env, monkeypatch):
        monkeypatch.setenv("DISABLED_TOOLS", "get-host, search_hosts")
        mcp, _ = create_server()
        names = _tool_names(mcp)
        assert "get_host" not in names and "search_hosts" not in names
        assert "check_connection" in names

    def test_compaction_can_be_switched_off(self, env, monkeypatch):
        monkeypatch.setenv("ZABBIX_COMPACT_TOOLS", "false")
        mcp, _ = create_server()
        desc = mcp._tool_manager._tools["search_hosts"].description
        assert "Args:" in desc

    def test_shutdown_hook_closes_every_client(self, env):
        _mcp, clients = create_server()
        (cleanup,) = env
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            cleanup()
        finally:
            asyncio.set_event_loop(None)
            loop.close()
        assert all(c._client.is_closed for c in clients.values())

    def test_shutdown_hook_gives_up_quietly_without_an_event_loop(self, env):
        # atexit runs after asyncio.run() has torn the loop down; the hook must
        # not turn shutdown into a traceback, and it cannot close anything then.
        _mcp, clients = create_server()
        (cleanup,) = env
        asyncio.set_event_loop(None)
        assert cleanup() is None
        assert not any(c._client.is_closed for c in clients.values())

    async def test_wrapped_tools_compress_text_and_pass_other_results_through(self, env, monkeypatch):
        seen: dict = {}

        def fake_register_all(mcp, resolver, read_only=False, disabled_tools=frozenset()):
            seen["resolver"] = resolver
            seen["read_only"] = read_only
            # A registry entry without ``fn`` is left alone, not wrapped.
            mcp._tool_manager._tools["odd"] = SimpleNamespace(name="odd", description="")

            @mcp.tool()
            async def probe_text() -> str:
                return "# Title\n\n**bold** " + "row\n" * 200

            @mcp.tool()
            async def probe_dict() -> dict:
                return {"k": 1}

        monkeypatch.setattr(server_mod, "register_all", fake_register_all)
        monkeypatch.setenv("ZABBIX_COMPACT", "1")
        monkeypatch.setenv("ZABBIX_RESPONSE_BUDGET", "300")
        mcp, _ = create_server()
        assert seen["resolver"].default_name == "default"
        assert seen["read_only"] is False

        text = await mcp._tool_manager._tools["probe_text"].fn()
        assert "**" not in text and not text.startswith("# ")
        assert text.endswith("chars]")
        assert len(text) < 400
        assert await mcp._tool_manager._tools["probe_dict"].fn() == {"k": 1}
        assert not hasattr(mcp._tool_manager._tools["odd"], "fn")


class _FakeMCP:
    def __init__(self):
        self.runs: list[dict] = []

    def run(self, **kwargs):
        self.runs.append(kwargs)


class TestMain:
    def test_version_flag_prints_the_version_and_exits(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["zbbx-mcp", "--version"])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 0
        assert capsys.readouterr().out.strip() == __version__

    def test_stdio_is_the_default_transport(self, monkeypatch):
        fake = _FakeMCP()
        monkeypatch.setattr(server_mod, "create_server", lambda: (fake, {}))
        monkeypatch.setattr(sys, "argv", ["zbbx-mcp"])
        main()
        assert fake.runs == [{"transport": "stdio"}]

    def test_network_transport_passes_host_and_port(self, monkeypatch):
        fake = _FakeMCP()
        monkeypatch.setattr(server_mod, "create_server", lambda: (fake, {}))
        monkeypatch.setattr(sys, "argv", [
            "zbbx-mcp", "--transport", "streamable-http", "--host", "127.0.0.1", "--port", "8999",
        ])
        main()
        assert fake.runs == [{"transport": "streamable-http", "host": "127.0.0.1", "port": 8999}]

    def test_unknown_transport_is_rejected_before_the_server_starts(self, monkeypatch):
        called: list = []
        monkeypatch.setattr(server_mod, "create_server", lambda: called.append(1))
        monkeypatch.setattr(sys, "argv", ["zbbx-mcp", "--transport", "carrier"])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 2
        assert called == []
