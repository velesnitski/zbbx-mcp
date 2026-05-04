# ADR 018: Service-check stale-gate before treating lastvalue=0 as failure

**Status:** Accepted
**Date:** 2026-05-04

## Problem

Several MCP tools that read service-check items
(``KEY_service_PRIMARY`` / ``_SECONDARY`` / ``_TERTIARY``) treat
``lastvalue == 0`` as "service is down". That is the right call when
the check is fresh and the item is healthy. It is the wrong call when:

- The check item is in ``state == "1"`` (Zabbix flagged it
  unsupported — broken script, missing dependency, agent-side
  error). The lastvalue stays at whatever the agent last sent before
  the polling broke.
- The item has not been polled recently (``lastclock`` more than
  ~30 minutes old). The last successful "0" lingers indefinitely
  and reads as DOWN even when the host has been quietly fine for
  weeks.

A real ops session today found exactly this failure mode: a service
check item was unsupported, ``lastvalue`` was the stale 0 from before
the script broke, and the report counted the host as down even though
service traffic was healthy. The same gate has already shipped in
the zabbix-reports port (``fetch_vpn_failures``,
``fetch_vpn_checks``); MCP needs to match.

## Decision

A new pure helper ``is_service_check_stale(item, now, stale_sec=1800)``
in ``fetch.py`` returns True when an item should not contribute to
service up/down counting:

```python
def is_service_check_stale(item, now, stale_sec=1800):
    if str(item.get("state", "")) == "1":
        return True
    last = int(item.get("lastclock", 0) or 0)
    if last <= 0:
        return True
    return (now - last) > stale_sec
```

The helper is re-exported through ``zbbx_mcp.data`` so existing tools
that already pull shared helpers from ``data`` get it without touching
their import block.

The gate is applied at every site that consumes service-check items:

- ``fetch.fetch_service_status`` — central reusable fetcher.
- ``fetch.fetch_all_data`` — the ``_service_item_call`` variant
  fetches ``state`` + ``lastclock`` and the post-processing drops
  stale items before building the maps.
- ``tools.service_brief.generate_service_brief`` — direct caller.
- ``tools.geo_traffic.detect_regional_anomalies`` — direct caller.
- ``tools.trends_health.get_health_assessment`` — direct caller.
- ``tools.geo_health.get_service_uptime_report`` — direct caller.
- ``tools.geo_health.get_service_health_matrix`` — direct caller.

Each direct site adds ``state`` and ``lastclock`` to its
``item.get`` ``output`` and runs the items through the helper before
building the per-host map. ``get_service_uptime_report`` returns a
dedicated "all check items are stale" message rather than rendering
a mostly-empty report.

The existing traffic-validation guard in ``get_service_health_matrix``
(treat hosts with ≥5 Mbps as up regardless of check item) is left in
place as a second layer — the stale-gate filters bogus check items
upstream, the traffic guard rescues hosts whose check items are gone
entirely.

Stale or unsupported items are still surfaced separately by
``get_stale_items`` (#108), so the gate just routes the noise to the
right place. No information is lost.

## Test approach

Eight unit tests in ``test_analytics.py`` cover the helper:

- ``state == "1"`` flips to stale regardless of lastclock.
- Fresh ``lastclock`` inside the window is not stale.
- ``lastclock`` past the window is stale.
- Zero / missing / garbage ``lastclock`` is stale.
- Custom ``stale_sec`` argument works in both directions.
- ``state == "1"`` overrides a fresh ``lastclock``.

The seven call-site changes are configuration-level (existing
counters now skip stale items); existing 332 tests still pass.

## Consequences

- 340 tests pass (332 pre-change + 8 new helper tests).
- Tool count unchanged at 154.
- ``WRITE_TOOLS`` unchanged.
- ``item.get`` calls at the seven sites now include ``state`` and
  ``lastclock`` in their ``output`` list — a couple extra fields on
  the wire, no extra request count.
- ``fetch_service_status`` now returns "missing" (no entry) for a
  host whose check items are all stale, rather than 0 (DOWN). Any
  caller that previously read 0 to mean DOWN now sees no entry and
  must decide what that means; the existing branches all use a
  three-way ``OK / DOWN / PARTIAL`` lookup with default-no-entry, so
  no caller breaks.
- ``get_service_uptime_report`` now returns a clear "no fresh check
  items" message instead of computing 0% uptime against a stale-only
  population.

## Not included

- A standalone ``get_stale_check_items`` tool. ``get_stale_items``
  (#108) already covers this with a broader filter; no need for a
  service-specific duplicate.
- Per-tool ``stale_sec`` overrides. The 30-minute default mirrors
  the zabbix-reports threshold and is reasonable across all the
  current consumers; an env var would be the cheapest knob if a
  deployment ever needs different.
- Maintenance-window awareness in the gate. Items put into a
  Zabbix maintenance window may legitimately stop polling for the
  duration; the gate currently treats them as stale. Same trade-off
  as in ADRs 010, 014: rare in practice, deferred until it bites.
