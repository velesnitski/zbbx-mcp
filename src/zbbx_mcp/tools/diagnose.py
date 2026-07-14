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
    canonical_host_name,
    excluded_test_note,
    filter_suppressed,
    host_ip,
    partition_test_hosts,
)
from zbbx_mcp.fetch import is_physical_traffic_in_key
from zbbx_mcp.formatters import format_age, format_severity
from zbbx_mcp.resolver import InstanceResolver
from zbbx_mcp.tools.correlation import subnet24
from zbbx_mcp.tools.ip_history import parse_ip_changes

# Width of the baseline window, in hours. It always sits immediately *before*
# the recent window (ADR 078).
_BASELINE_SPAN_HOURS = 24


def _traffic_windows(now: int, traffic_hours: int) -> tuple[int, int, int]:
    """Return ``(baseline_from, baseline_till, recent_from)``.

    The baseline is the ``_BASELINE_SPAN_HOURS`` immediately preceding the
    recent window, so it can never collapse however wide ``traffic_hours``
    gets. The old code anchored ``baseline_from`` at a hardcoded ``now - 24h``
    while ``baseline_till`` was ``now - traffic_hours``: any
    ``traffic_hours >= 24`` made the range empty (or inverted), so the baseline
    came back ``None``, the tool printed "No traffic items / trend data
    available", and the ``traffic_lost`` verdict became *unreachable* — simply
    widening the window silently disabled the check. Pure.
    """
    hours = max(int(traffic_hours), 1)
    recent_from = now - hours * 3600
    baseline_till = recent_from
    baseline_from = baseline_till - _BASELINE_SPAN_HOURS * 3600
    return baseline_from, baseline_till, recent_from


def _mean_by_item(trends: list[dict]) -> dict[str, float]:
    """Mean ``value_avg`` (bps) per itemid. Pure."""
    acc: dict[str, list[float]] = {}
    for t in trends:
        iid = str(t.get("itemid", "") or "")
        if not iid:
            continue
        try:
            acc.setdefault(iid, []).append(float(t.get("value_avg", 0) or 0))
        except (TypeError, ValueError):
            continue
    return {k: sum(v) / len(v) for k, v in acc.items() if v}


def _carrier_traffic_mbps(
    trends_base: list[dict], trends_recent: list[dict]
) -> tuple[float | None, float | None]:
    """``(baseline_mbps, recent_mbps)`` measured on the host's *carrier* NIC.

    A box usually has several physical NICs where only one carries the load
    (e.g. ``bond0`` at 60 Mbps beside an idle ``eno4`` at 0). Averaging every
    trend row across every NIC — as this used to — halved the real figure and
    skewed the baseline-to-recent ratio the verdict depends on. Instead: take
    the busiest interface in the *baseline* as the carrier and measure both
    windows on that same item, so the comparison is like-for-like and a
    collapse on the carrier cannot be masked by an idle peer. Pure.
    """
    base = _mean_by_item(trends_base)
    recent = _mean_by_item(trends_recent)
    if base:
        carrier = max(base, key=lambda k: base[k])
    elif recent:
        carrier = max(recent, key=lambda k: recent[k])
    else:
        return None, None
    b, r = base.get(carrier), recent.get(carrier)
    return (
        b / 1e6 if b is not None else None,
        r / 1e6 if r is not None else None,
    )


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


def _freshest_agent_ping(items: list[dict]) -> dict | None:
    """Return the `agent.ping` item with the most recent ``lastclock``.

    Items may span a canonical group's VIP records (ADR 049); the freshest
    reading is the box's true agent state — a stale sub-host record must
    not override the parent's live agent. None when no ping item present.
    Pure helper.
    """
    pings = [it for it in items if it.get("key_") == "agent.ping"]
    if not pings:
        return None
    return max(pings, key=lambda it: int(it.get("lastclock", "0") or 0))


