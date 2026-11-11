"""Short-lived cache for Zabbix *configuration* reads (ADR 166).

Measured on a large fleet, the costliest call after ``trend.get`` was the
fleet-wide item search that finds every host's network-interface items:
seconds per tool call, repeated by every tool, sometimes twice in one
call, for data that changes when someone edits a template — not between two
questions a minute apart.

This module caches such reads for a few minutes and coalesces concurrent
identical ones into a single request. It is for configuration only (which
items, hosts, keys exist), never for values: anything that reads
``lastvalue``, trends or history goes to the server every time. Any write
through the client clears the cache (``ZabbixClient.call``), so a tool that
creates an item and then looks for it sees it.

No ``tools/`` imports.
"""

from __future__ import annotations

import asyncio
import json
import os

DEFAULT_TTL_S = 300.0


def config_ttl() -> float:
    """Seconds a configuration read stays cached (``ZABBIX_CONFIG_CACHE_TTL``,
    ``0`` disables). An unparsable value falls back to the default."""
    raw = os.environ.get("ZABBIX_CONFIG_CACHE_TTL", "").strip()
    if not raw:
        return DEFAULT_TTL_S
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_TTL_S


def _key(method: str, params: dict) -> str:
    return "cfg:" + method + ":" + json.dumps(params, sort_keys=True, separators=(",", ":"))


def is_warm(client, method: str, params: dict) -> bool:
    """True when the read is cached and fresh, so using it costs nothing."""
    ttl = config_ttl()
    return ttl > 0 and client._get_cached(_key(method, params), ttl=ttl) is not None


async def cached_config(client, method: str, params: dict) -> list:
    """``client.call(method, params)``, cached for :func:`config_ttl` seconds,
    with concurrent identical calls sharing one request."""
    ttl = config_ttl()
    if ttl <= 0:
        return list(await client.call(method, params) or [])
    key = _key(method, params)
    cached = client._get_cached(key, ttl=ttl)
    if cached is not None:
        return cached
    inflight: dict = vars(client).setdefault("_cfg_inflight", {})
    pending = inflight.get(key)
    if pending is not None:
        return await asyncio.shield(pending)
    task = asyncio.ensure_future(client.call(method, params))
    inflight[key] = task
    try:
        result = list(await task or [])
    finally:
        inflight.pop(key, None)
    client._set_cache(key, result)
    return result
