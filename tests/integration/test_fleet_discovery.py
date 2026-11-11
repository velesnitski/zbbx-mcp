"""End-to-end: the cached fleet-wide NIC discovery against a real Zabbix (ADR 166).

The seeded fleet is far below the fleet threshold, so the MCP subprocess tests
never take this path; here the threshold is lowered in-process so a real
7.0 / 7.4 / 8.0 server answers the discovery (``templated: false``, no
``hostids``) and the value read by item id.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.integration.seed import KEY_ETH0, KEY_QUOTED
from zbbx_mcp import fetch
from zbbx_mcp.client import ZabbixClient
from zbbx_mcp.config import ZabbixConfig

pytestmark = pytest.mark.integration


def test_fleet_discovery_finds_the_seeded_nics_and_reads_their_values(seed_state, monkeypatch):
    monkeypatch.setattr(fetch, "FLEET_DISCOVERY_MIN_HOSTS", 1)
    seeded = {it["itemid"] for it in seed_state["items"] if it["key"] in (KEY_ETH0, KEY_QUOTED)}

    async def run():
        client = ZabbixClient(ZabbixConfig(url=seed_state["url"], token=seed_state["token"]))
        try:
            hostids = list(seed_state["hostids"].values())
            first = await fetch.physical_traffic_items(
                client, hostids, output=("itemid", "hostid", "lastvalue", "lastclock"))
            again = await fetch.physical_traffic_items(
                client, hostids, output=("itemid", "hostid", "lastvalue", "lastclock"))
            return first, again
        finally:
            await client.close()

    first, again = asyncio.run(run())
    assert {it["itemid"] for it in first} == seeded
    assert all("lastvalue" in it and "lastclock" in it for it in first)
    assert {it["itemid"] for it in again} == seeded
