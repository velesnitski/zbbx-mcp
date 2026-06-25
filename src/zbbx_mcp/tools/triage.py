"""Authoritative triage of a free-text alert line against live Zabbix state."""

import httpx

from zbbx_mcp.alert_triage import (
    classify_host_triage,
    classify_match,
    parse_alert_line,
)
from zbbx_mcp.data import collapse_dependent_problems, filter_suppressed
from zbbx_mcp.resolver import InstanceResolver

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
