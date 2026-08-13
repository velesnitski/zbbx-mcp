"""Output formatters for Zabbix data — severity labels, host/trigger formatting."""

import re
from datetime import datetime, timezone

__all__ = [
    "format_severity",
    "format_host_list",
    "format_host_detail",
    "normalize_problem_name",
    "format_age",
    "format_value",
]

# Zabbix severity levels (shared across triggers, problems, events)
SEVERITY_NAMES = {
    "0": "Not classified",
    "1": "Information",
    "2": "Warning",
    "3": "Average",
    "4": "High",
    "5": "Disaster",
}

# Host/interface status
HOST_STATUS = {"0": "Enabled", "1": "Disabled"}

# Interface types
INTERFACE_TYPES = {"1": "Agent", "2": "SNMP", "3": "IPMI", "4": "JMX"}

# Graph types
GRAPH_TYPES = {"0": "Normal", "1": "Stacked", "2": "Pie", "3": "Exploded"}


def _ts(epoch: str) -> str:
    """Convert Unix timestamp string to human-readable datetime."""
    try:
        dt = datetime.fromtimestamp(int(epoch), tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, OSError):
        return epoch


def format_severity(severity: str) -> str:
    return SEVERITY_NAMES.get(str(severity), f"Unknown ({severity})")


def format_age(seconds: int) -> str:
    """Render a duration in seconds as a compact human string.

    Negative inputs render as ``"0s"`` so callers don't have to clamp.
    The cutpoints are chosen so each unit's label is unambiguous at the
    next boundary (1m vs 60s, 1h vs 60m, etc.).
    """
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def format_value(value: str, units: str) -> str:
    """Format a numeric metric value with units (B/Bps, %, s, generic)."""
    if not value:
        return "N/A"
    try:
        num = float(value)
        if units in ("B", "Bps", "bps"):
            if num >= 1_073_741_824:
                return f"{num / 1_073_741_824:.2f} G{units}"
            if num >= 1_048_576:
                return f"{num / 1_048_576:.2f} M{units}"
            if num >= 1024:
                return f"{num / 1024:.2f} K{units}"
        elif units == "%":
            return f"{num:.1f}%"
        elif units == "s":
            if num >= 86400:
                return f"{num / 86400:.1f}d"
            if num >= 3600:
                return f"{num / 3600:.1f}h"
            if num >= 60:
                return f"{num / 60:.1f}m"
            return f"{num:.1f}s"
        if num == int(num):
            return f"{int(num)} {units}".strip()
        return f"{num:.2f} {units}".strip()
    except (ValueError, TypeError):
        return f"{value} {units}".strip()


_WS_RE = re.compile(r"\s+")


def normalize_problem_name(name: str, hostname: str = "") -> str:
    """Strip ``on <hostname>`` fragments from a Zabbix problem name.

    Zabbix triggers commonly embed the hostname in the problem name so each
    individual event is self-describing. That defeats per-name dedup when
    the same trigger fires on N hosts: ``Foo on host-a error`` and
    ``Foo on host-b error`` look like distinct problems even though they
    are the same trigger family. Callers should pair the normalised name
    (for grouping / dedup) with the original ``hosts`` column (so no
    information is lost).

    Sub-host form is tried before the bare parent so ``Foo on parent child
    error`` collapses to ``Foo error``, not ``Foo child error``. Hostnames
    in the project follow the convention ``parent`` / ``parent child``
    where the child shares the parent prefix on the first space.

    Returns the input unchanged when ``hostname`` is empty.
    """
    if not name:
        return ""
    cleaned = name.strip()
    if not hostname:
        return cleaned
    parts = hostname.split(" ", 1)
    parent = parts[0]
    candidates = [hostname]
    if parent and parent != hostname:
        candidates.append(parent)
    for candidate in candidates:
        if not candidate:
            continue
        pattern = re.compile(
            rf"\bon\s+{re.escape(candidate)}\b",
            re.IGNORECASE,
        )
        cleaned = pattern.sub("", cleaned)
    return _WS_RE.sub(" ", cleaned).strip()


def cell(value: object) -> str:
    """Sanitize a value for inclusion in a markdown table cell.

    Escapes literal `|` and flattens newlines so one bad input cannot
    collapse the whole table back into a single paragraph.
    """
    s = "" if value is None else str(value)
    return s.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def format_host_list(hosts: list) -> str:
    if not hosts:
        return "No hosts found."
    lines = []
    for h in hosts:
        status = "Enabled" if h.get("status") == "0" else "Disabled"
        avail = ""
        if h.get("active_available") == "1":
            avail = " [available]"
        elif h.get("active_available") == "2":
            avail = " [unavailable]"
        hid = f" (hostid: {h['hostid']})" if "hostid" in h else ""
        lines.append(f"- **{h.get('host', '?')}** ({h.get('name', '')}){hid} [{status}]{avail}")
    return "\n".join(lines)


