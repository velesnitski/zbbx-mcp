"""Composite per-host diagnostic — `diagnose_host` + `bulk_diagnose`.

Replaces the multi-tool chain operators (or LLM clients) run by hand
for every "is this host healthy?" question. Reuses existing
primitives (host.get, trend.get, problem.get, auditlog.get) plus
pure helpers from sibling modules.

Two tools share one data-gathering helper:

  - ``diagnose_host(host)`` — single-host verbose report.
  - ``bulk_diagnose(hosts|group|country)`` — fan-out across a target
    set, returns a compact table (one row per host).

Verdict labels: ``healthy``, ``degraded``, ``traffic_lost``, ``down``,
``https_down``, ``unknown``.
"""

from __future__ import annotations

import asyncio
import time as _time
from datetime import datetime, timezone

import httpx

from zbbx_mcp.country import resolve_country
from zbbx_mcp.data import (
    STATUS_ENABLED,
    TRAFFIC_IN_KEYS,
    host_ip,
)
from zbbx_mcp.formatters import format_age, format_severity
from zbbx_mcp.resolver import InstanceResolver
from zbbx_mcp.tools.correlation import subnet24
from zbbx_mcp.tools.ip_history import parse_ip_changes


def _ip_matches_subnet(ip: str, subnet: str) -> bool:
    """Return True if ``ip`` falls within ``subnet`` (CIDR or prefix).

    Supports:
      - "10.1.2.0/24"  — exact /24 match via subnet24() comparison
      - "10.1.0.0/16"  — /16 match by first two octets
      - "10.1.2"       — partial dotted prefix (no slash)

    Anything more exotic returns False. Pure helper.
    """
    if not ip or not subnet:
        return False
    if "/" in subnet:
        try:
            net, bits = subnet.rsplit("/", 1)
            bits_i = int(bits)
        except (ValueError, AttributeError):
            return False
        if bits_i == 24:
            return subnet24(ip) == subnet
        if bits_i == 16:
            ip_parts = ip.split(".")
            net_parts = net.split(".")
            if len(ip_parts) != 4 or len(net_parts) != 4:
                return False
            return ip_parts[:2] == net_parts[:2]
        return False
    # Prefix form (no /bits): startswith match on octet boundary
    pref = subnet.rstrip(".") + "."
    return ip.startswith(pref) or ip == subnet.rstrip(".")

_BULK_CONCURRENCY = 10
_BULK_MAX_HOSTS = 50


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


def _verdict_primary_signal(facts: dict) -> str:
    """Return a one-liner describing the dominant signal behind the verdict.

    Used by ``bulk_diagnose`` to fit the per-host insight into a table cell.
    """
    v = facts["verdict"]
    if v == "down":
        return "agent down + traffic collapsed"
    if v == "traffic_lost":
        base = facts.get("traffic_baseline_mbps")
        recent = facts.get("traffic_recent_mbps")
        if base and recent is not None:
            return f"traffic {base:.0f}→{recent:.1f} Mbps"
        return "traffic collapsed"
    if v == "https_down":
        age = facts.get("https_age_h")
        return f"HTTPS down ~{int(age)}h" if age else "HTTPS check failing"
    if v == "degraded":
        n = len(facts.get("problems", []))
        if n > 0:
            return f"{n} active problem(s)"
        if facts.get("agent_ping_val") == 0:
            return "agent unreachable"
        return "degraded"
    if v == "healthy":
        return "OK"
    return "—"


