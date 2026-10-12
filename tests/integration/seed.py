"""Seed a throwaway Zabbix 7.0 container with a small synthetic fleet.

Run against the stack in ``tests/integration/docker-compose.yml`` only::

    uv run python tests/integration/seed.py --url http://localhost:8080

It logs in with the official image's default administrator account (the
public default every fresh ``zabbix-web`` container ships with). That login
is only ever used against the disposable container this suite brings up and
tears down; nothing here is a credential for any real system.

What it creates (idempotent — a re-run reuses existing objects):

* host groups ``app_free`` / ``app_paid``
* hosts ``srv-aq9001`` .. ``srv-aq9004``, ``srv-bv9001`` and the sub-host
  ``srv-aq9001 aq9002`` (a ``parent label`` compound name, ADR 146), each
  with an agent interface on 192.0.2.0/24 and a ``{$COST_MONTH}`` macro
* trapper items ``net.if.in[eth0]``, ``net.if.in["enp3s0"]`` (the quoted key
  form, ADR 147) and ``system.cpu.util[,idle]``
* one trigger, on ``srv-aq9001``, that the pushed values fire
* an API token for the MCP under test

Values are pushed with ``history.push`` (Zabbix 7.0 API), falling back to the
trapper protocol spoken in pure Python. Hourly trends for the last week are
written as SQL (``--trends-sql``) for the caller to load with ``psql``: the
server only computes trends for the hour it is in, and the trend-based tools
need a baseline.

The resulting ids and the token are written as JSON to ``--state`` (default
``default_state_path()``), where ``tests/integration/conftest.py`` reads them.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import struct
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

__all__ = ["FLEET", "TRIGGER_NAME", "default_state_path", "main"]

# Public default of the official zabbix-web image; overridable for a stack
# started with other settings. Never a real account.
_DEFAULT_ADMIN_USER = "Admin"
_DEFAULT_ADMIN_PASSWORD = "zabbix"

TOKEN_NAME = "zbbx-mcp-integration"
TRIGGER_NAME = "CPU idle below 5% on {HOST.NAME}"

KEY_ETH0 = "net.if.in[eth0]"
KEY_QUOTED = 'net.if.in["enp3s0"]'
KEY_CPU_IDLE = "system.cpu.util[,idle]"

# host -> group, interface ip, traffic key, traffic (bits/s), cpu idle (%) or None
FLEET: dict[str, dict] = {
    "srv-aq9001": {"group": "app_paid", "ip": "192.0.2.11", "key": KEY_ETH0,
                   "traffic": 400_000_000, "idle": 2.0, "cost": "100"},
    "srv-aq9001 aq9002": {"group": "app_paid", "ip": "192.0.2.21", "key": KEY_ETH0,
                          "traffic": 100_000_000, "idle": None, "cost": None},
    "srv-aq9002": {"group": "app_paid", "ip": "192.0.2.12", "key": KEY_ETH0,
                   "traffic": 300_000_000, "idle": 70.0, "cost": "100"},
    "srv-aq9003": {"group": "app_free", "ip": "192.0.2.13", "key": KEY_ETH0,
                   "traffic": 200_000_000, "idle": 80.0, "cost": "50"},
    "srv-aq9004": {"group": "app_free", "ip": "192.0.2.14", "key": KEY_ETH0,
                   "traffic": 50_000_000, "idle": 90.0, "cost": "50"},
    "srv-bv9001": {"group": "app_free", "ip": "192.0.2.15", "key": KEY_QUOTED,
                   "traffic": 250_000_000, "idle": 85.0, "cost": "50"},
}
TRIGGER_HOST = "srv-aq9001"

VALUE_TYPE_FLOAT = 0
VALUE_TYPE_UINT = 3
ITEM_TYPE_TRAPPER = 2


def default_state_path() -> Path:
    """Where the seed state lives unless ``ZBBX_IT_STATE`` says otherwise."""
    raw = os.environ.get("ZBBX_IT_STATE", "")
    return Path(raw) if raw else Path(tempfile.gettempdir()) / "zbbx-it-seed.json"


class Api:
    """Minimal synchronous JSON-RPC client for the Zabbix web API."""

    def __init__(self, url: str):
        self.endpoint = url.rstrip("/") + "/api_jsonrpc.php"
        self.session = ""
        self._id = 0

    def call(self, method: str, params: dict | list | None = None, auth: bool = True):
        self._id += 1
        body = json.dumps({"jsonrpc": "2.0", "method": method,
                           "params": params if params is not None else {}, "id": self._id})
        headers = {"Content-Type": "application/json-rpc"}
        if auth and self.session:
            headers["Authorization"] = f"Bearer {self.session}"
        req = urllib.request.Request(self.endpoint, data=body.encode(), headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            reply = json.loads(resp.read().decode())
        if "error" in reply:
            err = reply["error"]
            raise RuntimeError(f"{method}: {err.get('code')} {err.get('message')} {err.get('data')}")
        return reply["result"]


def wait_for_api(api: Api, user: str, password: str, timeout_s: float) -> str:
    """Poll until the frontend answers and the database accepts a login."""
    deadline = time.monotonic() + timeout_s
    last = ""
    while time.monotonic() < deadline:
        try:
            version = api.call("apiinfo.version", {}, auth=False)
            api.session = api.call("user.login", {"username": user, "password": password}, auth=False)
            return str(version)
        except (OSError, urllib.error.URLError, RuntimeError, ValueError) as e:
            last = str(e)
            time.sleep(3)
    raise SystemExit(f"Zabbix API not ready after {timeout_s:.0f}s: {last}")


def _group_ids(api: Api) -> dict[str, str]:
    out = {}
    for name in sorted({h["group"] for h in FLEET.values()}):
        found = api.call("hostgroup.get", {"output": ["groupid"], "filter": {"name": [name]}})
        if found:
            out[name] = found[0]["groupid"]
        else:
            out[name] = api.call("hostgroup.create", {"name": name})["groupids"][0]
    return out


def _host_id(api: Api, name: str, spec: dict, groupid: str) -> str:
    found = api.call("host.get", {"output": ["hostid"], "filter": {"host": [name]}})
    if found:
        return found[0]["hostid"]
    params: dict = {
        "host": name,
        "groups": [{"groupid": groupid}],
        "interfaces": [{"type": 1, "main": 1, "useip": 1, "ip": spec["ip"],
                        "dns": "", "port": "10050"}],
    }
    if spec["cost"]:
        params["macros"] = [{"macro": "{$COST_MONTH}", "value": spec["cost"]}]
    return api.call("host.create", params)["hostids"][0]


def _item_id(api: Api, hostid: str, key: str, value_type: int, units: str) -> str:
    found = api.call("item.get", {"output": ["itemid"], "hostids": [hostid], "filter": {"key_": [key]}})
    if found:
        return found[0]["itemid"]
    return api.call("item.create", {
        "hostid": hostid, "name": f"Trapper {key}", "key_": key,
        "type": ITEM_TYPE_TRAPPER, "value_type": value_type, "units": units,
        "history": "7d", "trends": "365d",
    })["itemids"][0]


def _ensure_trigger(api: Api, hostid: str) -> str:
    found = api.call("trigger.get", {"output": ["triggerid"], "hostids": [hostid],
                                     "filter": {"description": [TRIGGER_NAME]}})
    if found:
        return found[0]["triggerid"]
    return api.call("trigger.create", {
        "description": TRIGGER_NAME,
        "expression": f"last(/{TRIGGER_HOST}/{KEY_CPU_IDLE})<5",
        "priority": 4,
    })["triggerids"][0]


def _create_token(api: Api, user: str) -> str:
    """A fresh API token for the MCP. An older one of the same name is removed."""
    old = api.call("token.get", {"output": ["tokenid"], "filter": {"name": TOKEN_NAME}})
    if old:
        api.call("token.delete", [t["tokenid"] for t in old])
    uid = api.call("user.get", {"output": ["userid"], "filter": {"username": [user]}})[0]["userid"]
    tid = api.call("token.create", {"name": TOKEN_NAME, "userid": uid})["tokenids"][0]
    return api.call("token.generate", [tid])[0]["token"]


def _sender_push(server: str, port: int, rows: list[dict]) -> str:
    """Trapper protocol ("sender data") in pure Python; returns the server's info line."""
    payload = json.dumps({"request": "sender data", "data": rows}).encode()
    packet = b"ZBXD\x01" + struct.pack("<Q", len(payload)) + payload
    with socket.create_connection((server, port), timeout=15) as s:
        s.sendall(packet)
        head = s.recv(13)
        size = struct.unpack("<Q", head[5:13])[0]
        data = b""
        while len(data) < size:
            chunk = s.recv(size - len(data))
            if not chunk:
                break
            data += chunk
    return json.loads(data.decode()).get("info", "")


