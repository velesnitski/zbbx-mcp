"""Problems that sit on a disabled trigger or disabled items ("frozen").

Zabbix does not close a problem when its trigger, or every item behind it,
is disabled, and ``problem.get`` cannot filter on trigger status. Such a
problem stays in the problem table forever and can never recover, so a tool
that counts open problems as current state reads it as a live outage. These
helpers identify them so callers can exclude them as monitoring debt.

Core module: no ``tools`` imports.
"""

from __future__ import annotations

import httpx

_DISABLED = "1"


async def frozen_trigger_ids(client, problems: list[dict]) -> set[str] | None:
    """Trigger ids of ``problems`` whose problem can never recover.

    One ``trigger.get`` over the distinct ``objectid``s. A trigger is frozen
    when it is disabled, when it has items and every one is disabled, or when
    it is absent from the reply (deleted or invisible). A trigger with no
    items (calculated / aggregate) is live.

    Returns ``None`` — "could not judge, keep everything live" — when the call
    fails or comes back empty for a non-empty id set: a fetch failure must
    never deflate an incident count.
    """
    tids = sorted({str(p["objectid"]) for p in problems if p.get("objectid")})
    if not tids:
        return set()
    try:
        triggers = await client.call("trigger.get", {
            "triggerids": tids,
            "output": ["triggerid", "status"],
            "selectItems": ["itemid", "status"],
        })
    except (httpx.HTTPError, ValueError, TypeError):
        return None
    if not isinstance(triggers, list) or not triggers:
        return None
    seen: set[str] = set()
    frozen: set[str] = set()
    for t in triggers:
        tid = str(t.get("triggerid", ""))
        seen.add(tid)
        items = t.get("items") or []
        if str(t.get("status", "0")) == _DISABLED or (
            items and all(str(i.get("status", "0")) == _DISABLED for i in items)
        ):
            frozen.add(tid)
    frozen.update(tid for tid in tids if tid not in seen)
    return frozen


def split_frozen(
    problems: list[dict], frozen_ids: set[str] | None,
) -> tuple[list[dict], list[dict]]:
    """Split ``problems`` into ``(live, frozen)`` by trigger id. Pure.

    ``frozen_ids`` of ``None`` (could not judge) keeps everything live.
    """
    if not frozen_ids:
        return list(problems), []
    live: list[dict] = []
    frozen: list[dict] = []
    for p in problems:
        (frozen if str(p.get("objectid", "")) in frozen_ids else live).append(p)
    return live, frozen


def frozen_note(n: int) -> str:
    """One line naming ``n`` excluded frozen problems; empty when none. Pure."""
    if n <= 0:
        return ""
    return (f"{n} problem(s) sit on disabled triggers or items and cannot recover — "
            "excluded as monitoring debt, not outages.")


async def drop_frozen(client, problems: list[dict]) -> tuple[list[dict], list[dict]]:
    """``(live, frozen)`` for ``problems`` in one call; never raises."""
    if not problems:
        return list(problems), []
    return split_frozen(problems, await frozen_trigger_ids(client, problems))
