"""Output formatters for Zabbix data — severity labels, host/trigger formatting."""

from datetime import datetime, timezone

__all__ = ["format_severity", "format_host_list", "format_host_detail"]

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


def format_host_list(hosts: list) -> str:
    if not hosts:
        return "No hosts found."
    lines = []
    for h in hosts:
        status = "Enabled" if h.get("status") == "0" else "Disabled"
        avail = ""
        if h.get("available") == "1":
            avail = " [available]"
        elif h.get("available") == "2":
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


def format_host_detail(host: dict) -> str:
    parts = [
        f"# Host: {host.get('host', '?')}",
        "",
        f"**Name:** {host.get('name', '')}",
        f"**Host ID:** {host.get('hostid', '?')}",
        f"**Status:** {'Enabled' if host.get('status') == '0' else 'Disabled'}",
    ]

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