def push_values(api: Api, items: list[dict], sender: tuple[str, int], timeout_s: float = 120) -> str:
    """Push each item's value at three recent clocks; retry until the server knows every item.

    ``items`` rows carry itemid, host, key and value. A freshly created item
    is unknown to the server until its configuration cache syncs, so a
    rejected row is retried rather than reported.
    """
    now = int(time.time())
    clocks = (now - 120, now - 60, now - 5)
    pending = list(items)
    deadline = time.monotonic() + timeout_s
    use_sender = False
    while pending and time.monotonic() < deadline:
        if not use_sender:
            rows = [{"itemid": it["itemid"], "value": str(it["value"]), "clock": c, "ns": 0}
                    for it in pending for c in clocks]
            try:
                result = api.call("history.push", rows)
            except RuntimeError as e:
                if "-32601" in str(e) or "Incorrect method" in str(e):
                    use_sender = True
                    continue
                raise
            per_row = result.get("data", [])
            failed = {rows[i]["itemid"] for i, r in enumerate(per_row) if "error" in r}
            pending = [it for it in pending if it["itemid"] in failed]
        else:
            rows = [{"host": it["host"], "key": it["key"], "value": str(it["value"]),
                     "clock": c, "ns": 0} for it in pending for c in clocks]
            info = _sender_push(sender[0], sender[1], rows)
            if "failed: 0" in info:
                pending = []
        if pending:
            time.sleep(3)
    if pending:
        raise SystemExit(f"Server never accepted values for: {[it['key'] for it in pending]}")
    return "sender" if use_sender else "history.push"


