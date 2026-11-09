"""What this server knows about how the Zabbix API differs across versions.

The client speaks 6.2 through 8.0 with one tool layer. Most of that is
additive — a newer server returns values an older one never did — and the
rule for those is simple: recognise them when they appear, never *ask* for
anything only a newer server understands, because an older one rejects the
whole call over a single unknown parameter (ADR 068/070).

The other kind is removal. Zabbix 8.0 dropped the ``massupdate`` methods and
the host-interface write API (ZBXNEXT-10387, ZBXNEXT-10814). This server
never called them; :data:`REMOVED_METHODS` exists so a guard test keeps it
that way, and so the answer to "does it still work on version X" is a lookup
rather than a search (ADR 162).

No ``tools/`` imports: tools and tests import from here.
"""

from __future__ import annotations

# Method -> first (major, minor) where the server no longer accepts it.
REMOVED_METHODS: dict[str, tuple[int, int]] = {
    "host.massupdate": (8, 0),
    "hostgroup.massupdate": (8, 0),
    "template.massupdate": (8, 0),
    "templategroup.massupdate": (8, 0),
    "hostinterface.create": (8, 0),
    "hostinterface.update": (8, 0),
    "hostinterface.delete": (8, 0),
    "hostinterface.massadd": (8, 0),
    "hostinterface.massremove": (8, 0),
    "hostinterface.replacehostinterfaces": (8, 0),
}

# item.value_type. 5 (binary) arrived in 6.4, 6 (JSON) in 8.0.
VALUE_TYPES: dict[str, str] = {
    "0": "float",
    "1": "character",
    "2": "log",
    "3": "unsigned int",
    "4": "text",
    "5": "binary",
    "6": "JSON",
}
NUMERIC_VALUE_TYPES = frozenset({0, 3})
HISTORY_TYPES = frozenset(int(k) for k in VALUE_TYPES)

# item.type. 22 (Browser) arrived in 7.0, 24 (Telemetry query) in 8.0.
ITEM_TYPES: dict[str, str] = {
    "0": "Zabbix agent",
    "2": "Zabbix trapper",
    "3": "Simple check",
    "5": "Zabbix internal",
    "7": "Zabbix agent (active)",
    "9": "Web item",
    "10": "External check",
    "11": "Database monitor",
    "12": "IPMI agent",
    "13": "SSH agent",
    "14": "Telnet agent",
    "15": "Calculated",
    "17": "SNMP trap",
    "18": "Dependent item",
    "19": "HTTP agent",
    "20": "SNMP agent",
    "21": "Script",
    "22": "Browser",
    "24": "Telemetry query",
}

# mediatype.type. 5 (Push, for the mobile application) arrived in 8.0.
MEDIA_TYPES: dict[str, str] = {
    "0": "Email", "1": "Script", "2": "SMS", "4": "Webhook", "5": "Push",
}

# Acknowledge-history action bits. 512/1024 are written by the server itself
# when a maintenance suppresses or releases a problem (8.0, ZBXNEXT-1150);
# such entries carry no user and no message.
_ACK_ACTIONS = (
    (1, "closed"), (2, "acknowledged"), (4, "message"), (8, "severity changed"),
    (16, "unacknowledged"), (32, "suppressed"), (64, "unsuppressed"),
    (128, "marked as cause"), (256, "marked as symptom"),
    (512, "suppressed by maintenance"), (1024, "released from maintenance"),
)
SYSTEM_ACK_BITS = 512 | 1024


def ack_actions(action: object) -> list[str]:
    """Labels for an acknowledge entry's ``action`` bitmask, lowest bit
    first. Unknown or unparsable input gives an empty list. Pure."""
    try:
        bits = int(str(action))
    except (TypeError, ValueError):
        return []
    return [label for bit, label in _ACK_ACTIONS if bits & bit]


def is_system_ack(entry: dict) -> bool:
    """True for an entry the server wrote on its own (maintenance), not a
    person — no user, a system action bit. Pure."""
    try:
        bits = int(str(entry.get("action", 0)))
    except (TypeError, ValueError):
        return False
    return bool(bits & SYSTEM_ACK_BITS) and str(entry.get("userid") or "0") == "0"


def compact_text(value: object, limit: int = 200) -> str:
    """A text, log or JSON value as one table-safe line: newlines folded,
    pipes escaped, cut at ``limit`` characters with an ellipsis. Pure."""
    text = " ".join(str(value if value is not None else "").split())
    text = text.replace("|", "\\|")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def removed_in(method: str) -> tuple[int, int] | None:
    """The version that removed ``method``, or None if every supported
    version accepts it. Pure."""
    return REMOVED_METHODS.get(method)
