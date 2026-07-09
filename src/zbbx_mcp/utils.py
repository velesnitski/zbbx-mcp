"""Shared utilities for tool modules."""

import os
import re
import tempfile
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


# --- Filesystem confinement (ADR 076) -------------------------------------
# Caller-controlled `file_path` / `output_dir` tool arguments are restricted to
# a small allowlist of roots, so a confused-deputy (prompt-injected) caller
# cannot read ~/.ssh, ~/.claude.json, /etc/*, etc., nor write outside user-data
# dirs. Mirrors the CWE-22/CWE-73 hardening from the yt-mcp advisory
# GHSA-99mq-fjjc-6v9j: realpath (symlink-safe) + commonpath (sibling-prefix-safe)
# against the roots, plus a read size cap.
_DEFAULT_ROOTS = ("~/Downloads", "~/Documents", "~/Desktop", "/tmp")
MAX_READ_BYTES = 100 * 1024 * 1024  # 100 MB cap on any caller-path read


def _allowed_roots() -> list[str]:
    """Realpath'd allowlist of roots for caller-supplied paths.

    Defaults to the user's Downloads/Documents/Desktop, /tmp, and the system
    temp dir (so pytest tmp paths resolve). Extend with ``ZBBX_FILE_ROOTS``
    (``os.pathsep``-separated absolute paths).
    """
    roots = [*_DEFAULT_ROOTS, tempfile.gettempdir()]
    extra = os.environ.get("ZBBX_FILE_ROOTS", "")
    roots += [p for p in extra.split(os.pathsep) if p.strip()]
    out: list[str] = []
    for r in roots:
        try:
            out.append(os.path.realpath(os.path.expanduser(r)))
        except OSError:
            continue
    return out


def _within_roots(resolved: str) -> bool:
    """True if realpath'd ``resolved`` sits inside an allowed root.

    Uses ``commonpath`` rather than ``startswith`` so a sibling directory like
    ``<root>-evil`` cannot slip through a prefix match.
    """
    for root in _allowed_roots():
        try:
            if os.path.commonpath([root, resolved]) == root:
                return True
        except ValueError:  # different drive / mixed absolute+relative
            continue
    return False


def confined_input_path(path: str, *, max_bytes: int = MAX_READ_BYTES) -> str:
    """Resolve a caller-supplied read path, confined to the allowed roots.

    Symlinks are resolved (realpath) *before* the root check, so a symlink
    planted inside an allowed root cannot redirect the read outside it. Raises
    ``ValueError`` if the path is empty, escapes the roots, is missing, or
    exceeds the size cap. Returns the resolved absolute path.
    """
    if not path or not str(path).strip():
        raise ValueError("empty file path")
    resolved = os.path.realpath(os.path.expanduser(str(path)))
    if not _within_roots(resolved):
        raise ValueError(
            f"Path '{path}' is outside the allowed roots (~/Downloads, "
            "~/Documents, ~/Desktop, temp). Set ZBBX_FILE_ROOTS to permit more."
        )
    if not os.path.isfile(resolved):
        raise ValueError(f"File not found: {resolved}")
    size = os.path.getsize(resolved)
    if size > max_bytes:
        raise ValueError(
            f"File too large: {size:,} bytes exceeds the {max_bytes:,}-byte cap."
        )
    return resolved


def _confined_dir(output_dir: str) -> str:
    """Realpath a caller output dir, confine it to the roots, and mkdir it."""
    path = os.path.realpath(os.path.expanduser(output_dir))
    if not _within_roots(path):
        raise ValueError(
            f"Output directory '{output_dir}' is not in the allowed roots. "
            "Use ~/Downloads, ~/Documents, ~/Desktop, temp, or set ZBBX_FILE_ROOTS."
        )
    os.makedirs(path, exist_ok=True)
    return path


def safe_output_path(output_dir: str, filename: str) -> str:
    """Validate an output dir + filename, confined to the allowed roots.

    ``filename`` must be a bare basename (no separators or traversal) so it
    cannot escape the validated directory.
    """
    if filename != os.path.basename(filename) or filename in ("", ".", ".."):
        raise ValueError(f"Unsafe output filename: {filename!r}")
    return os.path.join(_confined_dir(output_dir), filename)


def confined_output_path(path: str) -> str:
    """Resolve a caller-supplied full output *file* path, confined to the roots.

    For tools that take a complete output path rather than dir + name: confines
    the parent directory to the allowed roots (creating it) and returns the
    resolved file path.
    """
    resolved = os.path.realpath(os.path.expanduser(path))
    parent = os.path.dirname(resolved) or resolved
    if not _within_roots(parent):
        raise ValueError(
            f"Output path '{path}' is not in the allowed roots. "
            "Use ~/Downloads, ~/Documents, ~/Desktop, temp, or set ZBBX_FILE_ROOTS."
        )
    os.makedirs(parent, exist_ok=True)
    return resolved


# Read-only fields to strip when restoring Zabbix objects (rollback)
ROLLBACK_STRIP_FIELDS = frozenset({
    "lastchange", "flags", "templateid", "state",
    "error", "lastclock", "lastns", "lastvalue",
    "prevvalue", "evaltype",
})
