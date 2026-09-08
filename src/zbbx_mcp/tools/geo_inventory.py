"""``get_geo_inventory`` — the fleet in a country by where the boxes ARE.

Every ``country`` filter in this server matches the two-letter code in the
**hostname**. That is a naming convention, not a location, and the two drift:
a host named for one country can sit in a datacenter in another, and a
datacenter can be full of hosts named for somewhere else. Five tools already
resolve the real datacenter from the IP and print it in a City column — none
of them lets you *filter* by it. Asked "what serves this country", the only
honest answer was a hand-merge of several truncated tables.

This tool filters on the resolved datacenter and says three things a
hostname-based count cannot:

- which hosts are physically in the country, split Free / Paid and by tier,
  with carrier-NIC traffic and CPU;
- which hosts are **named** for the country but sit elsewhere — mislabelled
  out, with their real city;
- which hosts named for the country could not be resolved at all — reported
  as unknown, never silently dropped and never silently counted.

Real geo comes from ``resolve_datacenter`` (ADR 122). Where the deployment's
``ZABBIX_DATACENTER_CIDRS`` has no range for an address, there is no city,
and the host lands in the unresolved bucket. That is the correct reading: the
tool does not know, and says so.
"""

from __future__ import annotations

import httpx

from zbbx_mcp.classify import classify_host as _classify_host
from zbbx_mcp.classify import resolve_datacenter
from zbbx_mcp.data import (
    excluded_test_note,
    extract_country,
    host_ip,
    label_matches,
    partition_test_hosts,
)
from zbbx_mcp.fetch import (
    fetch_cpu_map,
    fetch_enabled_hosts,
    physical_traffic_items,
    to_mbps,
)
from zbbx_mcp.resolver import InstanceResolver
from zbbx_mcp.tools.traffic_totals import carrier_traffic

#: Tier words that mean "not paid". Mirrors ``get_product_summary`` — keep the
#: two in step, since Free/Paid must mean the same thing on every surface.
_FREE_WORDS = ("free", "relay", "basic")


def datacenter_cc(city: str | None) -> str:
    """Country code out of a resolved ``"City, CC"`` string; ``""`` if none.

    ``resolve_datacenter`` renders location as ``"Gravelines, FR"``. The code is
    the last comma-separated token. An empty or malformed string yields ``""``,
    which callers must treat as *unknown*, not as *not this country*.
    """
    if not city or ", " not in city:
        return ""
    return city.rsplit(", ", 1)[-1].strip().upper()


def is_free_tier(tier: str | None) -> bool:
    t = (tier or "").lower()
    return any(w in t for w in _FREE_WORDS)


