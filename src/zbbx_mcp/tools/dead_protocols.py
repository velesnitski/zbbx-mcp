"""Dead protocols on live hosts — what "UP if any protocol answers" hides.

The availability rule is deliberate: a host is UP for the hour if *any* of its
protocol checks answered, because one stuck check must not condemn a serving
machine. The cost of that rule is that a protocol can die completely and leave
no mark anywhere — not on the SLA, not in the problem list.

Two design choices here come from a live incident rather than from theory
(ADR 114):

**Discovery is by key pattern, not by the configured service keys.** The
outage that motivated this tool was on a check that is not one of the three
`ZABBIX_SERVICE*_CHECK_KEY` values. Looking only at configured keys is
precisely why nobody saw it — so this walks every ``*check*`` item instead.

**Results aggregate by check key, not only by host.** The same incident
produced one dead protocol on every host in a fleet. As per-host rows that is
hundreds of lines nobody reads; as a single line — *dead on N of N hosts* — it
is a platform outage with an obvious owner. The dead/judged ratio is the
discriminator between "this box is broken" and "this protocol is down
everywhere", and it is the question an operator actually has.

The pure core (``classify_protocol``, ``aggregate_by_check``) is unit tested;
the async tool wires trend data into it.
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from zbbx_mcp.classify import classify_host as _classify_host
from zbbx_mcp.data import (
    countries_for_region,
    excluded_test_note,
    extract_country,
    partition_test_hosts,
)
from zbbx_mcp.resolver import InstanceResolver

_MAX_HOSTS = 400          # bound the trend fetch; truncation is disclosed
_DEAD_WINDOW_H = 24       # silent this long on a live host = dead
_MIN_SEEN_H = 3           # fewer observed hours than this: no verdict
_MIN_FLEET_JUDGED = 3     # below this, "all of them" is not a fleet claim

# Verdict vocabulary.
ALIVE = "alive"
DIED = "died"             # answered earlier in the window, then went silent
NEVER_UP = "never up"     # zero passes since first seen — provisioning gap
TOO_YOUNG = "too young"   # too few observed hours to judge
HOST_DARK = "host dark"   # whole host down — the SLA's job, not this one


@dataclass
class ProtoVerdict:
    state: str
    since_bucket: int | None = None
    dead_h: int = 0


def classify_protocol(
    up_hours: set,
    seen_hours: set,
    host_up_hours: set,
    now_b: int,
    *,
    window_h: int = _DEAD_WINDOW_H,
    min_seen_h: int = _MIN_SEEN_H,
) -> ProtoVerdict:
    """Classify one check item over the recent window. Pure.

    Order matters. ``too young`` is checked before anything else, because a
    verdict from two samples is an artefact; ``host dark`` before deadness,
    because a machine that is entirely down is already covered elsewhere and
    listing every one of its protocols here would bury the real finding.

    ``died`` and ``never up`` are separated deliberately — they need different
    actions. Died is a regression with a timestamp you can match against a
    deploy; never-up is a provisioning gap, which is what an item looks like
    right after it has been recreated.
    """
    recent = set(range(now_b - window_h, now_b))
    seen_recent = seen_hours & recent
    if len(seen_recent) < min_seen_h:
        return ProtoVerdict(TOO_YOUNG)
    if up_hours & recent:
        return ProtoVerdict(ALIVE)
    if not (host_up_hours & recent):
        return ProtoVerdict(HOST_DARK)
    if up_hours:
        since = max(up_hours) + 1
        return ProtoVerdict(DIED, since, max(0, now_b - since))
    since = min(seen_hours)
    return ProtoVerdict(NEVER_UP, since, max(0, now_b - since))


def aggregate_by_check(rows: list[dict], judged: dict[str, int]) -> list[dict]:
    """Group per-host findings into one row per check key. Pure.

    ``judged`` is the number of hosts on which each key could actually be
    judged — the denominator. Without it the ratio would silently count only
    the hosts that failed, and every key would read as 100% dead.
    """
    by_key: dict[str, list[dict]] = {}
    for r in rows:
        by_key.setdefault(r["key"], []).append(r)
    out = []
    for key, found in by_key.items():
        total = judged.get(key, 0)
        dead = len(found)
        fleet_wide = total >= _MIN_FLEET_JUDGED and dead >= total
        out.append({
            "key": key,
            "dead": dead,
            "judged": total,
            "fleet_wide": fleet_wide,
            "hosts": sorted(r["hostname"] for r in found),
            "worst_h": max((r["dead_h"] for r in found), default=0),
            "kinds": sorted({r["kind"] for r in found}),
        })
    out.sort(key=lambda r: (not r["fleet_wide"], -r["dead"], -r["worst_h"]))
    return out


def _bucket_label(b: int) -> str:
    return datetime.fromtimestamp(b * 3600, tz=timezone.utc).strftime("%m-%d %H:00")


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()) -> None:

    if "detect_dead_protocols" not in skip:

        @mcp.tool()
        async def detect_dead_protocols(
            group: str = "",
            product: str = "",
            country: str = "",
            region: str = "",
            window_hours: int = _DEAD_WINDOW_H,
            max_hosts: int = _MAX_HOSTS,
            max_results: int = 25,
            include_test: bool = False,
            instance: str = "",
        ) -> str:
            """Find protocol checks that are dead while their host looks healthy.

            A host counts as UP when ANY protocol answers, so a protocol can
            fail completely and leave no mark on the SLA or the problem list.
            This is the surface for that blind spot.

            Results group by CHECK first: one protocol dead on every host is a
            platform outage with one owner, not N host faults, and the
            dead/judged ratio is what tells those apart. Discovery walks every
            `*check*` item rather than the three configured service keys —
            looking only at configured keys is how a fleet-wide outage stayed
            invisible.

            States: **died** (passed earlier, then went silent — a regression,
            timestamped), **never up** (zero passes since first seen — a
            provisioning gap). Hosts that are entirely dark, and items with too
            few observed hours to judge, are counted and named separately
            rather than guessed at.

            Args:
                group: Zabbix host group (optional)
                product: Filter by product (optional)
                country: Country code filter (optional)
                region: LATAM, APAC, EMEA, NA, CIS, ALL (optional)
                window_hours: Silence window that counts as dead (default: 24)
                max_hosts: Cap on hosts inspected; truncation is disclosed
                    (default: 400)
                max_results: Max rows per section (default: 25)
                include_test: Keep test/staging hosts (default: False)
                instance: Zabbix instance (optional)
            """
            try:
                client = resolver.resolve(instance)
                win = max(1, int(window_hours))

                hosts = await client.call("host.get", {
                    "output": ["hostid", "host"],
                    "selectGroups": ["name"],
                    "filter": {"status": "0"},
                })
                excluded: list[dict] = []
                if not include_test:
                    hosts, excluded = partition_test_hosts(hosts)
                host_map = {h["hostid"]: h for h in hosts}

                filtered = []
                for h in hosts:
                    prod, _ = _classify_host(h.get("groups", []))
                    if product and product.lower() not in (prod or "").lower():
                        continue
                    if group and not any(
                        g["name"].lower() == group.lower()
                        for g in h.get("groups", [])
                    ):
                        continue
                    if country and extract_country(h.get("host", "")).lower() != country.lower():
                        continue
                    if region:
                        rc = countries_for_region(region)
                        if rc and extract_country(h.get("host", "")).upper() not in rc:
                            continue
                    filtered.append(h["hostid"])
                if not filtered:
                    return "No servers match the filter." + excluded_test_note(excluded)

                truncated = max(0, len(filtered) - max_hosts)
                filtered = filtered[:max_hosts]

                # Every *check* item, not just the configured service keys.
                # nft.* are firewall-table assertions, not reachability, and
                # would drown the protocol signal.
                items = await client.call("item.get", {
                    "hostids": filtered,
                    "output": ["itemid", "hostid", "key_", "name", "value_type"],
                    "search": {"key_": "*check*"},
                    "searchWildcardsEnabled": True,
                    "filter": {"status": "0"},
                })
                checks = [
                    it for it in items or []
                    if not str(it.get("key_", "")).startswith("nft.")
                    and str(it.get("value_type", "")) == "3"   # uint 0/1
                ]
                if not checks:
                    return (
                        "No 0/1 protocol-check items found in this scope."
                        + excluded_test_note(excluded)
                    )

                now = int(_time.time())
                now_b = now // 3600
                # value_MAX, never value_avg: a uint hour collapses to its min
                # under Zabbix integer division, so avg cannot tell "answered
                # once" from "never answered" (ADR 0048).
                trends = await client.call("trend.get", {
                    "itemids": [c["itemid"] for c in checks],
                    "time_from": now - (win + 2) * 3600,
                    "output": ["itemid", "clock", "value_max"],
                    "limit": len(checks) * (win + 4) + 1000,
                })
                up_h: dict[str, set] = {}
                seen_h: dict[str, set] = {}
                for t in trends or []:
                    try:
                        iid = t["itemid"]
                        b = int(t["clock"]) // 3600
                        vmax = float(t["value_max"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    seen_h.setdefault(iid, set()).add(b)
                    if vmax >= 0.5:
                        up_h.setdefault(iid, set()).add(b)

                # Host is up for an hour if ANY of its checks answered — the
                # very rule this tool exists to see behind.
                host_up: dict[str, set] = {}
                for c in checks:
                    hid = c["hostid"]
                    host_up.setdefault(hid, set()).update(up_h.get(c["itemid"], set()))

                rows: list[dict] = []
                judged: dict[str, int] = {}
                counts: dict[str, int] = {}
                unjudged_hosts: set[str] = set()
                for c in checks:
                    iid, hid = c["itemid"], c["hostid"]
                    key = str(c.get("key_", "")).split("[")[0]
                    v = classify_protocol(
                        up_h.get(iid, set()), seen_h.get(iid, set()),
                        host_up.get(hid, set()), now_b, window_h=win,
                    )
                    counts[v.state] = counts.get(v.state, 0) + 1
                    if v.state in (TOO_YOUNG, HOST_DARK):
                        unjudged_hosts.add(host_map.get(hid, {}).get("host", hid))
                        continue
                    judged[key] = judged.get(key, 0) + 1
                    if v.state == ALIVE:
                        continue
                    rows.append({
                        "hostname": host_map.get(hid, {}).get("host", hid),
                        "key": key, "kind": v.state, "dead_h": v.dead_h,
                        "since": _bucket_label(v.since_bucket) if v.since_bucket else "?",
                    })

                header = (
                    f"**Dead protocols** ({win}h window, {len(filtered)} hosts, "
                    f"{len(checks)} checks; {counts.get(ALIVE, 0)} alive, "
                    f"{counts.get(DIED, 0)} died, {counts.get(NEVER_UP, 0)} never up, "
                    f"{counts.get(TOO_YOUNG, 0)} too young, "
                    f"{counts.get(HOST_DARK, 0)} on dark hosts)\n"
                )
                notes = ""
                if truncated:
                    notes += (f"\n\n_{truncated} host(s) beyond the {max_hosts} cap "
                              "were not inspected._")
                if unjudged_hosts:
                    names = sorted(unjudged_hosts)
                    notes += (
                        f"\n\n_{len(names)} host(s) had checks that could NOT be "
                        f"judged (too few observed hours, or the whole host was "
                        f"dark): {', '.join(names[:5])}"
                        f"{'…' if len(names) > 5 else ''}. Not counted as healthy._"
                    )
                if not rows:
                    return (header + "\nEvery judged protocol check answered "
                            "within the window." + notes + excluded_test_note(excluded))

                agg = aggregate_by_check(rows, judged)
                fleet = [a for a in agg if a["fleet_wide"]]
                partial = [a for a in agg if not a["fleet_wide"]]

                parts = [header]
                if fleet:
                    parts.append(
                        "\n**Dead on EVERY judged host — platform, not host:**\n"
                    )
                    parts.append("| Check | Dead / judged | Kind | Worst |")
                    parts.append("|-------|--------------:|------|------:|")
                    for a in fleet[:max_results]:
                        parts.append(
                            f"| `{a['key']}` | **{a['dead']}/{a['judged']}** | "
                            f"{', '.join(a['kinds'])} | {a['worst_h']}h |"
                        )
                if partial:
                    parts.append("\n**Dead on some hosts:**\n")
                    parts.append("| Check | Dead / judged | Kind | Worst | Hosts |")
                    parts.append("|-------|--------------:|------|------:|-------|")
                    for a in partial[:max_results]:
                        shown = ", ".join(a["hosts"][:3])
                        more = f" +{len(a['hosts']) - 3}" if len(a["hosts"]) > 3 else ""
                        parts.append(
                            f"| `{a['key']}` | {a['dead']}/{a['judged']} | "
                            f"{', '.join(a['kinds'])} | {a['worst_h']}h | {shown}{more} |"
                        )
                parts.append(
                    "\n_`died` = answered earlier in the window then went silent "
                    "(a regression — match the timestamp to a deploy). `never up` "
                    "= zero passes since first seen (a provisioning gap). Neither "
                    "shows on the SLA, because a host is UP whenever any protocol "
                    "answers._"
                )
                return "\n".join(parts) + notes + excluded_test_note(excluded)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"
