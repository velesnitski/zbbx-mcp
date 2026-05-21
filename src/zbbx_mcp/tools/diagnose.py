"""Composite per-host diagnostic — `diagnose_host`.

Replaces the multi-tool chain operators (or LLM clients) run by hand
for every "is this host healthy?" question. Reuses existing
primitives (host.get, trend.get, problem.get, auditlog.get) plus
pure helpers from sibling modules; ships one MCP tool call that
returns a unified verdict + recommended action.

Verdict labels: ``healthy``, ``degraded``, ``traffic_lost``, ``down``,
``https_down``, ``unknown``. Each step is independently available as
its own MCP tool; this one bundles the common sequence so the LLM
doesn't have to remember which tools to call in which order.
"""

from __future__ import annotations

import time as _time
from datetime import datetime, timezone

import httpx

from zbbx_mcp.data import (
    STATUS_ENABLED,
    TRAFFIC_IN_KEYS,
    host_ip,
)
from zbbx_mcp.formatters import format_age, format_severity
from zbbx_mcp.resolver import InstanceResolver
from zbbx_mcp.tools.ip_history import parse_ip_changes


def _classify_host_mode(host_record: dict, items: list[dict]) -> str:
    """Return 'server' if the host has agent / traffic items, else 'domain'.

    A domain-mode host is typically a Zabbix host configured purely
    for HTTPS / web-scenario checks against an external URL — it has
    no agent, no traffic interface, and the diagnostic chain skips
    the dashboard / IP-rotation steps and reads the domain check
    items directly.
    """
    if not items:
        return "domain"
    keys = {(it.get("key_") or "") for it in items}
    if any(k.startswith("net.if.in[") for k in keys):
        return "server"
    if "agent.ping" in keys or "agent.version" in keys:
        return "server"
    return "domain"