def _keep_active_or_recent(problems, now, problem_hours):
    """Keep every UNRESOLVED problem (any age) plus resolved ones in the window.

    The bug this fixes (ADR 069): a still-active problem is dropped when its
    *start* clock is older than ``problem_hours``, so a host with unresolved
    Disasters that began days ago read ``healthy`` / 0 problems. A days-long
    unresolved problem is more severe, not less — it must never be aged out.
    Only recently-resolved entries (``r_eventid`` set, present because
    ``problem.get`` is called with ``recent=True``) are subject to the
    look-back window. Pure helper.
    """
    cutoff = now - problem_hours * 3600
    out = []
    for p in problems:
        resolved = (p.get("r_eventid") or "0") not in ("0", "")
        if not resolved or int(p.get("clock", 0) or 0) >= cutoff:
            out.append(p)
    return out


async def _collect_diagnosis_inner(
    client,
    host_record: dict,
    items: list[dict],
    *,
    traffic_hours: int = 6,
    problem_hours: int = 24,
    rotation_days: int = 14,
    group_hostids: list[str] | None = None,
    include_suppressed: bool = False,
    now: int | None = None,
) -> dict:
    """Gather diagnosis facts for one host given pre-fetched ``host_record`` + ``items``.

    Returns a dict of facts plus ``verdict`` and ``action``.
    Used by both ``diagnose_host`` (renders verbose) and
    ``bulk_diagnose`` (renders one row).

    ``group_hostids`` — when the host is a multi-VIP physical machine,
    the hostids of every VIP in the canonical group. Problems are queried
    across all of them so a sub-host-specific problem still affects the
    verdict (ADR 046). Defaults to the rep host alone.

    ``include_suppressed`` — when False (default), maintenance-suppressed
    problems are dropped, so a host in a maintenance window does not read
    ``degraded`` from planned downtime (ADR 052).
    """
    if now is None:
        now = int(_time.time())
    hid = host_record["hostid"]
    mode = _classify_host_mode(host_record, items)

    # Active problems — across every VIP of the box when known, so a
    # sub-host problem is not invisible to the verdict.
    problem_hostids = group_hostids or [hid]
    problems = await client.call("problem.get", {
        "hostids": problem_hostids,
        "output": ["eventid", "name", "severity", "clock", "suppressed",
                   "r_eventid"],
        "sortfield": "eventid",
        "sortorder": "DESC",
        "limit": 30,
        "recent": True,
    })
    problems = filter_suppressed(problems, include_suppressed)
    # Never age out an UNRESOLVED problem by its start time — only resolved
    # entries are windowed (ADR 069).
    problems = _keep_active_or_recent(problems, now, problem_hours)

    # Server-mode-only data
    traffic_baseline = traffic_recent = None
    agent_ping_val: int | None = None
    agent_ping_age_min: float | None = None
    rotations: list[dict] = []

    if mode == "server":
        # One shared definition of "traffic item" with fetch_traffic_map — an
        # exact-match against a hardcoded key list used to disagree with it.
        iids = [
            it["itemid"] for it in items
            if is_physical_traffic_in_key(it.get("key_", ""))
        ]
        if iids:
            baseline_from, baseline_till, recent_from = _traffic_windows(
                now, traffic_hours
            )
            trends_base, trends_recent = await asyncio.gather(
                client.call("trend.get", {
                    "itemids": iids,
                    "time_from": baseline_from,
                    "time_till": baseline_till,
                    "output": ["itemid", "value_avg"],
                    "limit": len(iids) * _BASELINE_SPAN_HOURS,
                }),
                client.call("trend.get", {
                    "itemids": iids,
                    "time_from": recent_from,
                    "output": ["itemid", "value_avg"],
                    "limit": len(iids) * max(int(traffic_hours), 1),
                }),
            )
            traffic_baseline, traffic_recent = _carrier_traffic_mbps(
                trends_base, trends_recent
            )

        # Agent ping across the whole box: items may span several VIP records
        # (ADR 049), so pick the freshest reading — a stale sub-host record
        # should not override the parent's live agent.
        ping = _freshest_agent_ping(items)
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
    lines.append(f"### Active problems ({len(probs)})")
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


