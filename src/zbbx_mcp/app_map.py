"""The product's catalogue as a map the tool is GIVEN, not one it infers (ADR 143).

Every "what serves X" question is really "what does the client app offer
under X". Zabbix has three different notions of X — the code in a hostname,
the datacenter an IP resolves to, the host group — and none of them is the
product's. The product's own catalogue (audiences → sections → entries →
member servers by IP) lives in its database; a private exporter writes it
to a local JSON file, and this module reads that file. Same pattern as
``ZABBIX_DATACENTER_CIDRS`` (ADR 122): public code knows only the SHAPE,
the contents live in a gitignored ``*.local.json``.

Shape (generic vocabulary; example values are placeholders)::

    {
      "generated_at": "2026-01-01T00:00:00Z",
      "source": "db",
      "predicate": "active and heartbeat within 300 s",
      "audiences": {
        "default": {"sections": [
          {"name": "Free", "entries": [
            {"key": "aq_free", "title": "Base – Free", "code": "AQ",
             "members": [{"ip": "198.51.100.1", "load": 0.2, "clients": 3},
                         {"ip": "198.51.100.2"}],
             "display_load": 0.04}
          ]}
        ]}
      }
    }

``members[].load``, ``members[].clients`` and ``display_load`` are optional.
``code`` is a 2-letter code or ``"auto"``.

The loader fails CLOSED, as ``get_extra_dc_nets`` does: unusable input means
no map at all, never a partial one — a half-read catalogue would report some
entries against the product's data and silently omit the rest. It is NOT
cached: the exporter rewrites the file on its own schedule and a stale copy
held in memory would defeat the age check. No imports from ``tools/``.
"""

from __future__ import annotations

import json
import os
import re
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, timezone

APP_MAP_ENV = "ZABBIX_APP_MAP"

#: Window assumed when the predicate carries no time figure of its own.
DEFAULT_PREDICATE_WINDOW_S = 15 * 60

_WINDOW_RX = re.compile(r"(\d+)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes)\b", re.I)


@dataclass(frozen=True)
class Member:
    ip: str
    load: float | None = None
    clients: int | None = None


@dataclass(frozen=True)
class Entry:
    key: str
    title: str
    code: str
    members: tuple[Member, ...] = ()
    display_load: float | None = None


@dataclass(frozen=True)
class Section:
    name: str
    entries: tuple[Entry, ...] = ()


@dataclass(frozen=True)
class Audience:
    name: str
    sections: tuple[Section, ...] = ()


@dataclass(frozen=True)
class AppMap:
    generated_at: datetime
    source: str = ""
    predicate: str = ""
    audiences: dict[str, Audience] = field(default_factory=dict)


def _opt_float(v, what: str) -> float | None:
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise ValueError(f"{what} must be a number")
    try:
        return float(v)
    except ValueError as e:
        raise ValueError(f"{what} must be a number") from e


def _opt_int(v, what: str) -> int | None:
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise ValueError(f"{what} must be an integer")
    try:
        return int(float(v))
    except ValueError as e:
        raise ValueError(f"{what} must be an integer") from e


