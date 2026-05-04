"""Single-host outage detector — `get_host_floods`.

Fills the gap between two existing tools:
- `get_active_problems` collapses the same trigger across many hosts but keeps
  N distinct triggers on one host as N separate rows.
- `get_outage_clusters` requires ≥3 hosts in the same /24 (or /16, etc.) to
  fire, so a single host going down with ten triggers slips through.

`get_host_floods` answers "which hosts have many simultaneous problems right
now?" — the canonical signature of one whole host being down. Sub-hosts
(`parent` / `parent child`) are merged via the existing parent-map
convention so a parent-and-child outage reports as one event.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from zbbx_mcp.data import build_parent_map
from zbbx_mcp.formatters import normalize_problem_name
from zbbx_mcp.resolver import InstanceResolver

_SEVERITY_LABELS = {
    0: "Info", 1: "Info", 2: "Warning", 3: "Average", 4: "High", 5: "Disaster",
}


def _group_host_floods(
    records: list[dict],
    parent_map: dict[str, str],
    min_problems: int,
) -> list[dict]:
    """Group active-problem records by parent host and apply the threshold.

    Each input record needs ``hostid``, ``host``, ``name``, ``severity``
    (int), and ``clock`` (epoch seconds). Children are folded into their
    parent via ``parent_map`` so a parent-and-child outage counts once,
    keeping the parent's hostname for display.
    """
    by_parent: dict[str, dict] = {}
    for r in records:
        hid = r.get("hostid", "")
        # Walk up: a child's hostid maps to its parent, the parent maps to itself.
        parent_hid = parent_map.get(hid, hid)
        slot = by_parent.setdefault(
            parent_hid,
            {
                "hostid": parent_hid,
                "host": "",
                "problem_count": 0,
                "max_severity": 0,
                "earliest_clock": None,
                "triggers": [],
                "child_hostids": set(),
            },
        )
        slot["problem_count"] += 1
        slot["max_severity"] = max(slot["max_severity"], r.get("severity", 0))
        clock = r.get("clock", 0)
        if slot["earliest_clock"] is None or (clock and clock < slot["earliest_clock"]):
            slot["earliest_clock"] = clock
        # Prefer the parent's own hostname when available; fall back to whatever we see.
        if hid == parent_hid:
            slot["host"] = r.get("host", slot["host"]) or slot["host"]
        elif not slot["host"]:
            slot["host"] = r.get("host", "")
        slot["triggers"].append(r.get("name", ""))
        if hid != parent_hid:
            slot["child_hostids"].add(hid)

    floods = [
        {
            "hostid": s["hostid"],
            "host": s["host"],
            "problem_count": s["problem_count"],
            "max_severity": s["max_severity"],
            "earliest_clock": s["earliest_clock"],
            "sample_triggers": sorted(set(s["triggers"]))[:5],
            "child_count": len(s["child_hostids"]),
        }
        for s in by_parent.values()
        if s["problem_count"] >= min_problems
    ]
    floods.sort(key=lambda f: (-f["problem_count"], -f["max_severity"]))
    return floods


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()) -> None:

    if "get_host_floods" not in skip:

        @mcp.tool()
        async def get_host_floods(
            min_problems: int = 5,
            min_severity: int = 2,
            max_results: int = 20,
            instance: str = "",
        ) -> str:
            """Find hosts with many simultaneous active problems (whole-host outages).

            Single-host outages slip through both the per-name dedup in
            `get_active_problems` (different triggers count separately) and
            the spatial clustering in `get_outage_clusters` (which requires
            multiple hosts). This tool surfaces them by counting how many
            distinct triggers each host has in PROBLEM state right now.

            Sub-hosts (parent/parent-child convention) are merged into the
            parent so a parent + child outage counts as one event.

            Args:
                min_problems: Minimum simultaneous problems to flag (default: 5)
                min_severity: Minimum severity (0=info ... 5=disaster, default: 2)
                max_results: Maximum hosts to render (default: 20)
                instance: Zabbix instance name (optional)
            """
            try:
                client = resolver.resolve(instance)
                problems = await client.call("problem.get", {
                    "output": ["eventid", "name", "severity", "clock"],
                    "severities": list(range(min_severity, 6)),
                    "sortfield": "eventid",
                    "sortorder": "DESC",
                    "limit": 5000,
                    "recent": True,
                })
                if not problems:
                    return f"No active problems (severity >= {min_severity})."

                event_ids = [p["eventid"] for p in problems]
                events = await client.call("event.get", {
                    "output": ["eventid"],
                    "selectHosts": ["hostid", "host"],
                    "eventids": event_ids,
                })
                event_hosts = {
                    e["eventid"]: e.get("hosts", []) for e in events
                }

                # Parent-map for sub-host merging needs the full enabled-host list
                # because the convention is name-based ("parent child").
                hosts = await client.call("host.get", {
                    "output": ["hostid", "host"],
                    "filter": {"status": "0"},
                })
                parent_map = build_parent_map(hosts)

                records: list[dict] = []
                for p in problems:
                    for h in event_hosts.get(p["eventid"], []):
                        raw_name = p.get("name", "?")
                        host_label = h.get("host", "")
                        records.append({
                            "hostid": h.get("hostid", ""),
                            "host": host_label,
                            "name": normalize_problem_name(raw_name, host_label) or raw_name,
                            "severity": int(p.get("severity", 0)),
                            "clock": int(p.get("clock", 0)),
                        })
                if not records:
                    return "No host-bound active problems."

                floods = _group_host_floods(records, parent_map, min_problems)
                if not floods:
                    return (
                        f"No host floods (need ≥{min_problems} simultaneous "
                        f"problems on one host). Inspected {len(records)} "
                        f"problem records across {len({r['hostid'] for r in records})} hosts."
                    )

                shown = floods[:max_results]
                lines = [
                    f"**{len(floods)} hosts with active flood** "
                    f"(≥{min_problems} simultaneous problems, severity ≥ {min_severity})\n",
                    "| Host | # problems | Max severity | First seen | Sample triggers |",
                    "|------|-----------:|--------------|------------|-----------------|",
                ]
                for f in shown:
                    sev = _SEVERITY_LABELS.get(f["max_severity"], "?")
                    when = (
                        datetime.fromtimestamp(f["earliest_clock"], timezone.utc)
                        .strftime("%Y-%m-%d %H:%M UTC")
                        if f["earliest_clock"]
                        else "—"
                    )
                    sample = "; ".join(t[:50] for t in f["sample_triggers"])
                    suffix = f" (+{f['child_count']} sub-hosts)" if f["child_count"] else ""
                    lines.append(
                        f"| {f['host']}{suffix} | {f['problem_count']} | {sev} | {when} | {sample} |"
                    )
                if len(floods) > max_results:
                    lines.append(f"\n*{len(floods) - max_results} more omitted*")
                return "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"
