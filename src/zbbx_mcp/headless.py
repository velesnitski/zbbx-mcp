"""Headless mode: run any tool once from the command line (ADR 163).

``zbbx-mcp tools`` lists the tools and ``zbbx-mcp run <tool> [key=value ...]
[--args JSON|-] [--json] [--timeout SECONDS]`` calls one, for cron, launchd,
CI jobs and other bots that need what the tools already do without an MCP
client and without a second implementation.

The server is built exactly as the transports build it and the call goes
through the same ``FastMCP.call_tool`` the protocol handler uses, so the
argument validation, the response compression and budget, the analytics and
the ``ZABBIX_READ_ONLY`` / ``DISABLED_TOOLS`` / ``ZABBIX_TIER`` gating are the
ones an MCP client gets: a write tool under read-only does not exist here
either. Nothing is wired per tool; a tool is reachable the day it is
registered.

Exit codes: 0 success, 1 the tool reported an error (or timed out),
2 a usage or configuration error. Logs go to stderr at WARNING, so stdout
holds only the result. Mirrors slk-mcp's headless mode (its ADR 136).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import IO, Any

EXIT_OK = 0
EXIT_TOOL_ERROR = 1
EXIT_USAGE = 2

USAGE_RUN = ("usage: zbbx-mcp run <tool> [key=value ...] "
             "[--args JSON|-] [--json] [--timeout SECONDS]")
USAGE = "usage: zbbx-mcp tools [--json] | " + USAGE_RUN.removeprefix("usage: ")


class UsageError(Exception):
    """A mistake in the command line or configuration, not in Zabbix."""


def is_headless(argv: Sequence[str]) -> bool:
    return bool(argv) and argv[0] in ("run", "tools")


# --- command line ------------------------------------------------------------

@dataclass
class RunArgs:
    tool: str
    pairs: list[str] = field(default_factory=list)
    args_json: str = ""
    as_json: bool = False
    timeout: float = 300.0


def parse_run(argv: Sequence[str]) -> RunArgs:
    """``run`` arguments with flags allowed anywhere among the pairs, the way
    people type them. Pure."""
    if not argv or argv[0].startswith("-"):
        raise UsageError(USAGE_RUN)
    out = RunArgs(tool=argv[0])
    rest = list(argv[1:])
    while rest:
        tok = rest.pop(0)
        if tok == "--json":
            out.as_json = True
        elif tok in ("--args", "--timeout") or tok.startswith(("--args=", "--timeout=")):
            name, _, val = tok.partition("=")
            if not val:
                if not rest:
                    raise UsageError(f"{name} needs a value")
                val = rest.pop(0)
            if name == "--args":
                out.args_json = val
            else:
                try:
                    out.timeout = float(val)
                except ValueError:
                    raise UsageError(f"--timeout {val!r} is not a number of seconds") from None
                if out.timeout <= 0:
                    raise UsageError("--timeout must be positive")
        elif tok.startswith("--"):
            raise UsageError(f"unknown flag {tok}; {USAGE_RUN}")
        else:
            out.pairs.append(tok)
    return out


def _schema_type(spec: Any) -> str:
    """The JSON type a property accepts, looking through ``anyOf`` for the
    first non-null branch (how optional parameters are described). Pure."""
    if not isinstance(spec, dict):
        return ""
    if isinstance(spec.get("type"), str):
        return spec["type"]
    for branch in spec.get("anyOf") or []:
        t = _schema_type(branch)
        if t and t != "null":
            return t
    return ""


_TRUE = {"true", "1", "yes", "on"}
_FALSE = {"false", "0", "no", "off"}


def typed(key: str, val: str, spec: Any) -> Any:
    """A command-line string converted to the type the schema declares, or a
    UsageError that says what was expected. Pure."""
    t = _schema_type(spec)
    if t == "integer":
        try:
            f = float(val)
        except ValueError:
            raise UsageError(f"{key}={val!r} is not a whole number") from None
        if not f.is_integer():
            raise UsageError(f"{key}={val!r} is not a whole number")
        return int(f)
    if t == "number":
        try:
            return float(val)
        except ValueError:
            raise UsageError(f"{key}={val!r} is not a number") from None
    if t == "boolean":
        low = val.strip().lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
        raise UsageError(f"{key}={val!r} is not true or false")
    if t in ("array", "object"):
        try:
            return json.loads(val)
        except json.JSONDecodeError as e:
            raise UsageError(f"{key} takes JSON: {e.msg}") from None
    return val


def build_args(tool_name: str, schema: dict, args_json: str, pairs: Sequence[str],
               stdin: IO[str] | None = None) -> dict[str, Any]:
    """Merge the ``--args`` object and the ``key=value`` pairs (pairs win),
    typing each pair by the schema and refusing keys the tool does not take,
    so a typo fails loudly instead of being ignored. Pure apart from stdin."""
    out: dict[str, Any] = {}
    if args_json:
        raw = (stdin or sys.stdin).read() if args_json == "-" else args_json
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as e:
            raise UsageError(f"--args is not JSON: {e.msg}") from None
        if not isinstance(obj, dict):
            raise UsageError("--args must be a JSON object")
        out.update(obj)
    props = schema.get("properties") or {}
    for pair in pairs:
        key, sep, val = pair.partition("=")
        if not sep or not key:
            raise UsageError(f"argument {pair!r} is not key=value")
        if key not in props:
            raise UsageError(f"{tool_name} takes no argument {key!r}; it takes: "
                             + (", ".join(sorted(props)) or "nothing"))
        out[key] = typed(key, val, props[key])
    unknown = sorted(set(out) - set(props))
    if unknown:
        raise UsageError(f"{tool_name} takes no argument {unknown[0]!r}; it takes: "
                         + (", ".join(sorted(props)) or "nothing"))
    missing = [r for r in schema.get("required") or [] if r not in out]
    if missing:
        raise UsageError(f"{tool_name} needs {missing[0]}=…")
    return out


def result_text(result: Any) -> str:
    """The text of a ``call_tool`` result: content blocks, a
    ``(content, structured)`` pair, or a bare structured dict. Pure."""
    if isinstance(result, tuple) and result:
        result = result[0]
    if isinstance(result, dict):
        return json.dumps(result, ensure_ascii=False, indent=2)
    parts = [getattr(block, "text", None) for block in result or []]
    return "\n".join(p for p in parts if isinstance(p, str))


def first_sentence(text: str) -> str:
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    head, _, _ = line.partition(". ")
    return head.rstrip(".")


# --- execution ---------------------------------------------------------------

@contextlib.contextmanager
def _quiet_logging():
    """For the length of one headless command: our stderr at WARNING, off
    FastMCP's root handler, FastMCP's own messages at WARNING, analytics
    marked ``via: cli``. Everything is put back afterwards, so calling
    :func:`main` in-process (tests, an embedding script) leaves the logging
    as it found it."""
    from zbbx_mcp import logging as zlog

    zlogger = logging.getLogger("zbbx_mcp")
    propagate, via = zlogger.propagate, zlog._via
    env_set = "FASTMCP_LOG_LEVEL" not in os.environ
    # A record propagated from a child logger skips the root's level check,
    # so only our handlers — stderr at WARNING, the log file, the analytics —
    # should see ours. FastMCP installs its root handler when the server is
    # constructed, at FASTMCP_LOG_LEVEL.
    zlog.set_stderr_level(logging.WARNING)
    zlogger.propagate = False
    if env_set:
        os.environ["FASTMCP_LOG_LEVEL"] = "WARNING"
    zlog.set_via("cli")
    try:
        yield
    finally:
        zlog.set_stderr_level(logging.NOTSET)
        zlogger.propagate = propagate
        zlog.set_via(via)
        if env_set:
            os.environ.pop("FASTMCP_LOG_LEVEL", None)


def _build():
    """The server exactly as the transports build it. Imported here so the
    pure helpers above stay importable without configuration."""
    from zbbx_mcp.server import create_server
    return create_server()


async def _close(clients: dict) -> None:
    for c in clients.values():
        try:
            await c.close()
        except Exception:  # noqa: BLE001 — shutdown must not mask the result
            pass


async def _list(mcp) -> list:
    return sorted(await mcp.list_tools(), key=lambda t: t.name)


async def cmd_tools(argv: Sequence[str], stdout: IO[str]) -> int:
    if any(a != "--json" for a in argv):
        raise UsageError(USAGE)
    mcp, clients = _build()
    try:
        tools = await _list(mcp)
    finally:
        await _close(clients)
    if "--json" in argv:
        json.dump([{"name": t.name, "description": t.description or "",
                    "inputSchema": t.inputSchema} for t in tools],
                  stdout, ensure_ascii=False, indent=2)
        stdout.write("\n")
    else:
        width = max((len(t.name) for t in tools), default=0)
        for t in tools:
            stdout.write(f"{t.name:<{width}}  {first_sentence(t.description or '')}\n")
    return EXIT_OK


async def cmd_run(argv: Sequence[str], stdin: IO[str], stdout: IO[str],
                  stderr: IO[str]) -> int:
    from mcp.server.fastmcp.exceptions import ToolError

    from zbbx_mcp.logging import is_error_text
    from zbbx_mcp.utils import stop_tracking_outputs, track_outputs, written_since

    ra = parse_run(argv)
    mcp, clients = _build()
    try:
        tool = next((t for t in await _list(mcp) if t.name == ra.tool), None)
        if tool is None:
            raise UsageError(
                f"unknown tool {ra.tool!r} (it may be hidden by ZABBIX_READ_ONLY, "
                "DISABLED_TOOLS or ZABBIX_TIER); `zbbx-mcp tools` lists what is available")
        call_args = build_args(ra.tool, tool.inputSchema or {}, ra.args_json, ra.pairs, stdin)

        paths, token = track_outputs()
        started = time.time()
        try:
            result = await asyncio.wait_for(mcp.call_tool(ra.tool, call_args), ra.timeout)
            text, ok = result_text(result), True
        except ToolError as e:
            text, ok = str(e), False
        except asyncio.TimeoutError:
            text, ok = f"Error: {ra.tool} did not finish within {ra.timeout:g}s", False
        finally:
            stop_tracking_outputs(token)
        ok = ok and not is_error_text(text)
        files = written_since(paths, started)
    finally:
        await _close(clients)

    if ra.as_json:
        out: dict[str, Any] = {"tool": ra.tool, "args": call_args, "ok": ok, "text": text}
        if files:
            out["files"] = files
        json.dump(out, stdout, ensure_ascii=False)
        stdout.write("\n")
    else:
        stdout.write(text + "\n")
    return EXIT_OK if ok else EXIT_TOOL_ERROR


def main(argv: Sequence[str], stdin: IO[str] | None = None, stdout: IO[str] | None = None,
         stderr: IO[str] | None = None) -> int:
    """Run one headless command; returns the process exit code."""
    stdin, stdout, stderr = stdin or sys.stdin, stdout or sys.stdout, stderr or sys.stderr
    try:
        with _quiet_logging():
            if argv[0] == "tools":
                return asyncio.run(cmd_tools(argv[1:], stdout))
            return asyncio.run(cmd_run(argv[1:], stdin, stdout, stderr))
    except UsageError as e:
        stderr.write(f"zbbx-mcp: {e}\n")
        return EXIT_USAGE
    except ValueError as e:      # configuration: missing URL/token, bad policy
        stderr.write(f"zbbx-mcp: {e}\n")
        return EXIT_USAGE
    except KeyboardInterrupt:
        return 130