def format_problem_list(problems: list) -> str:
    if not problems:
        return "No problems found."
    lines = []
    for p in problems:
        severity = format_severity(p.get("severity", "0"))
        ack = " [ACK]" if p.get("acknowledged") == "1" else ""
        clock = _ts(p.get("clock", "0"))
        lines.append(
            f"- **[{severity}]** {p.get('name', 'Unknown')}{ack} — {clock} "
            f"(eventid: {p.get('eventid', '?')})"
        )
    return "\n".join(lines)


def format_host_detail(host: dict, context: dict | None = None) -> str:
    """Render one host. ``context`` carries the derived/fetched extras.

    The bare object answers "does this host exist"; an investigation always
    needs *where it is, what it runs, who hosts it, what it costs and whether
    it is moving traffic* — which used to cost three follow-up calls every
    time. ``context`` is optional so the formatter stays pure and the caller
    decides how much to pay for (ADR 099).

    Recognised keys: ``country``, ``product``, ``tier``, ``provider``,
    ``datacenter``, ``traffic_mbps``, ``cost_month``, ``bw_limit``,
    ``service_status``, ``templates``. Missing keys are simply not rendered.
    """
    ctx = context or {}
    parts = [
        f"# Host: {host.get('host', '?')}",
        "",
        f"**Name:** {host.get('name', '')}",
        f"**Host ID:** {host.get('hostid', '?')}",
        f"**Status:** {'Enabled' if host.get('status') == '0' else 'Disabled'}",
    ]

    # Identity line: where it is / what it is / who runs it.
    ident = []
    if ctx.get("country"):
        ident.append(f"**Country:** {ctx['country']}")
    if ctx.get("product"):
        prod = ctx["product"]
        if ctx.get("tier"):
            prod = f"{prod} / {ctx['tier']}"
        ident.append(f"**Product:** {prod}")
    if ctx.get("provider"):
        ident.append(f"**Provider:** {ctx['provider']}")
    if ctx.get("datacenter"):
        ident.append(f"**Location:** {ctx['datacenter']}")
    if ident:
        parts.extend(["", " | ".join(ident)])

    # Live state: traffic, service check, cost.
    state = []
    if ctx.get("traffic_mbps") is not None:
        state.append(f"**Traffic in:** {ctx['traffic_mbps']:.1f} Mbps")
    if ctx.get("service_status"):
        state.append(f"**Service check:** {ctx['service_status']}")
    if ctx.get("cost_month") is not None:
        state.append(f"**Cost/month:** {ctx['cost_month']:g}")
    if ctx.get("bw_limit") is not None:
        state.append(f"**BW limit:** {ctx['bw_limit']:g} Mbps")
    if state:
        parts.extend(["", " | ".join(state)])

    if host.get("description"):
        parts.extend(["", "## Description", host["description"]])

    groups = host.get("groups", [])
    if groups:
        parts.append("")
        parts.append("## Groups")
        for g in groups:
            parts.append(f"- {g.get('name', '?')}")

    interfaces = host.get("interfaces", [])
    if interfaces:
        parts.append("")
        parts.append("## Interfaces")
        for iface in interfaces:
            itype = {"1": "Agent", "2": "SNMP", "3": "IPMI", "4": "JMX"}.get(
                iface.get("type", ""), "?"
            )
            parts.append(f"- {itype}: {iface.get('ip', '')}:{iface.get('port', '')}")

    templates = ctx.get("templates")
    if templates:
        parts.extend(["", "## Templates"])
        parts.extend(f"- {t}" for t in templates)

    # An enrichment block that failed says so. Without this, "could not read"
    # and "not set" render identically — and the reader has no way to tell a
    # real absence from a permissions wall (ADR 103).
    unavailable = ctx.get("_unavailable")
    if unavailable:
        parts.extend(["", "## Not shown"])
        parts.extend(f"- {u}" for u in unavailable)
        parts.append(
            "\n_These are missing because a call failed, NOT because the value "
            "is absent. Check the token's host-group read permission and its "
            "role's API-method list._"
        )

    return "\n".join(parts)


def format_hostgroup_list(groups: list) -> str:
    if not groups:
        return "No host groups found."
    lines = []
    for g in groups:
        host_count = ""
        if "hosts" in g:
            host_count = f" ({len(g['hosts'])} hosts)"
        lines.append(f"- **{g.get('name', '?')}** (groupid: {g.get('groupid', '?')}){host_count}")
    return "\n".join(lines)