async def _collect_diagnosis_inner(
    client,
    host_record: dict,
    items: list[dict],
    *,
    traffic_hours: int = 6,
    problem_hours: int = 24,
    rotation_days: int = 14,
    now: int | None = None,
) -> dict:
    """Gather diagnosis facts for one host given pre-fetched ``host_record`` + ``items``.

    Returns a dict of facts plus ``verdict`` and ``action``.
    Used by both ``diagnose_host`` (renders verbose) and
    ``bulk_diagnose`` (renders one row).
    """
    if now is None:
        now = int(_time.time())
    hid = host_record["hostid"]
    mode = _classify_host_mode(host_record, items)

    # Active problems
    problems = await client.call("problem.get", {
        "hostids": [hid],
        "output": ["eventid", "name", "severity", "clock"],
        "sortfield": "eventid",
        "sortorder": "DESC",
        "limit": 30,
        "recent": True,
    })
    problem_cutoff = now - problem_hours * 3600
    problems = [p for p in problems if int(p.get("clock", 0)) >= problem_cutoff]

    # Server-mode-only data
    traffic_baseline = traffic_recent = None
    agent_ping_val: int | None = None
    agent_ping_age_min: float | None = None
    rotations: list[dict] = []

    if mode == "server":
        iids = [it["itemid"] for it in items if it.get("key_") in TRAFFIC_IN_KEYS]
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
                traffic_baseline = (
                    sum(float(t.get("value_avg", 0) or 0) for t in trends_base)
                    / len(trends_base) / 1e6
                )
            if trends_recent:
                traffic_recent = (
                    sum(float(t.get("value_avg", 0) or 0) for t in trends_recent)
                    / len(trends_recent) / 1e6
                )

        ping = next((it for it in items if it.get("key_") == "agent.ping"), None)
        if ping:
            try:
                agent_ping_val = int(float(ping.get("lastvalue", "0")))
            except (ValueError, TypeError):
                pass
            try:
                last = int(ping.get("lastclock", "0"))
                if last > 0:
                    agent_ping_age_min = (now - last) / 60.0
            except (ValueError, TypeError):
                pass

        if rotation_days > 0:
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
                        "old": old, "new": new,
                    })

    # Domain-mode-only data
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
                if https_down:
                    for p in problems:
                        if "https" in (p.get("name") or "").lower():
                            https_age_h = (now - int(p.get("clock", 0))) / 3600
                            break
            except (ValueError, TypeError):
                pass

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

    return {
        "host": host_record.get("host", ""),
        "hid": hid,
        "mode": mode,
        "ip": host_ip(host_record),
        "groups": ", ".join(g.get("name", "") for g in host_record.get("groups", [])),
        "agent_ping_val": agent_ping_val,
        "agent_ping_age_min": agent_ping_age_min,
        "traffic_baseline_mbps": traffic_baseline,
        "traffic_recent_mbps": traffic_recent,
        "problems": problems,
        "rotations": rotations,
        "https_down": https_down,
        "https_age_h": https_age_h,
        "verdict": verdict,
        "action": action,
        "now": now,
    }


