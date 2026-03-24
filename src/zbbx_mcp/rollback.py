"""Rollback log for CRUD operations.

Captures pre-mutation snapshots so any write operation can be undone.
Each ZabbixClient gets its own RollbackLog instance.
"""

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class Action(str, Enum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"


@dataclass
class RollbackEntry:
    action: Action
    object_type: str  # e.g., "host", "trigger", "item", "maintenance", "usermacro"
    object_id: str
    snapshot: dict[str, Any]  # full object state before mutation (empty for creates)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    description: str = ""  # human-readable summary

    @property
    def summary(self) -> str:
        ts = self.timestamp.strftime("%Y-%m-%d %H:%M UTC")
        return f"[{ts}] {self.action.value} {self.object_type} {self.object_id}: {self.description}"


class RollbackLog:
    """Bounded log of write operations with pre-mutation snapshots."""

    def __init__(self, max_entries: int = 50):
        self._entries: deque[RollbackEntry] = deque(maxlen=max_entries)

    def record(
        self,
        action: Action,
        object_type: str,
        object_id: str,
        snapshot: dict[str, Any],
        description: str = "",
    ) -> RollbackEntry:
        entry = RollbackEntry(
            action=action,
            object_type=object_type,
            object_id=object_id,
            snapshot=snapshot,
            description=description,
        )
        self._entries.append(entry)
        return entry

    def pop_last(self) -> RollbackEntry | None:
        """Remove and return the most recent entry, or None if empty."""
        if not self._entries:
            return None
        return self._entries.pop()

    @property
    def entries(self) -> list[RollbackEntry]:
        return list(self._entries)

    @property
    def last(self) -> RollbackEntry | None:
        return self._entries[-1] if self._entries else None

    def remove_by_index(self, index: int) -> bool:
        """Remove entry at index. Returns True if removed."""
        if 0 <= index < len(self._entries):
            del self._entries[index]
            return True
        return False

    def __len__(self) -> int:
        return len(self._entries)


# API methods for fetching and restoring snapshots, keyed by object_type
SNAPSHOT_CONFIG: dict[str, dict[str, str]] = {
    "host": {
        "get_method": "host.get",
        "create_method": "host.create",
        "update_method": "host.update",
        "delete_method": "host.delete",
        "id_field": "hostid",
        "get_params_extra": '{"selectGroups": ["groupid"], "selectInterfaces": "extend", "selectMacros": "extend", "selectTags": "extend"}',
    },
    "item": {
        "get_method": "item.get",
        "create_method": "item.create",
        "update_method": "item.update",
        "delete_method": "item.delete",
        "id_field": "itemid",
    },
    "trigger": {
        "get_method": "trigger.get",
        "create_method": "trigger.create",
        "update_method": "trigger.update",
        "delete_method": "trigger.delete",
        "id_field": "triggerid",
    },
    "maintenance": {
        "get_method": "maintenance.get",
        "create_method": "maintenance.create",
        "update_method": "maintenance.update",
        "delete_method": "maintenance.delete",
        "id_field": "maintenanceid",
        "get_params_extra": '{"selectHosts": ["hostid"], "selectGroups": ["groupid"], "selectTimeperiods": "extend"}',
    },
    "usermacro": {
        "get_method": "usermacro.get",
        "create_method": "usermacro.create",
        "update_method": "usermacro.update",
        "delete_method": "usermacro.delete",
        "id_field": "hostmacroid",
    },
    "hostgroup": {
        "get_method": "hostgroup.get",
        "create_method": "hostgroup.create",
        "update_method": "hostgroup.update",
        "delete_method": "hostgroup.delete",
        "id_field": "groupid",
    },
}
