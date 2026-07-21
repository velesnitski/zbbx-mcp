"""Minute-level service-check flap analysis (task 174, ADR 090).

A live flap audit proved three things the trigger layer cannot see:

- same-minute dips on geographically **distant** hosts are Zabbix
  prober/egress noise, not host problems — they must be subtracted before
  scoring any host;
- a TEST-class check flaps far more than the production check on the same
  host (script noise, weight ~0);
- chronically degraded hosts — production check failing in *most* hours,
  a minute or two at a time — fire **zero** triggers, because
  consecutive-fail triggers are blind to short dips. The truly sick end of
  the fleet is invisible to alerting.

``detect_check_flaps`` pulls raw minute history for the configured
service-check items and classifies every flap-minute, in priority order:

1. fleet-correlated (same minute, >=2 distant hosts) -> prober noise, discard;
2. host-correlated (same minute, >=2 prod services on ONE host) -> real host
   event, count;
3. TEST-only isolated -> script noise, tracked separately, weight 0;
4. residual per-host prod flap rate (dip-min/day) -> honest health rank.
"""

from __future__ import annotations

import re
import time as _time

import httpx

from zbbx_mcp.data import (
    KEY_service_PRIMARY,
    KEY_service_SECONDARY,
    KEY_service_TERTIARY,
    excluded_test_note,
    extract_country,
    partition_test_hosts,
)
from zbbx_mcp.resolver import InstanceResolver

_MAX_HOSTS = 12          # hard cap on fan-out (minute history is heavy)
_MAX_WINDOW_DAYS = 7     # raw history retention bound
# "[TEST]"-class check names: token-bounded, bracket-aware. An item named
# "Service TEST check" or "[TEST] service" matches; "latest"/"attestation"
# never do (same lookalike class as the host-name pattern, ADR 080).
_TEST_ITEM_RE = re.compile(r"(?:^|[-_\s(\[])test(?:[-_\s)\]]|$)", re.IGNORECASE)


def is_test_check(item_name: str) -> bool:
    """True if the item name marks a TEST-class (non-production) check. Pure."""
    return bool(_TEST_ITEM_RE.search(item_name or ""))