def _classify_verdict(
    *,
    mode: str,
    agent_ping_val: int | None,
    agent_ping_age_min: float | None,
    traffic_baseline_mbps: float | None,
    traffic_recent_mbps: float | None,
    open_problems: int,
    https_down: bool,
    https_age_h: float | None,
) -> tuple[str, str]:
    """Decide a single verdict label + a one-line action recommendation.

    Pure function: takes already-aggregated facts about the host and
    returns ``(verdict, action)``. See module docstring for the
    verdict label set.
    """
    if mode == "domain":
        if https_down:
            tag = f" for {int(https_age_h)}h" if https_age_h else ""
            return "https_down", (
                f"Endpoint HTTPS check has been failing{tag}. "
                "Verify the URL externally; check origin and TLS cert."
            )
        if open_problems > 0:
            return "degraded", (
                f"{open_problems} active problem(s) on the domain host; investigate."
            )
        return "healthy", "Domain endpoint passing checks."

    # server mode below
    agent_unreachable = (
        agent_ping_val is not None and agent_ping_val == 0
    ) or (agent_ping_age_min is not None and agent_ping_age_min > 5)

    traffic_collapsed = (
        traffic_baseline_mbps is not None
        and traffic_recent_mbps is not None
        and traffic_baseline_mbps >= 5.0
        and traffic_recent_mbps < traffic_baseline_mbps * 0.1
    )

    if agent_unreachable and traffic_collapsed:
        return "down", "Host is fully down — check VM / hosting provider console."
    if traffic_collapsed and not agent_unreachable:
        return "traffic_lost", (
            "Agent reachable but traffic collapsed. "
            "Investigate external connectivity; "
            "consider rotating the host's external IP if stale."
        )
    if agent_unreachable:
        return "degraded", (
            "Agent unreachable but traffic still flowing — agent-side issue "
            "(restart agent, check connectivity to Zabbix server)."
        )
    if open_problems > 0:
        return "degraded", (
            f"{open_problems} active problem(s); review the list above."
        )
    return "healthy", "No issues detected."


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()) -> None:

    if "diagnose_host" not in skip:

        @mcp.tool()
        async def diagnose_host(
            host: str,
            traffic_hours: int = 6,
            problem_hours: int = 24,
            rotation_days: int = 14,
            instance: str = "",
        ) -> str:
            """Run a multi-step diagnostic on a single host and return a verdict.

            Composes host.get + item.get + trend.get + problem.get +
            auditlog.get into one unified report: agent state, traffic
            vs baseline, active problems, recent IP rotations, and a
            verdict with a recommended action. Auto-detects server-mode
            hosts vs domain-mode hosts (HTTPS-check only).

            Args:
                host: Exact hostname (required)
                traffic_hours: Recent-window for traffic comparison (default: 6)
                problem_hours: Look-back for active problems (default: 24)
                rotation_days: Look-back for IP-rotation history (default: 14)
                instance: Zabbix instance name (optional)
            """
            if not host:
                return "Argument `host` is required."
            try:
                client = resolver.resolve(instance)

                # --- Step 1: locate the host -------------------------------
                hosts = await client.call("host.get", {
                    "output": ["hostid", "host", "name", "status"],
                    "selectInterfaces": ["ip"],
                    "selectGroups": ["name"],
                    "filter": {"host": [host], "status": STATUS_ENABLED},
                })
                if not hosts:
                    return f"Host not found or disabled: {host}"
                h = hosts[0]
                hid = h["hostid"]
                ip = host_ip(h)
                groups = ", ".join(g.get("name", "") for g in h.get("groups", []))

                # --- Step 2: items (used for mode classification + agent/traffic) ---
                items = await client.call("item.get", {
                    "hostids": [hid],
                    "output": ["itemid", "key_", "lastvalue", "lastclock"],
                    "filter": {"status": "0"},
                })
                mode = _classify_host_mode(h, items)

                # --- Step 3: active problems on this host ------------------
                problems = await client.call("problem.get", {
                    "hostids": [hid],
                    "output": ["eventid", "name", "severity", "clock"],
                    "sortfield": "eventid",
                    "sortorder": "DESC",
                    "limit": 30,
                    "recent": True,
                })
                # Filter to recent problems (within problem_hours)
                now = int(_time.time())
                problem_cutoff = now - problem_hours * 3600
                problems = [
                    p for p in problems
                    if int(p.get("clock", 0)) >= problem_cutoff
                ]

                # --- Step 4 (server mode): traffic baseline vs recent ------
                traffic_baseline = traffic_recent = None
                if mode == "server":
                    iids = [
                        it["itemid"] for it in items
                        if it.get("key_") in TRAFFIC_IN_KEYS
                    ]
                    if iids:
                        baseline_from = now - 24 * 3600
                        baseline_till = now - traffic_hours * 3600
                        recent_from = now - traffic_hours * 3600
                        trends_base = await client.call("trend.get", {
                            "itemids": iids,
                            "time_from": baseline_from,
                            "time_till": baseline_till,
                            "output": ["itemid", "value_avg"],
                            "limit": len(iids) * 24,
                        })
                        trends_recent = await client.call("trend.get", {
                            "itemids": iids,
                            "time_from": recent_from,
                            "output": ["itemid", "value_avg"],
                            "limit": len(iids) * 24,
                        })
                        if trends_base:
                            traffic_baseline = sum(
                                float(t.get("value_avg", 0) or 0)
                                for t in trends_base
                            ) / len(trends_base) / 1e6
                        if trends_recent:
                            traffic_recent = sum(
                                float(t.get("value_avg", 0) or 0)
                                for t in trends_recent
                            ) / len(trends_recent) / 1e6

                # --- Step 5 (server mode): agent ping freshness ------------
                agent_ping_val = None
                agent_ping_age_min = None
                if mode == "server":
                    ping = next(
                        (it for it in items if it.get("key_") == "agent.ping"),
                        None,
                    )
                    if ping:
                        try:
                            agent_ping_val = int(float(ping.get("lastvalue", "0")))
                        except (ValueError, TypeError):
                            agent_ping_val = None
                        try:
                            last = int(ping.get("lastclock", "0"))
                            if last > 0:
                                agent_ping_age_min = (now - last) / 60.0
                        except (ValueError, TypeError):
                            pass

                # --- Step 6 (server mode): IP rotation history -------------
                rotations: list[dict] = []
                if mode == "server":
                    records = await client.call("auditlog.get", {
                        "output": ["clock", "details"],
                        "filter": {"resourcetype": 2, "action": 1, "resourceid": hid},
                        "time_from": now - rotation_days * 86400,
                        "sortfield": "clock",
                        "sortorder": "DESC",
                        "limit": 50,
                    })
                    for r in records:
                        for old, new in parse_ip_changes(r.get("details", "")):
                            rotations.append({
                                "clock": int(r.get("clock", 0)),
                                "old": old,
                                "new": new,
                            })

                # --- Step 7 (domain mode): HTTPS check ---------------------
                https_down = False
                https_age_h: float | None = None
                if mode == "domain":
                    https_item = next(
                        (it for it in items
                         if "https" in (it.get("key_") or "").lower()
                         or "webcheck" in (it.get("key_") or "").lower()),
                        None,
                    )
                    if https_item:
                        try:
                            val = int(float(https_item.get("lastvalue", "0") or 0))
                            https_down = (val == 0)
                            last = int(https_item.get("lastclock", "0") or 0)
                            if last > 0 and https_down:
                                # Approximate "down for ~Nh" by walking the
                                # active-problem list for the matching trigger
                                for p in problems:
                                    pname = (p.get("name") or "").lower()
                                    if "https" in pname:
                                        https_age_h = (now - int(p.get("clock", 0))) / 3600
                                        break
                        except (ValueError, TypeError):
                            pass

                # --- Step 8: synthesize verdict ----------------------------
                verdict, action = _classify_verdict(
                    mode=mode,
                    agent_ping_val=agent_ping_val,
                    agent_ping_age_min=agent_ping_age_min,
                    traffic_baseline_mbps=traffic_baseline,
                    traffic_recent_mbps=traffic_recent,
                    open_problems=len(problems),
                    https_down=https_down,
                    https_age_h=https_age_h,
                )

                # --- Render ------------------------------------------------
                lines = [
                    f"## Diagnosis: {host}",
                    "",
                    f"**Verdict:** `{verdict}` ({mode} mode)",
                    "",
                    f"**Recommended action:** {action}",
                    "",
                    "### Identity",
                    f"- Host ID: {hid}",
                    f"- IP: {ip or '—'}",
                    f"- Groups: {groups or '—'}",
                    "",
                ]

                if mode == "server":
                    lines.append("### Agent")
                    if agent_ping_val is None:
                        lines.append("- No `agent.ping` item — not measured")
                    else:
                        state = "✓ reachable" if agent_ping_val == 1 else "✗ DOWN"
                        age = (
                            f"{agent_ping_age_min:.1f}m ago"
                            if agent_ping_age_min is not None else "?"
                        )
                        lines.append(f"- agent.ping = {agent_ping_val} ({state}, last update {age})")
                    lines.append("")
                    lines.append("### Traffic")
                    if traffic_baseline is None or traffic_recent is None:
                        lines.append("- No traffic items / trend data available")
                    else:
                        pct = (
                            (traffic_recent / traffic_baseline * 100)
                            if traffic_baseline > 0 else 0
                        )
                        lines.append(
                            f"- 24h baseline avg: **{traffic_baseline:.1f} Mbps**\n"
                            f"- Last {traffic_hours}h avg: **{traffic_recent:.1f} Mbps**"
                            f" ({pct:.0f}% of baseline)"
                        )
                    lines.append("")
                    lines.append(f"### IP rotation history (last {rotation_days}d)")
                    if not rotations:
                        lines.append("- No rotations in window")
                    else:
                        for r in rotations[:5]:
                            when = datetime.fromtimestamp(
                                r["clock"], timezone.utc
                            ).strftime("%Y-%m-%d %H:%M UTC")
                            age = format_age(now - r["clock"])
                            lines.append(
                                f"- {when} ({age} ago): `{r['old']}` → `{r['new']}`"
                            )
                        if len(rotations) > 5:
                            lines.append(f"  *{len(rotations) - 5} more omitted*")
                    lines.append("")

                lines.append(f"### Active problems ({len(problems)} in last {problem_hours}h)")
                if not problems:
                    lines.append("- None")
                else:
                    for p in problems[:10]:
                        sev = format_severity(p.get("severity", "0"))
                        age = format_age(now - int(p.get("clock", 0)))
                        lines.append(
                            f"- **[{sev}]** {p.get('name', '?')} (started {age} ago)"
                        )
                    if len(problems) > 10:
                        lines.append(f"  *{len(problems) - 10} more omitted*")

                return "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"