def _parse_generated_at(raw) -> datetime:
    """ISO-8601 → aware UTC datetime. A map whose age cannot be known is one
    the tool cannot vouch for, so the field is required."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("generated_at is missing")
    s = raw.strip()
    if s.endswith(("Z", "z")):  # fromisoformat accepts "Z" only from 3.11
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as e:
        raise ValueError(f"generated_at is not ISO-8601: {raw!r}") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_member(raw, where: str) -> Member:
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: member must be an object")
    ip = raw.get("ip")
    if not isinstance(ip, str) or not ip.strip():
        raise ValueError(f"{where}: member without an ip")
    return Member(
        ip=ip.strip(),
        load=_opt_float(raw.get("load"), f"{where}: member {ip} load"),
        clients=_opt_int(raw.get("clients"), f"{where}: member {ip} clients"),
    )


def _parse_entry(raw, where: str) -> Entry:
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: entry must be an object")
    key = raw.get("key")
    if not isinstance(key, str) or not key.strip():
        raise ValueError(f"{where}: entry without a key")
    key = key.strip()
    members = raw.get("members", [])
    if not isinstance(members, list):
        raise ValueError(f"{where}/{key}: members must be a list")
    return Entry(
        key=key,
        title=str(raw.get("title") or key),
        code=str(raw.get("code") or "").strip(),
        members=tuple(_parse_member(m, f"{where}/{key}") for m in members),
        display_load=_opt_float(raw.get("display_load"), f"{where}/{key}: display_load"),
    )


def _parse_section(raw, where: str) -> Section:
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: section must be an object")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"{where}: section without a name")
    name = name.strip()
    entries = raw.get("entries", [])
    if not isinstance(entries, list):
        raise ValueError(f"{where}/{name}: entries must be a list")
    return Section(name=name, entries=tuple(_parse_entry(e, f"{where}/{name}") for e in entries))


def parse_app_map(data) -> AppMap:
    """Validate the raw JSON object and build an :class:`AppMap`. Pure.

    Raises ``ValueError`` naming the first thing that is wrong. Any defect
    rejects the whole map — see the module docstring for why.
    """
    if not isinstance(data, dict):
        raise ValueError("top level must be an object")
    audiences_raw = data.get("audiences")
    if not isinstance(audiences_raw, dict) or not audiences_raw:
        raise ValueError("audiences must be a non-empty object")
    audiences: dict[str, Audience] = {}
    for name, body in audiences_raw.items():
        if not isinstance(body, dict):
            raise ValueError(f"audience {name}: must be an object")
        sections = body.get("sections", [])
        if not isinstance(sections, list):
            raise ValueError(f"audience {name}: sections must be a list")
        audiences[str(name)] = Audience(
            name=str(name),
            sections=tuple(_parse_section(s, f"audience {name}") for s in sections),
        )
    return AppMap(
        generated_at=_parse_generated_at(data.get("generated_at")),
        source=str(data.get("source") or ""),
        predicate=str(data.get("predicate") or ""),
        audiences=audiences,
    )


def load_app_map(source: str | None = None) -> tuple[AppMap | None, str]:
    """``(map, "")`` or ``(None, reason)``. Reads ``ZABBIX_APP_MAP`` unless
    ``source`` is given (a file path or inline JSON).

    Anything that starts with ``{`` or ``[`` is inline JSON; anything else is a path.
    The reason string is meant to be shown to the caller verbatim, so it
    says which of the two was tried and what went wrong.
    """
    raw = (os.environ.get(APP_MAP_ENV, "") if source is None else source).strip()
    if not raw:
        return None, f"{APP_MAP_ENV} is not set"
    inline = raw.startswith(("{", "["))
    try:
        if inline:
            data = json.loads(raw)
        else:
            if not os.path.isfile(raw):
                return None, f"file not found: {raw}"
            with open(raw) as fh:
                data = json.load(fh)
        return parse_app_map(data), ""
    except json.JSONDecodeError as e:
        return None, f"invalid JSON in {'inline value' if inline else raw}: {e}"
    except OSError as e:
        return None, f"cannot read {raw}: {e}"
    except (ValueError, TypeError, AttributeError) as e:
        return None, f"wrong shape: {e}"


def map_age_s(app_map: AppMap, now: float | None = None) -> float:
    """Seconds since ``generated_at``; never negative. Pure given ``now``."""
    ts = now if now is not None else _time.time()
    return max(0.0, ts - app_map.generated_at.timestamp())


def predicate_window_s(predicate: str) -> int:
    """The freshness window the predicate names, in seconds. Pure.

    ``"heartbeat within 300 s"`` → 300; ``"seen in the last 5 min"`` → 300.
    Without a figure the default (15 minutes) applies: the exporter's
    predicate is the only statement of how current its rows are, and a map
    older than that window describes a catalogue that has since moved on.
    """
    m = _WINDOW_RX.search(predicate or "")
    if not m:
        return DEFAULT_PREDICATE_WINDOW_S
    n, unit = int(m.group(1)), m.group(2).lower()
    return n * 60 if unit.startswith("m") else n


def index_hosts_by_ip(hosts: list[dict]) -> dict[str, dict]:
    """``{ip: host}`` over EVERY interface of every host. Pure.

    ``host_ip`` in ``data.py`` takes the first non-loopback interface, which
    is right for "where is this box" and wrong for a join: the product lists
    a server by whichever address it hands to clients, and that need not be
    the interface Zabbix lists first. The first host to claim an address
    keeps it, so two hosts sharing one IP resolve deterministically.
    """
    by_ip: dict[str, dict] = {}
    for h in hosts:
        for i in h.get("interfaces", []) or []:
            ip = (i.get("ip") or "").strip()
            if ip and ip != "127.0.0.1":
                by_ip.setdefault(ip, h)
    return by_ip


def join_entry(entry: Entry, by_ip: dict[str, dict]) -> tuple[list[dict], list[str]]:
    """``(matched hosts, unmatched member ips)`` for one entry. Pure.

    Hosts are de-duplicated by ``hostid`` (a host reachable on two member
    addresses is one server), in member order. An unmatched address is
    returned by value so the caller can NAME it: the product offers a
    server that Zabbix does not monitor, and that is a finding, not a zero.
    """
    matched: list[dict] = []
    seen: set[str] = set()
    unmatched: list[str] = []
    for m in entry.members:
        h = by_ip.get(m.ip)
        if h is None:
            unmatched.append(m.ip)
            continue
        hid = str(h.get("hostid", ""))
        if hid not in seen:
            seen.add(hid)
            matched.append(h)
    return matched, unmatched
