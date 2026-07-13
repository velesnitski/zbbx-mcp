# ADR 021: max_age_hours recency filter on get_outage_clusters and get_host_floods

**Status:** Accepted
**Date:** 2026-05-05

## Problem

A 2026-05-05 smoke test surfaced that two of the active-incident tools
returned ancient state mixed with current state:

- ``get_outage_clusters`` returned a batch of clusters of which only a
  couple were from the current week; the earliest event in one of them
  was roughly six months old.
- ``get_host_floods`` returned a batch of hosts in flood state, one of
  them carrying an ``earliest_clock`` from months earlier.

Both tools read Zabbix problems with ``recent: True``, which means
"events that have not been resolved" — not "events that started
recently". A trigger that fires once and never gets ``acknowledge``d
or auto-resolved stays in PROBLEM state forever, so the problem
record is "active" indefinitely.

That's correct for backlog hygiene (find the never-ack'd long tail)
but misleading for current-incident consumers (Slack alerters,
status pages, on-call dashboards) that want "what's wrong right
now."

The same recency gate was already shipped inline in
``zabbix-reports/vpn_brief.py`` (12h cutoff in
``fetch_mass_disruption_section``); moving the parameter into MCP
makes the recency view available to every other consumer.

## Decision

### Add ``max_age_hours: int = 0`` to both tools

``0`` means "unlimited" and preserves the existing behaviour — every
caller who doesn't set the argument sees today's output, unchanged.
A non-zero value drops any record (cluster or flood) whose earliest
clock is older than ``now - max_age_hours * 3600``.

The filter applies after the per-tool grouping has already happened.
For ``get_outage_clusters``, it filters the *problem records*
upstream of clustering (so a cluster doesn't accidentally include
ancient events in its time-range bookends). For ``get_host_floods``,
it filters the assembled flood list by ``earliest_clock``.

### Surface age in the rendered output

Both tools now show a "Started Nh ago" / "Age" column in the
rendered output. Operators triaging without the arg can still see at
a glance which clusters/floods are recent and which are stale
backlog.

A new pure helper ``_format_age(seconds)`` lives in
``tools/correlation.py`` and is reused by ``floods.py`` so the two
tools render ages identically:

```python
0..59 s   → "Ns"
60..3599  → "Nm"
3600..86399 → "Nh"
86400..    → "Nd"
```

Negative inputs clamp to ``"0s"`` so callers do not have to validate.

### README hint for #137 (deployment context)

The README's env-var table now lists the standard Zabbix template
keys (``icmppingloss`` and ``icmppingsec``) as suggested values for
``ZABBIX_PING_LOSS_KEY`` and ``ZABBIX_PING_RTT_KEY``. The actual env
var configuration on the deployed MCP server is a deployment-side
action by the operator, not an MCP code change.

## Test approach

5 new tests in ``test_analytics.py`` for ``_format_age`` covering
seconds / minutes / hours / days boundaries plus negative-clamped-
to-zero. The async tool wrappers do only fetch + filter + render and
are covered by existing registration / smoke tests; the new
``max_age_hours`` arg has a default that preserves prior behaviour,
so no integration test changes were needed.

## Consequences

- 370 tests pass (365 pre-change + 5 new).
- Tool count unchanged at 155.
- ``WRITE_TOOLS`` unchanged.
- ``get_outage_clusters`` and ``get_host_floods`` gain one optional
  keyword argument each; defaults preserve existing behaviour.
- Output of both tools gains a "Started" age field; consumers that
  parse the rendered text should expect the column. (The structured
  fields — ``start``, ``earliest_clock`` — were already present and
  remain unchanged.)
- README env-var table documents the standard Zabbix keys for the
  ping items, so a fresh deploy can populate them without guessing.

## Not included

- A ``min_age_hours`` complement (find old/forgotten clusters
  specifically). Useful for backlog hygiene but not requested; can
  be added if a hygiene consumer materialises.
- An auto-acknowledge sweep over old PROBLEM events. That's a Zabbix
  admin operation, not MCP scope.
- Setting ``ZABBIX_PING_LOSS_KEY`` / ``_RTT_KEY`` on the running MCP
  server. Operator action, deployment-side.
