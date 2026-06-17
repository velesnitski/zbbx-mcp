# ADR 062: `sync-mcp-label` must update every container, not short-circuit

**Status:** Accepted
**Date:** 2026-06-16

## Problem

ADR 061's `scripts/sync-mcp-label.py` re-keyed only the **first**
`mcpServers` container that matched. `~/.claude.json` carries one zabbix
entry per project, so a real config has several — and the script left all
but the first stale (`zabbix` instead of `zabbix v<version>`). Caught
immediately on first real use: of two containers, only one was renamed.

The cause was `main()`:

```python
if not any(rename_in(c) for c in mcp_containers(cfg)):
```

`any()` over a **generator** short-circuits — the moment `rename_in`
returns `True` for the first container, the generator is abandoned and
the remaining containers are never visited. ADR 061's tests exercised
`rename_in` on a single container directly, so the short-circuit in the
multi-container reduction slipped through.

## Decision

Extract `sync_config(cfg, get_version)` that maps `rename_in` over all
containers via a **list comprehension** before reducing with `any()`, so
every container is visited regardless of earlier results:

```python
return any([rename_in(c, get_version) for c in mcp_containers(cfg)])
```

`main()` now calls `sync_config`. The list is built fully first, so no
short-circuit; idempotence and the atomic-write/`.bak` path are unchanged.

## Test approach

New `TestSyncConfig`: a config with the zabbix entry in two project
containers must come back with **both** re-keyed (the bug left the second
plain `zabbix`), and a config with no match returns `False`. This is the
regression the ADR 061 tests structurally couldn't catch, since they
never went through the multi-container reduction.

## Consequences

- Tool count unchanged (162). Tests +2 (606 → 608).
- The label sync now updates the zabbix entry in every project block in
  one run; verified live (two containers both → `zabbix v1.15.0`).

## Not included

- **Multiple matching entries within one container.** Still assumes one
  zbbx-mcp entry per container (true in practice); two would collide on
  the same new key. Out of scope until it occurs.
