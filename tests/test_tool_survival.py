"""Survival sweep: every registered tool, called with only its required
arguments, survives an empty Zabbix and a failing Zabbix.

Two fakes per tool: a ``RecordingClient`` whose every method returns ``[]``
(an instance with nothing in it), and a client whose ``call`` raises
``httpx.HTTPError`` (an instance that is down). A tool must answer both
with text — an empty table, a "nothing found" line, an error message — and
never with a traceback. Write tools are swept too, with ``ZABBIX_READ_ONLY``
unset: the fake records and performs nothing. File output is confined to
``tmp_path`` and ``HOME`` points there, so nothing outside the test is
touched; sockets are refused, so nothing leaves the machine.

Required arguments are filled from the signature with neutral values (see
``_neutral``). Fixtures are synthetic.
"""

from __future__ import annotations

import asyncio
import inspect
import socket
import urllib.request

import httpx
import pytest

import zbbx_mcp.tools as tools_pkg
from tests.wiretest import CaptureMCP, RecordingClient, StubResolver
from zbbx_mcp.client import ZabbixClient
from zbbx_mcp.rollback import RollbackLog
from zbbx_mcp.tools import ALL_TOOLS, WRITE_TOOLS


def _registered_modules() -> list:
    """The module list ``register_all`` iterates, read from the package."""
    src = inspect.getsource(tools_pkg.register_all)
    body = src.split("modules = [", 1)[1].split("]", 1)[0]
    names = [n.strip() for n in body.replace("\n", " ").split(",") if n.strip()]
    return [getattr(tools_pkg, n) for n in names]


def _capture(client) -> dict:
    mcp = CaptureMCP()
    for module in _registered_modules():
        module.register(mcp, StubResolver(client))
    return mcp.fns


TOOL_NAMES = sorted(_capture(RecordingClient()))
_ERROR_WORDS = ("error", "failed", "unavailable", "could not", "cannot")

# Tools whose signature cannot be satisfied generically. Each entry says why;
# keep it empty unless a tool genuinely needs a hand-built argument.
ALLOW_LIST: dict[str, str] = {}

_ID_SUFFIXES = ("_id", "_ids", "id", "ids", "index")
_COUNTRY_NAMES = ("country", "countries", "cc", "region")


def _neutral(name: str, annotation, tmp_path) -> object:
    ann = annotation if isinstance(annotation, str) else getattr(annotation, "__name__", str(annotation))
    if ann == "int":
        return 1
    if ann == "float":
        return 1.0
    if ann == "bool":
        return False
    if "path" in name or name == "source":
        return str(tmp_path / "srv-aq9001.csv")
    if name.endswith(_ID_SUFFIXES):
        return "1"
    if name in _COUNTRY_NAMES:
        return "aq"
    return "srv-aq9001"


def _required_kwargs(fn, tmp_path) -> dict:
    out = {}
    for p in inspect.signature(fn).parameters.values():
        if p.default is inspect.Parameter.empty:
            out[p.name] = _neutral(p.name, p.annotation, tmp_path)
    return out


# API verbs that change state. Zabbix answers every one of them with an
# object (``{"hostids": [...]}``, ``true`` for an import), never with a list,
# so the empty instance answers them with ``{}``: an empty object is the
# neutral write reply, ``[]`` is not a shape Zabbix can return there.
_WRITE_VERBS = frozenset({
    "create", "update", "delete", "massadd", "massremove", "massupdate",
    "execute", "acknowledge", "import",
})


class EmptyClient(RecordingClient):
    """An instance with nothing in it: reads return ``[]``, writes ``{}``.

    Borrows the real client's rollback surface (``rollback_log``,
    ``snapshot_and_record``, ``record_create``) so write tools journal exactly
    as they would in production — into memory, never to a server.
    """

    frontend_url = "https://zabbix.example.com"

    def __init__(self):
        super().__init__()
        self.rollback_log = RollbackLog()

    async def call(self, method, params):
        self.calls.append((method, params))
        return {} if method.rsplit(".", 1)[-1] in _WRITE_VERBS else []

    snapshot = ZabbixClient.snapshot
    snapshot_and_record = ZabbixClient.snapshot_and_record
    record_create = ZabbixClient.record_create
    call_many = ZabbixClient.call_many


class FailingClient(EmptyClient):
    """Every API call fails the way a down instance fails."""

    async def call(self, method, params):
        self.calls.append((method, params))
        raise httpx.HTTPError(f"connection refused during {method}")


@pytest.fixture(autouse=True)
def _sandbox(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ZBBX_FILE_ROOTS", str(tmp_path))
    for var in ("ZABBIX_READ_ONLY", "ZABBIX_SLACK_WEBHOOK", "SLACK_WEBHOOK_URL",
                "ZABBIX_CROSSCHECK_FACTS", "ZABBIX_APP_MAP"):
        monkeypatch.delenv(var, raising=False)

    def _no_net(*a, **k):
        raise OSError("network disabled in the survival sweep")

    monkeypatch.setattr(socket, "getaddrinfo", _no_net)
    monkeypatch.setattr(socket, "gethostbyname_ex", _no_net)
    monkeypatch.setattr(socket.socket, "connect", _no_net)
    monkeypatch.setattr(urllib.request, "urlopen", _no_net)
    monkeypatch.chdir(tmp_path)


def _run(name, client, tmp_path):
    fn = _capture(client)[name]
    return asyncio.run(fn(**_required_kwargs(fn, tmp_path)))


def test_sweep_covers_every_registered_tool():
    assert set(TOOL_NAMES) == set(ALL_TOOLS)
    assert set(TOOL_NAMES) >= WRITE_TOOLS
    assert set(ALLOW_LIST) <= set(TOOL_NAMES)


@pytest.mark.parametrize("name", TOOL_NAMES)
def test_tool_survives_empty_instance(name, tmp_path):
    if name in ALLOW_LIST:
        pytest.skip(ALLOW_LIST[name])
    client = EmptyClient()
    out = _run(name, client, tmp_path)
    assert isinstance(out, (str, dict)), type(out)
    assert out, "a tool must say something, even about nothing"
    if isinstance(out, str):
        assert "Traceback" not in out


@pytest.mark.parametrize("name", TOOL_NAMES)
def test_tool_survives_failing_instance(name, tmp_path):
    if name in ALLOW_LIST:
        pytest.skip(ALLOW_LIST[name])
    client = FailingClient()
    out = _run(name, client, tmp_path)
    assert isinstance(out, (str, dict)), type(out)
    text = out if isinstance(out, str) else repr(out)
    assert "Traceback" not in text
    if client.calls:  # the failure was reached, so the answer must name it
        assert any(w in text.lower() for w in _ERROR_WORDS), text[:300]
