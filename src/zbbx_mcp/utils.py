"""Shared utilities for tool modules."""

import os
import re
import time
from collections.abc import Callable
from datetime import datetime, timezone

from zbbx_mcp.client import ZabbixClient


_RELATIVE_RE = re.compile(r"^\s*(\d+)\s*([smhdw])\s*$", re.IGNORECASE)
_RELATIVE_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_time(value: str | int | float, now: int | None = None) -> int:
    """Parse a time input into a Unix epoch timestamp (seconds).

    Accepts:
      - epoch int or numeric string ("1715000000")
      - ISO date ("2026-04-19")
      - ISO datetime ("2026-04-19T10:30:00", "2026-04-19 10:30:00")
      - relative duration ("24h", "7d", "30m", "1w", "90s") — subtracted from now

    Raises ValueError on unrecognized input.
    """
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip()
    if not s:
        raise ValueError("empty time value")

    if s.lstrip("-").isdigit():
        return int(s)

    m = _RELATIVE_RE.match(s)
    if m:
        qty = int(m.group(1))
        unit = m.group(2).lower()
        base = int(time.time()) if now is None else now
        return base - qty * _RELATIVE_UNITS[unit]

    iso = s.replace(" ", "T")
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError as e:
        raise ValueError(f"unrecognized time: {value!r}") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def format_results(
    data: list,
    formatter: Callable[[list], str],
    label: str,
    max_results: int,
) -> str:
    """Format API results with consistent header and truncation notice."""
    result = formatter(data)
    count = len(data)
    if count == 0:
        return result
    header = f"**Found: {count} {label}**"
    if count >= max_results:
        header += f" (showing first {max_results}, more may exist)"
    return f"{header}\n\n{result}"


async def resolve_group_ids(client: ZabbixClient, group: str) -> list[str] | None:
    """Resolve a host group name to group IDs.

    Returns list of group IDs, or None if group not found.
    """
    groups = await client.call("hostgroup.get", {
        "output": ["groupid"],
        "filter": {"name": [group]},
    })
    if not groups:
        return None
    return [g["groupid"] for g in groups]


_SAFE_OUTPUT_DIRS = frozenset({
    os.path.expanduser("~/Downloads"),
    os.path.expanduser("~/Documents"),
    os.path.expanduser("~/Desktop"),
    "/tmp",
})


def safe_output_path(output_dir: str, filename: str) -> str:
    """Validate and resolve output path. Restricts to safe directories."""
    path = os.path.realpath(os.path.expanduser(output_dir))
    if not any(path.startswith(safe) for safe in _SAFE_OUTPUT_DIRS):
        raise ValueError(
            f"Output directory '{output_dir}' not in allowed paths. "
            f"Use ~/Downloads, ~/Documents, ~/Desktop, or /tmp."
        )
    os.makedirs(path, exist_ok=True)
    return os.path.join(path, filename)


# Read-only fields to strip when restoring Zabbix objects (rollback)
ROLLBACK_STRIP_FIELDS = frozenset({
    "lastchange", "flags", "templateid", "state",
    "error", "lastclock", "lastns", "lastvalue",
    "prevvalue", "evaltype",
})
