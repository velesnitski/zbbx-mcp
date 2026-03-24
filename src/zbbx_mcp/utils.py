"""Shared utilities for tool modules."""

from typing import Callable

from zbbx_mcp.client import ZabbixClient


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


# Read-only fields to strip when restoring Zabbix objects (rollback)
ROLLBACK_STRIP_FIELDS = frozenset({
    "lastchange", "flags", "templateid", "state",
    "error", "lastclock", "lastns", "lastvalue",
    "prevvalue", "evaltype",
})
