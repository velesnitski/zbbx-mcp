"""``get_app_view`` — what the client app offers, joined to live Zabbix by IP.

Every other "what serves X" surface in this server answers from Zabbix's own
notions of X: the code in a hostname (``country=``), the datacenter an IP
resolves to (``get_geo_inventory``), the host group (``product`` / ``tier``).
None of them is what a user sees. The product's catalogue — audiences,
sections, entries, and the member servers each entry hands out — lives in
the product's database and reaches this tool as a map (``ZABBIX_APP_MAP``,
ADR 143). This tool takes that map as given and answers the question the
map cannot: how are the servers behind each entry doing right now?

Per entry it reports members / matched / unmatched — an unmatched address
is NAMED, never counted as a zero-traffic server — then carrier-NIC traffic
(total and median per matched server), CPU median and max, the servers
whose agent is not reporting, and the entry's own ``display_load`` where
the map carries one. A section and audience rollup follows.

The map states when it was generated and under what predicate its rows were
selected. Both are printed, and a map older than the predicate's own window
is flagged stale: the catalogue has moved on and the figures describe an
earlier one.
"""

from __future__ import annotations

import time as _time
from statistics import median

import httpx

from zbbx_mcp.app_map import (
    APP_MAP_ENV,
    AppMap,
    Audience,
    Entry,
    Section,
    index_hosts_by_ip,
    join_entry,
    load_app_map,
    map_age_s,
    predicate_window_s,
)
from zbbx_mcp.data import label_matches
from zbbx_mcp.fetch import (
    fetch_cpu_map,
    fetch_enabled_hosts,
    physical_traffic_items,
    to_mbps,
)
from zbbx_mcp.resolver import InstanceResolver
from zbbx_mcp.tools.traffic_totals import carrier_traffic


def find_audience(app_map: AppMap, audience: str) -> Audience | None:
    """Exact (case-insensitive) audience lookup; empty means ``default``."""
    want = (audience or "").strip() or "default"
    return next((a for name, a in app_map.audiences.items() if label_matches(name, want)), None)


def scope_entries(aud: Audience, section: str = "", entry: str = "") -> list[tuple[Section, Entry]]:
    """``(section, entry)`` pairs in scope. Exact label matching (ADR 137):
    an entry matches on its key or its title, never on a substring. Pure."""
    return [
        (s, e)
        for s in aud.sections if label_matches(s.name, section)
        for e in s.entries if label_matches(e.key, entry) or label_matches(e.title, entry)
    ]


def entry_stats(
    matched: list[dict], unmatched: list[str], per_host: dict[str, float], cpu: dict[str, float],
) -> dict:
    """Fold one entry's joined hosts into figures with explicit coverage. Pure.

    Traffic is summed only over hosts with a live carrier reading, and the
    shortfall is carried as ``covered`` so the caller can print ``k/n``.
    ``silent`` is the matched hosts with no live CPU reading — the shared
    definition of "agent not reporting" (ADR 140), named rather than rendered
    as 0 % CPU.
    """
    bps = [per_host[str(h["hostid"])] for h in matched if str(h["hostid"]) in per_host]
    cpus = [cpu[h["hostid"]] for h in matched if h["hostid"] in cpu]
    return {
        "matched": len(matched),
        "unmatched": list(unmatched),
        "covered": len(bps),
        "bps": float(sum(bps)),
        "median_bps": float(median(bps)) if bps else None,
        "cpu_med": float(median(cpus)) if cpus else None,
        "cpu_max": float(max(cpus)) if cpus else None,
        "silent": [h.get("host", "") for h in matched if h["hostid"] not in cpu],
        "_bps": bps,
        "_cpus": cpus,
    }


def rollup(stats: list[dict]) -> dict:
    """Combine entry stats; the median is over every covered host, not over
    entry medians. Pure."""
    bps = [v for s in stats for v in s["_bps"]]
    cpus = [v for s in stats for v in s["_cpus"]]
    return {
        "entries": len(stats),
        "members": sum(s["matched"] + len(s["unmatched"]) for s in stats),
        "matched": sum(s["matched"] for s in stats),
        "unmatched": sum(len(s["unmatched"]) for s in stats),
        "covered": len(bps),
        "bps": float(sum(bps)),
        "median_bps": float(median(bps)) if bps else None,
        "cpu_med": float(median(cpus)) if cpus else None,
        "cpu_max": float(max(cpus)) if cpus else None,
        "silent": sum(len(s["silent"]) for s in stats),
    }