def _dedupe_records_by_canonical(records: list[dict]) -> tuple[list[dict], dict[str, int]]:
    """Collapse a host list to one record per canonical (physical) machine.

    For each canonical group prefer the parent (host name without a space)
    as the representative; fall back to the first record when no parent is
    present in the resolved set. Returns ``(deduped_records, sub_counts)``
    where ``sub_counts`` is keyed by canonical name with the number of
    sub-host records collapsed into each kept record (0 when standalone).

    Bulk diagnosis previously ran ``_collect_diagnosis_inner`` once per
    Zabbix record; multi-record physical machines therefore showed up as
    N near-identical rows in the output. Pre-dedup at the entry of
    ``_run_bulk_diagnosis`` so the fan-out emits one row per box (ADR 039).
    """
    groups: dict[str, list[dict]] = {}
    for r in records:
        cn = canonical_host_name(r.get("host", ""))
        groups.setdefault(cn, []).append(r)
    deduped: list[dict] = []
    sub_counts: dict[str, int] = {}
    for cn, recs in groups.items():
        rep = recs[0]
        for r in recs:
            if " " not in r.get("host", ""):
                rep = r
                break
        # Carry the whole group's hostids so the diagnosis can query
        # problems across every VIP, not just the rep (ADR 046).
        rep["_group_hostids"] = [r["hostid"] for r in recs]
        deduped.append(rep)
        sub_counts[cn] = len(recs) - 1
    return deduped, sub_counts


