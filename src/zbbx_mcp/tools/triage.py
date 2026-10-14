"""Authoritative triage of a free-text alert line against live Zabbix state."""

import time as _time

import httpx

from zbbx_mcp.alert_triage import (
    classify_host_triage,
    classify_match,
    parse_alert_line,
)
from zbbx_mcp.anomaly import pick_traffic_interface
from zbbx_mcp.data import collapse_dependent_problems, filter_suppressed
from zbbx_mcp.fetch import physical_traffic_items, to_mbps
from zbbx_mcp.resolver import InstanceResolver
from zbbx_mcp.traffic_verdict import (
    RECENT_SECONDS,
    Evidence,
    classify_shape,
    is_traffic_drop_alert,
    traffic_triage,
    usual_for_hour,
)

_RESOLVE_LIMIT = 10


async def _search_host(client, candidate):
    """Resolve one candidate token to Zabbix host record(s): exact, then search.

    Exact host-name match first; failing that a substring search (which also
    catches multi-VIP hosts — "node-eu-a1 bb2" is found by "node-eu-a1" —
    and Web-Check domains keyed by their domain name).
    """
    exact = await client.call("host.get", {
        "output": ["hostid", "host", "name"],
        "selectHostGroups": ["name"],
        "filter": {"host": [candidate]},
    })
    if exact:
        return exact
    return await client.call("host.get", {
        "output": ["hostid", "host", "name"],
        "selectHostGroups": ["name"],
        "search": {"host": candidate, "name": candidate},
        "searchByAny": True,
        "searchWildcardsEnabled": True,
        "limit": _RESOLVE_LIMIT,
    })