def classify_flap_minutes(
    dips: dict[tuple[str, str], set[int]],
    test_items: set[str],
    host_country: dict[str, str],
    window_days: float,
) -> dict[str, dict]:
    """Classify per-(host,item) dip minutes into noise vs real. Pure.

    ``dips`` maps ``(hostid, itemid)`` to the set of minute-buckets
    (``clock // 60``) where the check read 0. ``test_items`` is the set of
    TEST-class itemids. ``host_country`` maps hostid -> ISO2 ("" unknown).

    Priority order per minute (a minute consumed by an earlier class never
    reaches a later one):

    1. **fleet noise** — prod checks dip the same minute on hosts spanning
       >=2 distinct countries (or >=3 hosts when countries are unknown):
       the prober/egress blinked, not the fleet. Discarded for every host.
    2. **host event** — >=2 distinct prod checks on ONE host dip the same
       minute: something real happened on that box.
    3. **test noise** — a TEST-class check dips alone: script noise,
       tracked separately, weight zero.
    4. **prod flap** — residual single-service prod dips; with (2) they
       form the honest per-host rate (dip-min/day).

    Returns per-host dicts with minute counts and ``rate_per_day``.
    """
    # minute -> {hostid -> set of prod itemids dipping}
    prod_by_minute: dict[int, dict[str, set[str]]] = {}
    for (hid, iid), minutes in dips.items():
        if iid in test_items:
            continue
        for m in minutes:
            prod_by_minute.setdefault(m, {}).setdefault(hid, set()).add(iid)

    fleet_noise_minutes: set[int] = set()
    for m, per_host in prod_by_minute.items():
        countries = {host_country.get(h, "") for h in per_host}
        countries.discard("")
        if len(countries) >= 2 or len(per_host) >= 3:
            fleet_noise_minutes.add(m)

    days = max(window_days, 1e-9)
    out: dict[str, dict] = {}

    def _host(hid: str) -> dict:
        return out.setdefault(hid, {
            "fleet_noise_min": 0, "host_event_min": 0,
            "prod_flap_min": 0, "test_noise_min": 0, "rate_per_day": 0.0,
        })

    for m, per_host in prod_by_minute.items():
        for hid, iids in per_host.items():
            h = _host(hid)
            if m in fleet_noise_minutes:
                h["fleet_noise_min"] += 1
            elif len(iids) >= 2:
                h["host_event_min"] += 1
            else:
                h["prod_flap_min"] += 1

    for (hid, iid), minutes in dips.items():
        if iid not in test_items:
            continue
        h = _host(hid)
        for m in minutes:
            if m not in fleet_noise_minutes:
                h["test_noise_min"] += 1

    for h in out.values():
        h["rate_per_day"] = round(
            (h["host_event_min"] + h["prod_flap_min"]) / days, 2
        )
    return out


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()) -> None:

    if "detect_check_flaps" not in skip:

        @mcp.tool()
        async def detect_check_flaps(
            hosts: str = "",
            group: str = "",
            country: str = "",
            window_days: int = 3,
            max_hosts: int = 8,
            chronic_min_per_day: float = 10.0,
            max_results: int = 15,
            include_test: bool = False,
            instance: str = "",
        ) -> str:
            """Fleet flap matrix from raw minute history — noise/real split.

            Classifies every service-check flap-minute: fleet-correlated
            (prober noise, subtracted), host-correlated (real event),
            TEST-check-only (script noise), residual prod flaps (honest
            rate). Surfaces chronic flappers that fire zero triggers.

            Args:
                hosts: Comma/space-separated host names (optional)
                group: Host group (optional) — one of hosts/group/country required
                country: Country filter (optional)
                window_days: History window, capped at 7 (default: 3)
                max_hosts: Fan-out cap, hard max 12 (default: 8 — minute
                    history is heavy)
                chronic_min_per_day: Rate marking a host chronic (default: 10)
                max_results: Matrix rows shown (default: 15)
                include_test: Keep test/staging *hosts* in a scoped sweep
                    (explicitly named hosts are always analyzed)
                instance: Zabbix instance (optional)
            """
            try:
                host_list = [
                    h.strip() for h in (hosts or "").replace(",", " ").split()
                    if h.strip()
                ]
                if not host_list and not group and not country:
                    return "At least one of `hosts`, `group`, or `country` is required."
                service_keys = [
                    k for k in (KEY_service_PRIMARY, KEY_service_SECONDARY,
                                KEY_service_TERTIARY) if k
                ]
                if not service_keys:
                    return "No service check keys configured."

                client = resolver.resolve(instance)
                days = max(1, min(int(window_days), _MAX_WINDOW_DAYS))
                cap = max(1, min(int(max_hosts), _MAX_HOSTS))
                now = int(_time.time())
                time_from = now - days * 86400

                params: dict = {
                    "output": ["hostid", "host"],
                    "selectGroups": ["name"],
                    "filter": {"status": "0"},
                }
                filt: dict = {"status": "0"}
                if host_list:
                    filt["host"] = host_list
                params["filter"] = filt
                if group:
                    groups = await client.call("hostgroup.get", {
                        "output": ["groupid"], "filter": {"name": [group]},
                    })
                    if not groups:
                        return f"Host group '{group}' not found."
                    params["groupids"] = [g["groupid"] for g in groups]
                records = await client.call("host.get", params)
                if country:
                    cc = country.strip().upper()[:2]
                    records = [r for r in records
                               if extract_country(r.get("host", "")) == cc]

                excluded: list[dict] = []
                if not include_test and not host_list:
                    records, excluded = partition_test_hosts(records)
                if not records:
                    return "No matching hosts." + excluded_test_note(excluded)
                truncated = len(records) - cap if len(records) > cap else 0
                records = records[:cap]
                hostids = [r["hostid"] for r in records]
                host_name = {r["hostid"]: r["host"] for r in records}
                host_country = {
                    hid: extract_country(name) for hid, name in host_name.items()
                }

                items = await client.call("item.get", {
                    "hostids": hostids,
                    "output": ["itemid", "hostid", "name", "key_", "value_type"],
                    "filter": {"key_": service_keys, "status": "0"},
                })
                if not items:
                    return ("No service check items on the selected hosts."
                            + excluded_test_note(excluded))
                test_items = {i["itemid"] for i in items
                              if is_test_check(i.get("name", ""))}
                item_host = {i["itemid"]: i["hostid"] for i in items}

                # Raw minute history, one query per value_type present.
                dips: dict[tuple[str, str], set[int]] = {}
                total_samples = 0
                by_vtype: dict[int, list[str]] = {}
                for i in items:
                    try:
                        vt = int(i.get("value_type", 3))
                    except (TypeError, ValueError):
                        vt = 3
                    by_vtype.setdefault(vt, []).append(i["itemid"])
                for vt, iids in by_vtype.items():
                    history = await client.call("history.get", {
                        "itemids": iids,
                        "history": vt,
                        "time_from": time_from,
                        "output": ["itemid", "clock", "value"],
                        "limit": len(iids) * days * 1440 + 1000,
                    })
                    for rec in history:
                        total_samples += 1
                        try:
                            if float(rec.get("value", 1)) != 0:
                                continue
                            minute = int(rec["clock"]) // 60
                        except (TypeError, ValueError):
                            continue
                        hid = item_host.get(rec.get("itemid", ""))
                        if hid:
                            dips.setdefault((hid, rec["itemid"]), set()).add(minute)

                if total_samples == 0:
                    return ("No raw history in the window (check history "
                            "retention)." + excluded_test_note(excluded))

                stats = classify_flap_minutes(
                    dips, test_items, host_country, float(days))

                # Problem events per host in the window — the "trigger count"
                # side of the chronic-but-silent split.
                events = await client.call("event.get", {
                    "hostids": hostids,
                    "source": 0, "object": 0, "value": 1,
                    "time_from": time_from,
                    "output": ["eventid", "objectid"],
                    "selectHosts": ["hostid"],
                    "limit": 2000,
                })
                event_count: dict[str, int] = {}
                for e in events:
                    for h in e.get("hosts", []) or []:
                        hid = h.get("hostid")
                        if hid in host_name:
                            event_count[hid] = event_count.get(hid, 0) + 1

                ranked = sorted(
                    hostids,
                    key=lambda h: -stats.get(h, {}).get("rate_per_day", 0.0),
                )
                fleet_noise_total = sum(
                    s["fleet_noise_min"] for s in stats.values())
                test_noise_total = sum(
                    s["test_noise_min"] for s in stats.values())

                parts = [
                    f"**Check-flap matrix** ({days}d, {len(hostids)} hosts, "
                    f"{len(items)} checks, {total_samples:,} samples)\n",
                    "| Host | Rate/day | Host events | Prod flaps | "
                    "Test noise | Fleet noise | Problem events |",
                    "|------|---------:|------------:|-----------:|"
                    "-----------:|------------:|---------------:|",
                ]
                for hid in ranked[:max_results]:
                    s = stats.get(hid, {
                        "fleet_noise_min": 0, "host_event_min": 0,
                        "prod_flap_min": 0, "test_noise_min": 0,
                        "rate_per_day": 0.0,
                    })
                    parts.append(
                        f"| {host_name[hid]} | {s['rate_per_day']} | "
                        f"{s['host_event_min']} | {s['prod_flap_min']} | "
                        f"{s['test_noise_min']} | {s['fleet_noise_min']} | "
                        f"{event_count.get(hid, 0)} |"
                    )
                if len(ranked) > max_results:
                    parts.append(f"\n*{len(ranked) - max_results} more hosts omitted*")

                parts.append(
                    f"\nNoise subtracted fleet-wide: {fleet_noise_total} "
                    f"prober-correlated min, {test_noise_total} TEST-check min."
                )
                candidates = [
                    host_name[hid] for hid in ranked
                    if stats.get(hid, {}).get("rate_per_day", 0.0)
                    >= chronic_min_per_day
                    and event_count.get(hid, 0) == 0
                ]
                if candidates:
                    parts.append(
                        f"\n**Rate-based trigger candidates** (chronic ≥ "
                        f"{chronic_min_per_day:g} min/day, zero problem events "
                        f"— invisible to consecutive-fail triggers): "
                        + ", ".join(candidates[:10])
                        + (f" (+{len(candidates) - 10} more)"
                           if len(candidates) > 10 else "")
                    )
                if truncated:
                    parts.append(
                        f"\n_{truncated} matching host(s) beyond the "
                        f"max_hosts={cap} cap were not analyzed._"
                    )
                return "\n".join(parts) + excluded_test_note(excluded)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"