async def _run_bulk_diagnosis(
    client,
    records: list[dict],
    *,
    traffic_hours: int,
    problem_hours: int,
    rotation_days: int = 0,
    include_suppressed: bool = False,
) -> str:
    """Run diagnosis on a resolved host list and render the table.

    Pre-folds the input list so parent + sub-host records collapse to one
    diagnostic row per physical machine (ADR 039). Then batches one
    item.get for the deduped set and fans out per-host calls with
    bounded concurrency.
    """
    if not records:
        return "No hosts matched the target set."

    original_count = len(records)
    records, sub_counts = _dedupe_records_by_canonical(records)

    # Fetch items across every VIP of every box (ADR 049) — traffic lives on
    # the sub-host interfaces, so a rep-only read can miss it. Map each VIP's
    # items back to its canonical group so each rep sees the whole box.
    all_hostids = sorted({
        hid for r in records for hid in (r.get("_group_hostids") or [r["hostid"]])
    })
    all_items = await client.call("item.get", {
        "hostids": all_hostids,
        "output": ["itemid", "hostid", "key_", "lastvalue", "lastclock"],
        "filter": {"status": "0"},
    })
    items_by_host: dict[str, list[dict]] = {}
    for it in all_items:
        items_by_host.setdefault(str(it.get("hostid", "")), []).append(it)
    sem = asyncio.Semaphore(_BULK_CONCURRENCY)
    now = int(_time.time())

    def _group_items(rec: dict) -> list[dict]:
        out: list[dict] = []
        for hid in (rec.get("_group_hostids") or [rec["hostid"]]):
            out.extend(items_by_host.get(str(hid), []))
        return out

    async def diagnose_one(rec: dict) -> dict:
        async with sem:
            return await _collect_diagnosis_inner(
                client, rec, _group_items(rec),
                traffic_hours=traffic_hours,
                problem_hours=problem_hours,
                rotation_days=rotation_days,
                group_hostids=rec.get("_group_hostids"),
                include_suppressed=include_suppressed,
                now=now,
            )

    results = await asyncio.gather(*[diagnose_one(r) for r in records])

    # Annotate each result with its sub-host count so the rendered table
    # shows "parent (+N sub)" when the canonical group covered more than
    # one Zabbix record.
    for r in results:
        cn = canonical_host_name(r.get("host", ""))
        sub = sub_counts.get(cn, 0)
        if sub > 0:
            r["host"] = f"{r['host']} (+{sub} sub)"

    return _render_bulk_table(results, original_count)


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

    if country:
        # Country filter runs Python-side via resolve_country (hostname +
        # inventory fallback). Don't pre-truncate via the API ``limit`` —
        # otherwise we'd filter from an arbitrarily-windowed sample and
        # miss most matching hosts. Pull inventory so resolve_country has
        # both signals; cap at the end.
        params["selectInventory"] = ["country_code", "country_name"]
    else:
        # No country filter: server-side limit avoids over-fetching.
        params["limit"] = max_hosts + 1

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
            include_suppressed: bool = False,
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
                problem_hours: Look-back window for recently-resolved problems
                    (default: 24); active problems are reported regardless of age
                rotation_days: Look-back for IP-rotation history (default: 14)
                include_suppressed: Count maintenance-suppressed problems in the
                    verdict (default: False — a maintenance host won't read degraded)
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
                rep = hosts[0]
                # Pull the whole canonical group (parent + VIP sub-hosts) so a
                # sub-host-specific problem still affects the verdict (ADR 046).
                canonical = canonical_host_name(rep["host"])
                group = await client.call("host.get", {
                    "output": ["hostid", "host"],
                    "search": {"host": canonical},
                    "searchWildcardsEnabled": False,
                    "filter": {"status": STATUS_ENABLED},
                })
                group_hostids = [
                    g["hostid"] for g in group
                    if canonical_host_name(g.get("host", "")) == canonical
                ] or [rep["hostid"]]
                # Items across the whole box (ADR 049): traffic lives on the
                # sub-host VIP interfaces, so a rep-only read can miss it.
                items = await client.call("item.get", {
                    "hostids": group_hostids,
                    "output": ["itemid", "hostid", "key_", "lastvalue", "lastclock"],
                    "filter": {"status": "0"},
                })
                facts = await _collect_diagnosis_inner(
                    client, rep, items,
                    traffic_hours=traffic_hours,
                    problem_hours=problem_hours,
                    rotation_days=rotation_days,
                    group_hostids=group_hostids,
                    include_suppressed=include_suppressed,
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
            include_suppressed: bool = False,
            include_test: bool = False,
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
                problem_hours: Look-back window for recently-resolved problems
                    (default: 24); active problems are reported regardless of age
                max_hosts: Safety cap on fan-out (default: 20, max: 50)
                include_suppressed: Count maintenance-suppressed problems
                    (default: False)
                include_test: Keep test/staging hosts when the target set comes
                    from `group`/`country` (default: False). Hosts named
                    explicitly in `hosts` are never dropped — naming one is
                    itself the request to diagnose it.
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
                # Only a *scoped* sweep (group/country) drops test boxes. An
                # explicitly named host is always diagnosed — asking for it by
                # name is the request.
                excluded: list[dict] = []
                if not include_test and not host_list:
                    records, excluded = partition_test_hosts(records)
                out = await _run_bulk_diagnosis(
                    client, records,
                    traffic_hours=traffic_hours,
                    problem_hours=problem_hours,
                    rotation_days=0,  # skip auditlog for speed in bulk
                    include_suppressed=include_suppressed,
                )
                return out + excluded_test_note(excluded)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

    if "diagnose_subnet" not in skip:

        @mcp.tool()
        async def diagnose_subnet(
            subnet: str,
            traffic_hours: int = 6,
            problem_hours: int = 24,
            max_hosts: int = 20,
            include_suppressed: bool = False,
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
                problem_hours: Look-back window for recently-resolved problems
                    (default: 24); active problems are reported regardless of age
                max_hosts: Safety cap on fan-out (default: 20, max: 50)
                include_suppressed: Count maintenance-suppressed problems
                    (default: False)
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
                    include_suppressed=include_suppressed,
                )
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"
