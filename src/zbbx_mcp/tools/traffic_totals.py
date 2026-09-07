"""``get_traffic_totals`` — one number for a slice of the fleet, with its coverage.

Every traffic surface in this server is per-host and sorted, and the response
budget truncates the list. Asked "how much traffic does this group carry", a
reader was left summing a table that had been cut off and extrapolating the
rest. That is not a measurement, and it is the wrong shape for the question:
a total is one number, and it should cost one call.

Coverage is the point, not a footnote. A host with no readable traffic item is
**counted as uncovered and named**, never folded into the total as zero. A
total over 200 hosts of which 40 are silent is a different fact from a total
over 200, and the two must not print the same. Same rule as ADR 128 (a check
nobody carries is not healthy) and ADR 136 (a value never fetched is not a
value): the boundary of the claim goes in the sentence.

Per host, the figure is the **carrier NIC** — the busiest physical inbound
interface (ADR 078/105) — so a box with an idle second port is not
double-counted and not halved.
"""

from __future__ import annotations

import httpx

from zbbx_mcp.classify import classify_host as _classify_host
from zbbx_mcp.data import (
    excluded_test_note,
    extract_country,
    label_matches,
    partition_test_hosts,
)
from zbbx_mcp.fetch import physical_traffic_items, to_mbps
from zbbx_mcp.resolver import InstanceResolver


def carrier_traffic(items: list[dict]) -> tuple[dict[str, float], int]:
    """``({hostid: carrier_bps}, never_collected)`` from traffic items. Pure.

    The carrier is the busiest interface per host. An item with
    ``lastclock <= 0`` has never produced a value and is skipped — and counted,
    so the caller can say how many readings were placeholders rather than
    quietly treating them as zero bits per second.
    """
    per_host: dict[str, float] = {}
    never = 0
    for it in items or []:
        try:
            if int(it.get("lastclock") or 0) <= 0:
                never += 1
                continue
            hid = str(it["hostid"])
            val = float(it.get("lastvalue"))
        except (KeyError, TypeError, ValueError):
            continue
        if val > per_host.get(hid, -1.0):
            per_host[hid] = val
    return per_host, never


def summarise(
    hosts: list[dict],
    per_host: dict[str, float],
    *,
    top: int = 5,
) -> dict:
    """Fold per-host carrier readings into a total with explicit coverage. Pure.

    ``hosts`` is the full slice; ``per_host`` only the ones with a live reading.
    The difference is the uncovered set, reported by name.
    """
    covered = [h for h in hosts if str(h["hostid"]) in per_host]
    uncovered = [h for h in hosts if str(h["hostid"]) not in per_host]
    total_bps = sum(per_host[str(h["hostid"])] for h in covered)
    ranked = sorted(covered, key=lambda h: per_host[str(h["hostid"])], reverse=True)
    return {
        "servers": len(hosts),
        "covered": len(covered),
        "uncovered": [h.get("host", "") for h in uncovered],
        "total_mbps": round(to_mbps(total_bps), 1),
        "avg_mbps_per_covered": (
            round(to_mbps(total_bps) / len(covered), 1) if covered else None
        ),
        "top": [
            (h.get("host", ""), round(to_mbps(per_host[str(h["hostid"])]), 1))
            for h in ranked[: max(int(top), 0)]
        ],
    }


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()) -> None:
    """Register the traffic totals tool."""
    if "get_traffic_totals" not in skip:

        @mcp.tool()
        async def get_traffic_totals(
            group: str = "",
            product: str = "",
            tier: str = "",
            country: str = "",
            include_test: bool = False,
            top: int = 5,
            instance: str = "",
        ) -> str:
            """Total inbound traffic for a slice of the fleet, as ONE number with
            its coverage stated.

            Sums each host's carrier NIC (busiest physical inbound interface)
            across every host matching the filters. Hosts with no readable
            traffic item are counted as UNCOVERED and named — never added as
            zero — so the total's denominator is explicit.

            Filters are exact, case-insensitive matches: ``tier="Free"`` is
            the Free tier alone, not every tier whose name starts with Free.

            Args:
                group: Exact host-group name (optional)
                product: Exact product label (optional)
                tier: Exact tier label (optional)
                country: 2-letter code from the hostname (optional)
                include_test: Keep test hosts (default False)
                top: How many largest contributors to list (default 5)
                instance: Zabbix instance (optional)
            """
            try:
                client = resolver.resolve(instance)
                hosts = await client.call("host.get", {
                    "output": ["hostid", "host"],
                    "selectGroups": ["name"],
                    "filter": {"status": "0"},
                })
                excluded: list[dict] = []
                if not include_test:
                    hosts, excluded = partition_test_hosts(hosts)

                slice_: list[dict] = []
                for h in hosts:
                    prod, host_tier = _classify_host(h.get("groups", []))
                    if not label_matches(prod, product):
                        continue
                    if not label_matches(host_tier, tier):
                        continue
                    if group and not any(
                        (g.get("name") or "").lower() == group.lower()
                        for g in h.get("groups", [])
                    ):
                        continue
                    if country and extract_country(h.get("host", "")).lower() != country.lower():
                        continue
                    slice_.append(h)

                filt = ", ".join(
                    f"{k}={v}" for k, v in (
                        ("group", group), ("product", product),
                        ("tier", tier), ("country", country),
                    ) if v
                ) or "whole fleet"
                if not slice_:
                    return f"No enabled hosts match ({filt})."

                items = await physical_traffic_items(
                    client,
                    [h["hostid"] for h in slice_],
                    direction="in",
                    output=("itemid", "hostid", "key_", "lastvalue", "lastclock"),
                )
                per_host, never = carrier_traffic(items)
                s = summarise(slice_, per_host, top=top)

                parts = [
                    f"Traffic total — {filt}",
                    "",
                    f"**{s['total_mbps']:,.1f} Mbps** across **{s['covered']} of "
                    f"{s['servers']}** host(s)"
                    + (f"; {s['avg_mbps_per_covered']:,.1f} Mbps per covered host"
                       if s["avg_mbps_per_covered"] is not None else ""),
                ]
                if s["top"]:
                    parts.append("")
                    parts.append(f"Top {len(s['top'])}:")
                    for name, mbps in s["top"]:
                        parts.append(f"- {name}: {mbps:,.1f} Mbps")
                if s["uncovered"]:
                    shown = ", ".join(s["uncovered"][:8])
                    more = len(s["uncovered"]) - 8
                    parts.append(
                        f"\n_{len(s['uncovered'])} host(s) carry NO readable inbound "
                        f"traffic item and are EXCLUDED from the total — not counted as "
                        f"zero: {shown}{f' (+{more} more)' if more > 0 else ''}. "
                        "The total above is a floor for the slice, not its whole._"
                    )
                if never:
                    parts.append(
                        f"_{never} traffic item(s) have never collected (lastclock=0) "
                        "and were ignored rather than read as 0 bps._"
                    )
                if excluded:
                    parts.append(excluded_test_note(excluded))
                return "\n".join(parts)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error computing traffic totals: {e}"