def trends_sql(items: list[dict], now: int | None = None, days: int = 7) -> str:
    """Hourly trend rows for the last ``days``, stopping two hours short of now.

    The server writes the current hour's trend itself; leaving a two-hour gap
    keeps these rows from ever colliding with it.
    """
    now = int(time.time()) if now is None else now
    last_hour = (now // 3600) * 3600 - 2 * 3600
    first_hour = last_hour - days * 86400
    lines = ["BEGIN;"]
    for it in items:
        table = "trends_uint" if it["value_type"] == VALUE_TYPE_UINT else "trends"
        v = it["value"]
        lo, hi = (int(v * 0.9), int(v * 1.1)) if table == "trends_uint" else (v * 0.9, min(v * 1.1, 100.0))
        values = ",\n".join(
            f"({it['itemid']},{h},60,{lo},{v},{hi})" for h in range(first_hour, last_hour + 1, 3600)
        )
        lines.append(
            f"INSERT INTO {table} (itemid,clock,num,value_min,value_avg,value_max) VALUES\n"
            f"{values}\nON CONFLICT DO NOTHING;"
        )
    lines.append("COMMIT;")
    return "\n".join(lines) + "\n"


def wait_for_problem(api: Api, hostid: str, timeout_s: float = 120) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if api.call("problem.get", {"output": ["eventid"], "hostids": [hostid]}):
            return
        time.sleep(3)
    raise SystemExit("The seeded trigger never produced a problem.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default=os.environ.get("ZBBX_IT_URL", "http://localhost:8080"))
    ap.add_argument("--state", default=str(default_state_path()))
    ap.add_argument("--trends-sql", default="", help="write hourly trend INSERTs here")
    ap.add_argument("--wait", type=float, default=300, help="seconds to wait for the API")
    ap.add_argument("--sender-host", default="127.0.0.1")
    ap.add_argument("--sender-port", type=int, default=10051)
    args = ap.parse_args(argv)

    user = os.environ.get("ZBBX_IT_ADMIN_USER", _DEFAULT_ADMIN_USER)
    password = os.environ.get("ZBBX_IT_ADMIN_PASSWORD", _DEFAULT_ADMIN_PASSWORD)

    api = Api(args.url)
    version = wait_for_api(api, user, password, args.wait)
    print(f"Zabbix API {version} ready")

    groups = _group_ids(api)
    hostids: dict[str, str] = {}
    items: list[dict] = []
    for name, spec in FLEET.items():
        hid = _host_id(api, name, spec, groups[spec["group"]])
        hostids[name] = hid
        items.append({"host": name, "key": spec["key"], "value": spec["traffic"],
                      "value_type": VALUE_TYPE_UINT,
                      "itemid": _item_id(api, hid, spec["key"], VALUE_TYPE_UINT, "bps")})
        if spec["idle"] is not None:
            items.append({"host": name, "key": KEY_CPU_IDLE, "value": spec["idle"],
                          "value_type": VALUE_TYPE_FLOAT,
                          "itemid": _item_id(api, hid, KEY_CPU_IDLE, VALUE_TYPE_FLOAT, "%")})
    triggerid = _ensure_trigger(api, hostids[TRIGGER_HOST])
    token = _create_token(api, user)
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::add-mask::{token}")

    via = push_values(api, items, (args.sender_host, args.sender_port))
    print(f"Pushed {len(items)} items via {via}")
    wait_for_problem(api, hostids[TRIGGER_HOST])
    print("Trigger fired")

    if args.trends_sql:
        Path(args.trends_sql).write_text(trends_sql(items))
        print(f"Trend SQL written to {args.trends_sql}")

    state = {"url": args.url, "token": token, "version": version, "hostids": hostids,
             "groupids": groups, "triggerid": triggerid,
             "items": [{k: it[k] for k in ("host", "key", "itemid")} for it in items]}
    path = Path(args.state)
    path.write_text(json.dumps(state, indent=2))
    os.chmod(path, 0o600)
    print(f"State written to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