def _render_full_report(
    facts: dict, *, traffic_hours: int, problem_hours: int, rotation_days: int,
) -> str:
    """Render the verbose `diagnose_host` markdown report."""
    host = facts["host"]
    mode = facts["mode"]
    now = facts["now"]
    lines = [
        f"## Diagnosis: {host}",
        "",
        f"**Verdict:** `{facts['verdict']}` ({mode} mode)",
        "",
        f"**Recommended action:** {facts['action']}",
        "",
        "### Identity",
        f"- Host ID: {facts['hid']}",
        f"- IP: {facts['ip'] or '—'}",
        f"- Groups: {facts['groups'] or '—'}",
        "",
    ]
    if mode == "server":
        lines.append("### Agent")
        agent_val = facts["agent_ping_val"]
        if agent_val is None:
            lines.append("- No `agent.ping` item — not measured")
        else:
            state = "✓ reachable" if agent_val == 1 else "✗ DOWN"
            age_min = facts["agent_ping_age_min"]
            age = f"{age_min:.1f}m ago" if age_min is not None else "?"
            lines.append(f"- agent.ping = {agent_val} ({state}, last update {age})")
        lines.append("")
        lines.append("### Traffic")
        base = facts["traffic_baseline_mbps"]
        recent = facts["traffic_recent_mbps"]
        if base is None or recent is None:
            lines.append("- No traffic items / trend data available")
        else:
            pct = (recent / base * 100) if base > 0 else 0
            lines.append(
                f"- 24h baseline avg: **{base:.1f} Mbps**\n"
                f"- Last {traffic_hours}h avg: **{recent:.1f} Mbps**"
                f" ({pct:.0f}% of baseline)"
            )
        lines.append("")
        lines.append(f"### IP rotation history (last {rotation_days}d)")
        rots = facts["rotations"]
        if not rots:
            lines.append("- No rotations in window")
        else:
            for r in rots[:5]:
                when = datetime.fromtimestamp(r["clock"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                age = format_age(now - r["clock"])
                lines.append(f"- {when} ({age} ago): `{r['old']}` → `{r['new']}`")
            if len(rots) > 5:
                lines.append(f"  *{len(rots) - 5} more omitted*")
        lines.append("")

    probs = facts["problems"]
    lines.append(f"### Active problems ({len(probs)} in last {problem_hours}h)")
    if not probs:
        lines.append("- None")
    else:
        for p in probs[:10]:
            sev = format_severity(p.get("severity", "0"))
            age = format_age(now - int(p.get("clock", 0)))
            lines.append(f"- **[{sev}]** {p.get('name', '?')} (started {age} ago)")
        if len(probs) > 10:
            lines.append(f"  *{len(probs) - 10} more omitted*")
    return "\n".join(lines)


def _render_bulk_table(rows: list[dict], total_targets: int) -> str:
    """Render the compact bulk-diagnose markdown table."""
    if not rows:
        return "No hosts matched the target set."
    # Sort by verdict severity (down > traffic_lost > https_down > degraded > healthy)
    severity_order = {
        "down": 0, "traffic_lost": 1, "https_down": 2,
        "degraded": 3, "healthy": 4, "unknown": 5,
    }
    rows.sort(key=lambda r: severity_order.get(r["verdict"], 9))

    bad_count = sum(1 for r in rows if r["verdict"] in {"down", "traffic_lost", "https_down"})
    header = [
        f"## Bulk diagnosis — {len(rows)} of {total_targets} host(s)",
        f"({bad_count} flagged as down / traffic_lost / https_down)",
        "",
        "| Host | Verdict | Mode | Primary signal | Action |",
        "|------|---------|------|----------------|--------|",
    ]
    for r in rows:
        action = r["action"]
        if len(action) > 70:
            action = action[:67] + "..."
        header.append(
            f"| {r['host']} | `{r['verdict']}` | {r['mode']} | "
            f"{_verdict_primary_signal(r)} | {action} |"
        )
    return "\n".join(header)


async def _run_bulk_diagnosis(
    client,
    records: list[dict],
    *,
    traffic_hours: int,
    problem_hours: int,
    rotation_days: int = 0,
) -> str:
    """Run diagnosis on a resolved host list and render the table.

    Batches one item.get for all hosts at once, then fans out the
    remaining per-host calls (problem.get, trend.get, auditlog.get)
    with bounded concurrency.
    """
    if not records:
        return "No hosts matched the target set."
    hostids = [r["hostid"] for r in records]
    all_items = await client.call("item.get", {
        "hostids": hostids,
        "output": ["itemid", "hostid", "key_", "lastvalue", "lastclock"],
        "filter": {"status": "0"},
    })
    items_by_host: dict[str, list[dict]] = {}
    for it in all_items:
        items_by_host.setdefault(str(it.get("hostid", "")), []).append(it)
    sem = asyncio.Semaphore(_BULK_CONCURRENCY)
    now = int(_time.time())

    async def diagnose_one(rec: dict) -> dict:
        async with sem:
            return await _collect_diagnosis_inner(
                client, rec, items_by_host.get(str(rec["hostid"]), []),
                traffic_hours=traffic_hours,
                problem_hours=problem_hours,
                rotation_days=rotation_days,
                now=now,
            )

    results = await asyncio.gather(*[diagnose_one(r) for r in records])
    return _render_bulk_table(results, len(records))


async def _fetch_host_records(
    client,
    *,
    hosts: list[str] | None = None,
    group: str = "",
    country: str = "",
    max_hosts: int = _BULK_MAX_HOSTS,
) -> list[dict]:
    """Resolve the target host set from the bulk_diagnose filters.

    At least one of ``hosts`` / ``group`` / ``country`` must be set.
    Caps the result at ``max_hosts`` (callers should warn when truncated).
    """
    filt: dict = {"status": STATUS_ENABLED}
    params: dict = {
        "output": ["hostid", "host", "name", "status"],
        "selectInterfaces": ["ip"],
        "selectGroups": ["name"],
        "filter": filt,
        "limit": max_hosts + 1,
    }
    if hosts:
        filt["host"] = hosts
    if group:
        # Resolve group name to groupid
        gs = await client.call("hostgroup.get", {
            "output": ["groupid"],
            "filter": {"name": [group]},
        })
        if not gs:
            return []
        params["groupids"] = [g["groupid"] for g in gs]

    records = await client.call("host.get", params)

    if country:
        wanted = (country or "").strip().upper()[:2]
        records = [r for r in records if resolve_country(r) == wanted]
    return records[:max_hosts]


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
                hosts = await client.call("host.get", {
                    "output": ["hostid", "host", "name", "status"],
                    "selectInterfaces": ["ip"],
                    "selectGroups": ["name"],
                    "filter": {"host": [host], "status": STATUS_ENABLED},
                })
                if not hosts:
                    return f"Host not found or disabled: {host}"
                items = await client.call("item.get", {
                    "hostids": [hosts[0]["hostid"]],
                    "output": ["itemid", "key_", "lastvalue", "lastclock"],
                    "filter": {"status": "0"},
                })
                facts = await _collect_diagnosis_inner(
                    client, hosts[0], items,
                    traffic_hours=traffic_hours,
                    problem_hours=problem_hours,
                    rotation_days=rotation_days,
                )
                return _render_full_report(
                    facts,
                    traffic_hours=traffic_hours,
                    problem_hours=problem_hours,
                    rotation_days=rotation_days,
                )
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

    if "bulk_diagnose" not in skip:

        @mcp.tool()
        async def bulk_diagnose(
            hosts: str = "",
            group: str = "",
            country: str = "",
            traffic_hours: int = 6,
            problem_hours: int = 24,
            max_hosts: int = 20,
            instance: str = "",
        ) -> str:
            """Run diagnose_host across a target set; return one row per host.

            Specify the target set via at least one of: ``hosts`` (comma- or
            space-separated names), ``group`` (host-group name), or
            ``country`` (ISO-2 / ISO-3 / English name). Multiple filters
            compose (host-list ∩ group ∩ country).

            Returns a compact markdown table sorted by verdict severity
            (down → traffic_lost → https_down → degraded → healthy).

            Args:
                hosts: Comma/space-separated hostnames
                group: Host-group name filter
                country: Country filter (ISO-2 / ISO-3 / English name)
                traffic_hours: Recent-window for traffic comparison (default: 6)
                problem_hours: Look-back for active problems (default: 24)
                max_hosts: Safety cap on fan-out (default: 20, max: 50)
                instance: Zabbix instance name (optional)
            """
            host_list = [
                h.strip() for h in (hosts or "").replace(",", " ").split() if h.strip()
            ]
            if not host_list and not group and not country:
                return (
                    "At least one of `hosts`, `group`, or `country` is required."
                )
            cap = min(max_hosts, _BULK_MAX_HOSTS)
            try:
                client = resolver.resolve(instance)
                records = await _fetch_host_records(
                    client,
                    hosts=host_list or None,
                    group=group, country=country,
                    max_hosts=cap,
                )
                return await _run_bulk_diagnosis(
                    client, records,
                    traffic_hours=traffic_hours,
                    problem_hours=problem_hours,
                    rotation_days=0,  # skip auditlog for speed in bulk
                )
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

    if "diagnose_subnet" not in skip:

        @mcp.tool()
        async def diagnose_subnet(
            subnet: str,
            traffic_hours: int = 6,
            problem_hours: int = 24,
            max_hosts: int = 20,
            instance: str = "",
        ) -> str:
            """Diagnose every host whose interface IP falls in a subnet.

            Designed to follow up on get_outage_clusters output: when a
            cluster row reports "5 hosts on 1.2.3.0/24", paste that CIDR
            in here to get a verdict for each host.

            Accepts:
              - "1.2.3.0/24" (CIDR /24)
              - "1.2.0.0/16" (CIDR /16)
              - "1.2.3"      (dotted prefix, no slash)

            Args:
                subnet: CIDR or dotted prefix (required)
                traffic_hours: Recent-window for traffic comparison (default: 6)
                problem_hours: Look-back for active problems (default: 24)
                max_hosts: Safety cap on fan-out (default: 20, max: 50)
                instance: Zabbix instance name (optional)
            """
            if not subnet:
                return "Argument `subnet` is required."
            cap = min(max_hosts, _BULK_MAX_HOSTS)
            try:
                client = resolver.resolve(instance)
                all_hosts = await client.call("host.get", {
                    "output": ["hostid", "host", "name", "status"],
                    "selectInterfaces": ["ip"],
                    "selectGroups": ["name"],
                    "filter": {"status": STATUS_ENABLED},
                })
                records = [
                    h for h in all_hosts
                    if _ip_matches_subnet(host_ip(h) or "", subnet)
                ]
                if not records:
                    return f"No hosts found in {subnet}."
                records = records[:cap]
                return await _run_bulk_diagnosis(
                    client, records,
                    traffic_hours=traffic_hours,
                    problem_hours=problem_hours,
                    rotation_days=0,
                )
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"
