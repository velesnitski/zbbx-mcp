# ADR 022: Parent + sub-host fold to canonical id in cohesion and cluster counts

**Status:** Accepted
**Date:** 2026-05-05

## Problem

A 2026-05-05 wave smoke kept reporting "Centered on US (43%)" with
7 hosts. Of the 5 visible US hosts in the rendered output, two were
``edge-us65`` and ``edge-us65 us71`` — the parent + sub-host
convention used across the fleet, where one physical machine has a
parent Zabbix host plus a child whose hostname is parent + space +
suffix. The cohesion guard counted them as two distinct US hosts;
the unique-host counter in clustering counted them as two distinct
hostids. Real distinct US machines were 2 / 6 = 33%, which would
have correctly failed the 0.4 cohesion gate, but the inflated 3 / 7
= 43% slipped through.

The same shape affects ``get_outage_clusters``: parent and sub-host
each produce separate hostids, both contribute to the
``≥ min_hosts`` distinct-hosts-per-subnet check, and the cluster
inflates.

The same fix already shipped in zabbix-reports
(``vpn_brief.py:fetch_mass_disruption_section``); MCP needed parity.

## Decision

### Use ``data.build_parent_map`` to canonicalise hostids

``build_parent_map(hosts) → {child_hid: parent_hid}`` already exists
in ``data.py`` (used by ``get_host_floods`` since ADR 015). It walks
the host list and pairs each child (hostname containing a space)
with its parent (the prefix before the space). For any hostid the
canonical form is ``parent_map.get(hid, hid)`` — parent for
children, self for parents.

### `detect_disruption_wave` (`tools/disruption.py`)

Traffic aggregation now keys by canonical hostid:

```python
parent_map = build_parent_map(hosts)
host_baseline: dict[str, float] = {}
host_recent: dict[str, float] = {}
for iid, hid in iid_to_hid.items():
    canon = parent_map.get(hid, hid)
    if iid in baseline:
        host_baseline[canon] = host_baseline.get(canon, 0) + baseline[iid]
    if iid in recent:
        host_recent[canon] = host_recent.get(canon, 0) + recent[iid]
```

A sub-host's ``net.if.in[*]`` items sum into the parent's bucket.
Downstream `all_dropped`, peer-relative filter, and `_compute_waves`
inputs are then keyed by canonical id only — the cohesion guard's
``[r["country"] for r in bucket]`` produces one entry per physical
machine, and the unique-host set ``{r["hostid"]}`` is naturally
deduplicated.

### `get_outage_clusters` (`tools/correlation.py`)

Inside `_build_records`, each record's ``hostid`` is replaced with
the canonical form, and the displayed ``host`` label uses the
parent's hostname when available. ``_cluster_problems`` then dedupes
correctly when computing ``len(unique_hosts) >= min_hosts``.

The grouping key (subnet24 / subnet16 / provider / hostgroup) still
uses the *child's* IP because in practice sub-hosts share a /24 with
their parent, and using the child's record makes the dedup happen at
counting time rather than at key derivation time.

### #139 redirect — existing helper is enough

The proposed ``data.canonical_hostid_map`` would wrap
``parent_map.get(hid, hid)`` in a function. After this commit lands,
the three tools that need canonicalisation
(``floods.py``, ``disruption.py``, ``correlation.py``) all use
``build_parent_map`` directly with a single inline ``.get(hid, hid)``
at the use site. Adding a new helper for ``dict.get(k, k)`` is over-
engineering. ``build_parent_map`` is the shared abstraction; the
goal of #139 is satisfied by making sure all three tools use it.

## Test approach

Four new tests in ``test_analytics.py``:

- ``build_parent_map`` pairs child hostid with parent hostid (smoke
  test of the existing helper to anchor the fix).
- The canonical-id pattern (``pm.get(hid, hid)``) folds parent + sub
  to one canonical id when fed through a set.
- End-to-end through ``_compute_waves``: 5 distinct canonical
  machines with 2 / 5 = 40% US share passes; the same data with 3
  apparent US "hosts" before canonicalisation would fail — but
  callers must canonicalise upstream, which the tool now does.
- End-to-end through ``_cluster_problems``: 4 records collapse to 3
  distinct canonical hosts; ``min_hosts=3`` passes, ``min_hosts=4``
  fails (would have passed without canonicalisation).

The async tool wrappers use ``build_parent_map`` directly; their
behaviour is covered by the existing registration / smoke tests.

## Consequences

- 374 tests pass (370 pre-change + 4 new helper tests).
- Tool count unchanged at 155.
- ``WRITE_TOOLS`` unchanged.
- ``detect_disruption_wave`` no longer fires false-positive cohesion
  passes when parent + sub-host duplicate a country in the
  same cluster.
- ``get_outage_clusters`` unique-host counts shrink in the rare case
  where parent + sub appear in the same window; clusters that
  depended on the inflated count for ``min_hosts`` will no longer
  fire (which is the correct behaviour — they were spurious).
- Output of ``get_outage_clusters`` may show fewer "Hosts" entries
  in the affected-list because parent + sub now display as the
  parent's hostname only.

## Not included

- A dedicated ``canonical_hostid_map`` helper in ``data.py``. Per
  the analysis above, ``build_parent_map`` is the shared abstraction
  and ``dict.get(k, k)`` does not deserve its own function. If a
  fourth consumer materialises and the inline-pattern starts feeling
  repetitive, this is a five-line addition.
- Calculated-item-style cross-reference detection (parents and
  children that share an item by name rather than by hostname
  prefix). The hostname-prefix convention is the only one in use
  across the fleet today; if it changes, ``build_parent_map`` is the
  right place to extend.
