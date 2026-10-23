import httpx

from zbbx_mcp.resolver import InstanceResolver
from zbbx_mcp.rollback import SNAPSHOT_CONFIG, TEMPLATE_LINK, Action, RollbackEntry
from zbbx_mcp.utils import ROLLBACK_STRIP_FIELDS


async def _undo_template_link(client, entry: RollbackEntry) -> str:
    """Undo a link (unlink, never clear) or an unlink (relink); return the summary."""
    host_id = entry.snapshot.get("hostid", "")
    tid = entry.snapshot.get("templateid", entry.object_id)
    if entry.action == Action.CREATE:
        await client.call("host.massremove", {"hostids": [host_id], "templateids": [tid]})
        return f"Rolled back link: unlinked template {tid} from host {host_id}"
    await client.call("host.massadd", {"hosts": [{"hostid": host_id}],
                                       "templates": [{"templateid": tid}]})
    msg = f"Rolled back unlink: relinked template {tid} to host {host_id}"
    if entry.snapshot.get("cleared"):
        msg += (" (the unlink cleared its items; they are re-created from the "
                "template, but their history is not restored)")
    return msg


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()) -> None:

    if "get_rollback_history" not in skip:

        @mcp.tool()
        async def get_rollback_history(instance: str = "") -> str:
            """Show recent write operations that can be rolled back.

            Args:
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)
                entries = client.rollback_log.entries

                if not entries:
                    return "No rollback history. Write operations will appear here."

                lines = []
                for i, e in enumerate(reversed(entries)):
                    idx = len(entries) - 1 - i
                    ts = e.timestamp.strftime("%Y-%m-%d %H:%M UTC")
                    has_snap = "yes" if e.snapshot else "no"
                    lines.append(
                        f"- **#{idx}** [{ts}] `{e.action.value}` "
                        f"**{e.object_type}** {e.object_id}\n"
                        f"  {e.description} (snapshot: {has_snap})"
                    )

                return f"**Rollback history ({len(entries)} entries)**\n\n" + "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

    if "rollback_last" not in skip:

        @mcp.tool()
        async def rollback_last(instance: str = "") -> str:
            """Undo the most recent write operation.

            Args:
                instance: Zabbix instance (optional)
            """
            try:
                client = resolver.resolve(instance)
                # Peek, don't pop: the entry is dropped only once the undo has
                # succeeded, so a failed undo can be retried (as rollback_by_index).
                entry = client.rollback_log.last

                if not entry:
                    return "Nothing to roll back."

                if entry.object_type == TEMPLATE_LINK:
                    msg = await _undo_template_link(client, entry)
                    client.rollback_log.discard(entry)
                    return f"{msg}. ({entry.description})"

                cfg = SNAPSHOT_CONFIG.get(entry.object_type)
                if not cfg:
                    return f"Rollback not supported for object type '{entry.object_type}'."

                id_field = cfg["id_field"]

                if entry.action == Action.CREATE:
                    # Undo create → delete the object
                    await client.call(cfg["delete_method"], [entry.object_id])
                    client.rollback_log.discard(entry)
                    return (
                        f"Rolled back CREATE: deleted {entry.object_type} "
                        f"{entry.object_id}. ({entry.description})"
                    )

                elif entry.action == Action.UPDATE:
                    if not entry.snapshot:
                        return "Cannot roll back: no snapshot was captured for this update."
                    # Undo update → restore previous state
                    restore = dict(entry.snapshot)
                    # Ensure the ID field is present
                    restore[id_field] = entry.object_id
                    # Remove read-only fields that can't be sent back
                    for key in ROLLBACK_STRIP_FIELDS:
                        restore.pop(key, None)

                    await client.call(cfg["update_method"], restore)
                    client.rollback_log.discard(entry)
                    return (
                        f"Rolled back UPDATE: restored {entry.object_type} "
                        f"{entry.object_id} to previous state. ({entry.description})"
                    )

                elif entry.action == Action.DELETE:
                    if not entry.snapshot:
                        return "Cannot roll back: no snapshot was captured before deletion."
                    # Undo delete → re-create from snapshot
                    restore = dict(entry.snapshot)
                    # Remove the ID field since we're creating a new object
                    restore.pop(id_field, None)
                    # Remove read-only fields
                    for key in ("lastchange", "flags", "state", "error",
                                "lastclock", "lastns", "lastvalue", "prevvalue",
                                "evaltype"):
                        restore.pop(key, None)

                    result = await client.call(cfg["create_method"], restore)
                    new_id_key = f"{id_field}s"
                    new_ids = result.get(new_id_key, ["?"])
                    new_id = new_ids[0] if new_ids else "?"
                    client.rollback_log.discard(entry)
                    return (
                        f"Rolled back DELETE: re-created {entry.object_type} "
                        f"as {new_id} (was {entry.object_id}). ({entry.description})"
                    )

                return f"Unknown action: {entry.action}"
            except (httpx.HTTPError, ValueError) as e:
                return f"Rollback failed: {e}"

    if "rollback_by_index" not in skip:

        @mcp.tool()
        async def rollback_by_index(index: int, discard: bool = False, instance: str = "") -> str:
            """Undo a specific write operation by its index from rollback history.

            Use get_rollback_history to see available indices.

            Args:
                index: Entry index from rollback history (0 = oldest)
                discard: Remove the entry from the journal WITHOUT undoing it — for
                    an entry whose undo cannot run (unsupported type, no snapshot,
                    object already gone), which would otherwise block rollback_last
                    forever (default: False)
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)
                entries = client.rollback_log.entries

                if not entries:
                    return "No rollback history."

                if index < 0 or index >= len(entries):
                    return f"Invalid index {index}. Valid range: 0–{len(entries) - 1}."

                entry = entries[index]
                if discard:
                    # Only the local journal changes; Zabbix is not touched
                    # (ADR 158). Said plainly so nobody reads it as an undo.
                    client.rollback_log.discard(entry)
                    return (f"Discarded #{index} ({entry.action.value} {entry.object_type} "
                            f"{entry.object_id}) from the journal without undoing it — "
                            "Zabbix was not changed.")
                if entry.object_type == TEMPLATE_LINK:
                    msg = await _undo_template_link(client, entry)
                    client.rollback_log.discard(entry)
                    return f"{msg} (#{index})."

                cfg = SNAPSHOT_CONFIG.get(entry.object_type)
                if not cfg:
                    return f"Rollback not supported for '{entry.object_type}'."

                id_field = cfg["id_field"]

                if entry.action == Action.CREATE:
                    await client.call(cfg["delete_method"], [entry.object_id])
                    msg = f"Rolled back CREATE #{index}: deleted {entry.object_type} {entry.object_id}."
                elif entry.action == Action.UPDATE:
                    if not entry.snapshot:
                        return f"Cannot roll back #{index}: no snapshot."
                    restore = dict(entry.snapshot)
                    restore[id_field] = entry.object_id
                    for key in ROLLBACK_STRIP_FIELDS:
                        restore.pop(key, None)
                    await client.call(cfg["update_method"], restore)
                    msg = f"Rolled back UPDATE #{index}: restored {entry.object_type} {entry.object_id}."
                elif entry.action == Action.DELETE:
                    if not entry.snapshot:
                        return f"Cannot roll back #{index}: no snapshot."
                    restore = dict(entry.snapshot)
                    restore.pop(id_field, None)
                    for key in ("lastchange", "flags", "state", "error",
                                "lastclock", "lastns", "lastvalue", "prevvalue",
                                "evaltype"):
                        restore.pop(key, None)
                    result = await client.call(cfg["create_method"], restore)
                    new_ids = result.get(f"{id_field}s", ["?"])
                    new_id = new_ids[0] if new_ids else "?"
                    msg = f"Rolled back DELETE #{index}: re-created {entry.object_type} as {new_id}."
                else:
                    return f"Unknown action for #{index}."

                # Remove the entry from the log
                client.rollback_log.discard(entry)
                return msg
            except (httpx.HTTPError, ValueError) as e:
                return f"Rollback failed: {e}"
