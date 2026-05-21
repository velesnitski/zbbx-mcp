"""Shared parser for the ``tags`` MCP argument → Zabbix API tag filter.

Several Zabbix endpoints (``host.get``, ``problem.get``, ``item.get``,
``trigger.get``) accept a ``tags`` array of ``{tag, value, operator}``
objects. To keep MCP call sites uniform we expose one string argument
``tags`` that callers populate as ``"key:value,key2:value2"``; this
module parses it into the array Zabbix expects.

Format spec:
    "role:edge"                — single tag/value equality.
    "role:edge,env:prod"       — multiple tags; AND-combined (evaltype 0).
    "role:edge, env:prod"      — whitespace ignored.
    "role:"                    — empty value; "exists" check (operator 4).
    "role"                     — same as above; "exists" check.
    ""                         — no filter.

Operator vocabulary (Zabbix 6.x):
    0 — equals (default for key:value form)
    4 — exists (default for bare-key / empty-value form)

Other operators (contains, not-equals, not-contains, not-exists) are
not exposed yet — add when there's a concrete need; the bare-key
shortcut suffices for the common "is this tag present at all?" case.
"""

from __future__ import annotations


def parse_tag_filter(spec: str) -> list[dict]:
    """Parse a ``"key:value,key:value"`` spec into Zabbix tag filter form.

    Returns an empty list when ``spec`` is empty or whitespace.
    Skips empty / malformed pairs silently so a partial spec like
    ``"role:edge,"`` doesn't break the call.
    """
    if not spec or not spec.strip():
        return []
    out: list[dict] = []
    for raw in spec.split(","):
        token = raw.strip()
        if not token:
            continue
        if ":" not in token:
            # Bare key — "exists" check
            out.append({"tag": token, "value": "", "operator": 4})
            continue
        key, _, value = token.partition(":")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if not value:
            out.append({"tag": key, "value": "", "operator": 4})
        else:
            out.append({"tag": key, "value": value, "operator": 0})
    return out


__all__ = ["parse_tag_filter"]