async def _traffic_evidence(client, hostid: str, now: int) -> Evidence:
    """Carrier-interface evidence for one host: last 2 h, the usual for this
    hour from the previous 7 days, and the shape of the last day (ADR 152).
    The carrier is the physical interface with the highest 7-day average,
    so a dead or idle secondary NIC is never the one judged."""
    items = await physical_traffic_items(
        client, [hostid], output=("itemid", "hostid", "key_", "value_type"))
    if not items:
        return Evidence(None, None, classify_shape([], None, now))
    week_from, day_from = now - 8 * 86400, now - 86400
    trends = await client.call("trend.get", {
        "itemids": [i["itemid"] for i in items],
        "time_from": week_from, "time_till": day_from,
        "output": ["itemid", "clock", "value_avg"],
    })
    by_item: dict[str, list[tuple[int, float]]] = {}
    for t in trends or []:
        by_item.setdefault(t["itemid"], []).append((int(t["clock"]), to_mbps(t["value_avg"])))
    carrier = pick_traffic_interface([
        (iid, (sum(v for _, v in pts) / len(pts)) if pts else None)
        for iid, pts in by_item.items()
    ])
    if carrier is None:
        return Evidence(None, None, classify_shape([], None, now))
    usual = usual_for_hour(by_item[carrier], (now // 3600) % 24)
    vtype = next((i.get("value_type", "3") for i in items if i["itemid"] == carrier), "3")
    hist = await client.call("history.get", {
        "itemids": [carrier], "history": int(vtype),
        "time_from": day_from, "output": ["clock", "value"],
    })  # history.get takes no sort parameters (ADR 096); classify_shape sorts
    series = [(int(h["clock"]), to_mbps(h["value"])) for h in hist or []]
    recent = [v for ts, v in series if ts >= now - RECENT_SECONDS]
    recent_avg = sum(recent) / len(recent) if recent else None
    return Evidence(recent_avg, usual, classify_shape(series, usual, now))


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()) -> None:

    if "triage_slack_alert" not in skip:

        @mcp.tool()
        async def triage_slack_alert(
            text: str,
            include_suppressed: bool = False,
            instance: str = "",
        ) -> str:
            """Turn one AI/Slack alert line into an authoritative Zabbix verdict.

            Read-only. Parses the alert, resolves each named host to its Zabbix
            object, then RE-QUERIES live problems — never trusting the feed's
            claimed state — and classifies per host: real_now / recovered /
            symptom_of_cluster, or flags the host AMBIGUOUS / NOT_FOUND instead
            of guessing. Does not acknowledge, suppress, or remediate.

            Args:
                text: One alert line (paste it as-is)
                include_suppressed: Count maintenance-suppressed problems
                    (default: False)
                instance: Zabbix instance name (optional)
            """
            if not (text or "").strip():
                return "Empty alert — paste the alert line."
            parsed = parse_alert_line(text)
            candidates = parsed["hosts"]
            if not candidates:
                return (
                    "No host/domain could be extracted from the alert line. "
                    "Paste a line that names the server or domain, or query Zabbix "
                    "directly (e.g. get_active_problems)."
                )
            try:
                client = resolver.resolve(instance)

                # 1) Resolve each candidate to a Zabbix host (or AMBIGUOUS/NOT_FOUND).
                resolved: list[tuple[str, dict]] = []
                for cand in candidates:
                    match = classify_match(cand, await _search_host(client, cand))
                    resolved.append((cand, match))

                hostids = [
                    m["chosen"]["hostid"]
                    for _, m in resolved
                    if m["status"] in ("EXACT", "FUZZY")
                ]

                # 2) Ground truth: re-query current problems for resolved hosts.
                by_host: dict[str, list] = {}
                dep_map: dict[str, set] = {}
                if hostids:
                    # problem.get does NOT support selectHosts (Zabbix rejects it
                    # with -32602); problems carry only objectid (the trigger).
                    # We map problem→host through trigger.get below, which DOES.
                    problems = await client.call("problem.get", {
                        "hostids": hostids,
                        "output": ["eventid", "name", "severity", "clock",
                                   "objectid", "suppressed"],
                        "recent": True,
                        "limit": 200,
                    })
                    problems = filter_suppressed(problems, include_suppressed)
                    trig_ids = sorted({
                        p["objectid"] for p in problems if p.get("objectid")
                    })
                    trig_hosts: dict[str, list] = {}
                    if trig_ids:
                        trigs = await client.call("trigger.get", {
                            "triggerids": trig_ids,
                            "selectDependencies": ["triggerid"],
                            "selectHosts": ["hostid"],
                            "output": ["triggerid"],
                        })
                        dep_map = {
                            t["triggerid"]: {
                                d["triggerid"] for d in t.get("dependencies", [])
                            }
                            for t in trigs
                        }
                        trig_hosts = {
                            t["triggerid"]: [h["hostid"] for h in t.get("hosts", [])]
                            for t in trigs
                        }
                    # Attribute each problem to the resolved host(s) its trigger
                    # fires on — intersected with what we actually resolved, since
                    # a multi-host trigger can name hosts we didn't ask about.
                    resolved_ids = set(hostids)
                    for p in problems:
                        for hid in trig_hosts.get(p.get("objectid"), []):
                            if hid in resolved_ids:
                                by_host.setdefault(hid, []).append(p)

                # 3) Classify + render.
                lines = [f"**Triage** — {parsed['trigger'][:140]}"]
                claimed = parsed["claimed_state"]
                if claimed:
                    lines.append(f"_feed claimed: {claimed} (re-queried below)_")
                lines.append("")

                traffic_class = is_traffic_drop_alert(
                    text, *(p.get("name", "") for ps in by_host.values() for p in ps))
                if traffic_class:
                    lines.append("_traffic-drop alert: verdict from the carrier interface, "
                                 "not from the problem table (ADR 152)_")
                    lines.append("")
                now = int(_time.time())

                for cand, match in resolved:
                    status = match["status"]
                    if status == "NOT_FOUND":
                        lines.append(f"- `{cand}` → **NOT_FOUND** — no matching Zabbix host/domain.")
                        continue
                    if status == "AMBIGUOUS":
                        names = ", ".join(
                            (h.get("host") or "?") for h in match["candidates"][:5]
                        )
                        lines.append(
                            f"- `{cand}` → **AMBIGUOUS** ({len(match['candidates'])} matches: "
                            f"{names}…) — resolve manually, not guessing."
                        )
                        continue
                    host = match["chosen"]
                    hid = host["hostid"]
                    hname = host.get("host") or host.get("name") or hid
                    its = by_host.get(hid, [])
                    kept, _collapsed = collapse_dependent_problems(its, dep_map)
                    is_symptom = bool(its) and not kept
                    if traffic_class:
                        ev = await _traffic_evidence(client, hid, now)
                        verdict, action = traffic_triage(ev, bool(its), now)
                    else:
                        verdict, action = classify_host_triage(its, is_symptom)
                    tag = "" if status == "EXACT" else " _(fuzzy match)_"
                    lines.append(f"- `{cand}` → **{hname}**{tag} — **{verdict}**. {action}")
                    for p in its[:4]:
                        lines.append(f"    - {p.get('name', '?')[:90]}")
                    if len(its) > 4:
                        lines.append(f"    - …+{len(its) - 4} more")

                return "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error querying Zabbix: {e}"