def fmt_age(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    if s < 86400:
        return f"{s // 3600} h {(s % 3600) // 60:02d} min"
    return f"{s // 86400} d {(s % 86400) // 3600} h"


def _mbps(bps: float | None) -> str:
    return "–" if bps is None else f"{to_mbps(bps):,.0f} Mbps"


def _cpu(med: float | None, mx: float | None) -> str:
    return "–" if med is None else f"{med:.0f}% / {mx:.0f}%"


def _cov(s: dict) -> str:
    return "" if s["covered"] == s["matched"] else f" ({s['covered']}/{s['matched']} measured)"


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()) -> None:
    """Register the app-catalogue view tool."""
    if "get_app_view" not in skip:

        @mcp.tool()
        async def get_app_view(
            audience: str = "default",
            section: str = "",
            entry: str = "",
            top: int = 5,
            instance: str = "",
        ) -> str:
            """What the client app offers, and how those servers are doing now.

            Reads the product's own catalogue from `ZABBIX_APP_MAP` (audiences →
            sections → entries → member servers by IP) — a map the tool is
            GIVEN, not one inferred from hostnames, datacenters or host groups —
            and joins every member IP to the enabled Zabbix hosts.

            Per entry: members / matched / unmatched (unmatched IPs are named,
            never counted as zero), carrier-NIC traffic total and median per
            matched server, CPU median and max, servers whose agent is not
            reporting (count and names), and the map's `display_load`. Then a
            section and audience rollup. The header states when the map was
            generated, its age and the selection predicate, and warns when
            the map is older than the predicate's own window.

            Args:
                audience: Exact audience name (default "default")
                section: Exact section name (optional)
                entry: Exact entry key or title (optional)
                top: Busiest matched servers to list (default 5)
                instance: Zabbix instance (optional)
            """
            try:
                app_map, why = load_app_map()
                if app_map is None:
                    return (
                        f"No app map available — {why}. Set {APP_MAP_ENV} to the exporter's "
                        "JSON file (or inline JSON); see ADR 143 for the shape."
                    )
                want_aud = (audience or "").strip() or "default"
                aud = find_audience(app_map, want_aud)
                if aud is None:
                    return (
                        f"Audience `{want_aud}` is not in the map. Audiences: "
                        + ", ".join(f"`{n}`" for n in app_map.audiences) + "."
                    )
                if section and not any(label_matches(s.name, section) for s in aud.sections):
                    return (
                        f"Section `{section}` is not in audience `{aud.name}`. Sections: "
                        + (", ".join(f"`{s.name}`" for s in aud.sections) or "none") + "."
                    )
                scope = scope_entries(aud, section, entry)
                if not scope:
                    pool = [s for s in aud.sections if label_matches(s.name, section)]
                    keys = [e.key for s in pool for e in s.entries]
                    where = f"section `{section}`" if section else f"audience `{aud.name}`"
                    return (
                        f"Entry `{entry}` is not in {where}. Entries: "
                        + (", ".join(f"`{k}`" for k in keys) or "none") + "."
                    )

                now = int(_time.time())
                age = map_age_s(app_map, now)
                window = predicate_window_s(app_map.predicate)

                client = resolver.resolve(instance)
                hosts = await fetch_enabled_hosts(client, groups=False, interfaces=True)
                by_ip = index_hosts_by_ip(hosts)
                joined = [(s, e, *join_entry(e, by_ip)) for s, e in scope]
                ids = list(dict.fromkeys(h["hostid"] for _s, _e, m, _u in joined for h in m))
                items = await physical_traffic_items(
                    client, ids, direction="in",
                    output=("itemid", "hostid", "key_", "lastvalue", "lastclock"),
                )
                per_host, _never = carrier_traffic(items)
                cpu = await fetch_cpu_map(client, ids, now)

                filt = ", ".join(
                    f"{k} `{v}`" for k, v in (("section", section), ("entry", entry)) if v
                )
                parts = [f"App view — audience `{aud.name}`" + (f", {filt}" if filt else ""), ""]
                parts.append(
                    f"_Map generated {app_map.generated_at.strftime('%Y-%m-%dT%H:%M:%SZ')} "
                    f"({fmt_age(age)} ago)" + (f", source: {app_map.source}" if app_map.source else "")
                    + (f"; rows satisfy: {app_map.predicate}" if app_map.predicate else "") + "._"
                )
                if age > window:
                    parts.append(
                        f"**Stale map** — {fmt_age(age)} old against a {window} s window; the "
                        "catalogue has likely moved on and the figures below describe an earlier one."
                    )
                parts.append("")
                parts.append(
                    "| Section / Entry | code | members | matched | unmatched | traffic | median/srv "
                    "| CPU med / max | not reporting | display load | map clients |"
                )
                parts.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")

                stats: list[tuple[Section, Entry, dict]] = []
                notes_unmatched: list[str] = []
                notes_silent: list[str] = []
                for s, e, matched, unmatched in joined:
                    st = entry_stats(matched, unmatched, per_host, cpu)
                    stats.append((s, e, st))
                    clients = [m.clients for m in e.members if m.clients is not None]
                    parts.append(
                        f"| {s.name} / {e.title} (`{e.key}`) | {e.code or '?'} | {len(e.members)} "
                        f"| {st['matched']} | {len(unmatched)} | {_mbps(st['bps'])}{_cov(st)} "
                        f"| {_mbps(st['median_bps'])} | {_cpu(st['cpu_med'], st['cpu_max'])} "
                        f"| {len(st['silent'])} | "
                        f"{'–' if e.display_load is None else f'{e.display_load:.2f}'} | "
                        f"{sum(clients) if clients else '–'} |"
                    )
                    if unmatched:
                        notes_unmatched.append(f"- `{e.key}`: {', '.join(unmatched)}")
                    if st["silent"]:
                        notes_silent.append(f"- `{e.key}`: {', '.join(st['silent'])}")

                if notes_unmatched:
                    parts.append("")
                    parts.append(
                        "**Unmatched members** — offered by the product, but no enabled Zabbix host "
                        "carries the address on any interface. Not in any figure above:"
                    )
                    parts.extend(notes_unmatched)
                if notes_silent:
                    parts.append("")
                    parts.append(
                        "**Agent not reporting** — matched, but no CPU reading within the live "
                        "window (ADR 140). Counted as matched, excluded from CPU:"
                    )
                    parts.extend(notes_silent)

                sections_in_scope = list(dict.fromkeys(s.name for s, _e, _st in stats))
                parts.append("")
                parts.append(
                    "| Rollup | entries | members | matched | unmatched | traffic | median/srv "
                    "| CPU med / max | not reporting |"
                )
                parts.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
                for name in sections_in_scope:
                    r = rollup([st for s, _e, st in stats if s.name == name])
                    parts.append(_rollup_row(f"Section {name}", r))
                r = rollup([st for _s, _e, st in stats])
                parts.append(_rollup_row(f"Audience {aud.name}", r))

                if top > 0:
                    ranked = sorted(
                        ((per_host[str(h["hostid"])], h["host"], e.key)
                         for _s, e, matched, _u in joined for h in matched
                         if str(h["hostid"]) in per_host),
                        reverse=True,
                    )
                    if ranked:
                        parts.append("")
                        parts.append(f"Top {min(top, len(ranked))} matched servers by traffic:")
                        parts.extend(
                            f"- {name} — {to_mbps(v):,.0f} Mbps — `{key}`" for v, name, key in ranked[:top]
                        )
                return "\n".join(parts)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error building app view: {e}"


def _rollup_row(label: str, r: dict) -> str:
    return (
        f"| **{label}** | {r['entries']} | {r['members']} | {r['matched']} | {r['unmatched']} "
        f"| {_mbps(r['bps'])}{_cov(r)} | {_mbps(r['median_bps'])} "
        f"| {_cpu(r['cpu_med'], r['cpu_max'])} | {r['silent']} |"
    )