def bucket_hosts(hosts: list[dict], country: str) -> dict[str, list[dict]]:
    """Sort hosts into ``in_geo`` / ``named_elsewhere`` / ``unresolved``. Pure.

    Each host dict gains ``_cc_name``, ``_cc_dc``, ``_city``, ``_provider``,
    ``_product``, ``_tier`` so callers do not re-derive them.

    ``unresolved`` is hosts whose *name* claims the country but whose IP resolves
    to no datacenter. They are neither in nor out. Folding them into ``in_geo``
    would assert a location nobody measured; dropping them would make a
    coverage gap read as a smaller fleet.
    """
    want = country.strip().upper()
    out: dict[str, list[dict]] = {"in_geo": [], "named_elsewhere": [], "unresolved": []}
    for h in hosts:
        ip = host_ip(h)
        provider, city = resolve_datacenter(ip) if ip else ("Unknown", "")
        h["_provider"], h["_city"] = provider, city
        h["_cc_dc"] = datacenter_cc(city)
        h["_cc_name"] = (extract_country(h.get("host", "")) or "").upper()
        h["_product"], h["_tier"] = _classify_host(h.get("groups", []))
        if h["_cc_dc"] == want:
            out["in_geo"].append(h)
        elif h["_cc_name"] == want and h["_cc_dc"]:
            out["named_elsewhere"].append(h)
        elif h["_cc_name"] == want and not h["_cc_dc"]:
            out["unresolved"].append(h)
    return out


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()) -> None:
    """Register the real-geo inventory tool."""
    if "get_geo_inventory" not in skip:

        @mcp.tool()
        async def get_geo_inventory(
            country: str,
            product: str = "",
            tier: str = "",
            include_test: bool = False,
            top: int = 5,
            instance: str = "",
        ) -> str:
            """What serves a country, by where the servers physically ARE.

            Filters on the datacenter resolved from each host's IP — NOT on the
            country code in the hostname, which every other `country` filter
            uses and which is a naming convention rather than a location.

            Reports the in-country fleet split Free / Paid and by tier, with
            carrier-NIC traffic and CPU; then names the hosts whose NAME claims
            this country but which sit elsewhere (with their real city); then
            the hosts named for this country whose location could not be
            resolved at all — disclosed as unknown, never counted in or out.

            Args:
                country: 2-letter datacenter country code, e.g. FR
                product: Exact product label (optional)
                tier: Exact tier label (optional)
                include_test: Keep test hosts (default False)
                top: Largest contributors to list (default 5)
                instance: Zabbix instance (optional)
            """
            try:
                want = (country or "").strip().upper()
                if len(want) != 2 or not want.isalpha():
                    return "country must be a 2-letter code, e.g. FR."
                client = resolver.resolve(instance)
                hosts = await fetch_enabled_hosts(client, groups=True, interfaces=True)
                excluded: list[dict] = []
                if not include_test:
                    hosts, excluded = partition_test_hosts(hosts)

                b = bucket_hosts(hosts, want)
                keep = [
                    h for h in b["in_geo"]
                    if label_matches(h["_product"], product) and label_matches(h["_tier"], tier)
                ]
                filt = ", ".join(f"{k}={v}" for k, v in (("product", product), ("tier", tier)) if v)
                head = f"Datacenter inventory — {want}" + (f" ({filt})" if filt else "")
                if not keep:
                    return (
                        f"{head}\n\nNo enabled hosts resolve to a datacenter in {want}"
                        + (f" matching {filt}" if filt else "") + "."
                        + _disclosures(b, want, excluded)
                    )

                ids = [h["hostid"] for h in keep]
                items = await physical_traffic_items(
                    client, ids, direction="in",
                    output=("itemid", "hostid", "key_", "lastvalue", "lastclock"),
                )
                per_host, _never = carrier_traffic(items)
                cpu = await fetch_cpu_map(client, ids)

                # Roll up by (product, tier). Traffic is only summed over hosts
                # that have a live reading; the shortfall is stated per row.
                rows: dict[tuple[str, str], dict] = {}
                for h in keep:
                    k = (h["_product"] or "Unknown", h["_tier"] or "Unknown")
                    r = rows.setdefault(k, {"n": 0, "bps": 0.0, "covered": 0, "cpu": [], "kind": "Free" if is_free_tier(k[1]) else "Paid"})
                    r["n"] += 1
                    v = per_host.get(str(h["hostid"]))
                    if v is not None:
                        r["bps"] += v
                        r["covered"] += 1
                    c = cpu.get(h["hostid"])
                    if c is not None:
                        r["cpu"].append(c)

                kinds = {"Free": {"n": 0, "bps": 0.0, "covered": 0}, "Paid": {"n": 0, "bps": 0.0, "covered": 0}}
                for r in rows.values():
                    kinds[r["kind"]]["n"] += r["n"]
                    kinds[r["kind"]]["bps"] += r["bps"]
                    kinds[r["kind"]]["covered"] += r["covered"]

                parts = [head, "", f"**{len(keep)} host(s)** physically in {want}", ""]
                parts.append("| | hosts | traffic | covered |")
                parts.append("|---|---:|---:|---:|")
                for kind in ("Free", "Paid"):
                    kd = kinds[kind]
                    parts.append(f"| **{kind}** | {kd['n']} | {to_mbps(kd['bps']):,.0f} Mbps | {kd['covered']}/{kd['n']} |")
                parts.append("")
                parts.append("| Product / Tier | type | hosts | traffic | Mbps/host | avg CPU |")
                parts.append("|---|---|---:|---:|---:|---:|")
                for (prod, tr), r in sorted(rows.items(), key=lambda kv: -kv[1]["bps"]):
                    mb = to_mbps(r["bps"])
                    per = f"{mb / r['covered']:,.0f}" if r["covered"] else "–"
                    cpu_s = f"{sum(r['cpu']) / len(r['cpu']):.0f}%" if r["cpu"] else "–"
                    cov = "" if r["covered"] == r["n"] else f" ({r['covered']}/{r['n']} measured)"
                    parts.append(f"| {prod} / {tr} | {r['kind']} | {r['n']} | {mb:,.0f} Mbps{cov} | {per} | {cpu_s} |")

                ranked = sorted(keep, key=lambda h: per_host.get(str(h["hostid"]), -1.0), reverse=True)
                if top > 0:
                    parts.append("")
                    parts.append(f"Top {min(top, len(ranked))} by traffic:")
                    for h in ranked[:top]:
                        v = per_host.get(str(h["hostid"]))
                        parts.append(
                            f"- {h['host']} — {to_mbps(v):,.0f} Mbps — {h['_city'] or '?'}"
                            + ("" if h["_cc_name"] == want else f" (named `{h['_cc_name'].lower() or '??'}`)")
                            if v is not None else f"- {h['host']} — no traffic reading — {h['_city'] or '?'}"
                        )

                named_in = [h for h in keep if h["_cc_name"] and h["_cc_name"] != want]
                if named_in:
                    from collections import Counter
                    by_name = Counter(h["_cc_name"].lower() for h in named_in)
                    parts.append(
                        f"\n_{len(named_in)} of the {len(keep)} are named for another country "
                        f"({', '.join(f'`{k}`×{v}' for k, v in by_name.most_common())}) — "
                        "a hostname-based count would have missed them entirely._"
                    )
                parts.append(_disclosures(b, want, excluded))
                return "\n".join(parts)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error building geo inventory: {e}"


def _disclosures(b: dict[str, list[dict]], want: str, excluded: list[dict]) -> str:
    out: list[str] = []
    if b["named_elsewhere"]:
        from collections import Counter
        where = Counter(h["_city"] for h in b["named_elsewhere"])
        ex = ", ".join(f"{h['host']} → {h['_city']}" for h in b["named_elsewhere"][:6])
        more = len(b["named_elsewhere"]) - 6
        out.append(
            f"\n_**{len(b['named_elsewhere'])} host(s) are NAMED `{want.lower()}` but sit elsewhere** — "
            f"{', '.join(f'{c}×{n}' for c, n in where.most_common())}. "
            f"Excluded from the figures above: {ex}{f' (+{more} more)' if more > 0 else ''}._"
        )
    if b["unresolved"]:
        ex = ", ".join(h["host"] for h in b["unresolved"][:8])
        more = len(b["unresolved"]) - 8
        out.append(
            f"\n_**{len(b['unresolved'])} host(s) named `{want.lower()}` could not be placed** — no "
            f"datacenter range covers their IP. Neither counted in nor out: {ex}"
            f"{f' (+{more} more)' if more > 0 else ''}. Add their ranges to ZABBIX_DATACENTER_CIDRS "
            "(`build_datacenter_overrides` drafts it) to settle them._"
        )
    if excluded:
        out.append(excluded_test_note(excluded))
    return "\n".join(out)
