"""Cost summary tools — read-only views of fleet cost totals and gaps.

Extracted from the former monolithic costs.py.
"""

import asyncio

import httpx

from zbbx_mcp.data import host_ip
from zbbx_mcp.resolver import InstanceResolver


def _render_cost_summary(
    *,
    prod_costs: dict[str, dict],
    prov_costs: dict[str, dict],
    prod_totals: dict[str, int],
    prov_totals: dict[str, int],
    costed: int,
    total_hosts: int,
    redact_partial: bool = False,
) -> str:
    """Render the get_cost_summary markdown report.

    When ``redact_partial`` is True (intended for externally-shared
    artifacts), filter both the per-product and per-provider tables
    to keep only rows where every server in that group has a cost
    macro set. The grand total is recomputed from the kept product
    rows, the "Servers with cost / Without" line is suppressed, and
    a footer flags that the output was filtered.

    Pure helper — testable without a Zabbix server.
    """
    if redact_partial:
        prod_view = {
            k: v for k, v in prod_costs.items()
            if v["count"] == prod_totals.get(k, v["count"])
        }
        prov_view = {
            k: v for k, v in prov_costs.items()
            if v["count"] == prov_totals.get(k, v["count"])
        }
        grand_total = sum(v["total"] for v in prod_view.values())
    else:
        prod_view = prod_costs
        prov_view = prov_costs
        grand_total = sum(v["total"] for v in prod_costs.values())

    parts = [
        f"**Cost Summary: ${grand_total:,.2f}/month "
        f"(${grand_total * 12:,.2f}/year)**",
    ]
    if not redact_partial:
        parts.append(f"Servers with cost: {costed} | Without: {total_hosts - costed}")
    parts.extend([
        "",
        "## By Product",
        "| Product / Tier | Servers | Cost/Month | Cost/Year |",
        "|---|---|---|---|",
    ])
    for key in sorted(prod_view, key=lambda x: -prod_view[x]["total"]):
        p = prod_view[key]
        parts.append(
            f"| {key} | {p['count']} | ${p['total']:,.2f} | ${p['total'] * 12:,.2f} |"
        )

    parts.extend([
        "",
        "## By Provider",
        "| Provider | Servers | Cost/Month | Cost/Year |",
        "|---|---|---|---|",
    ])
    for prov in sorted(prov_view, key=lambda x: -prov_view[x]["total"]):
        p = prov_view[prov]
        parts.append(
            f"| {prov} | {p['count']} | ${p['total']:,.2f} | ${p['total'] * 12:,.2f} |"
        )

    if redact_partial:
        parts.extend(["", "*Filtered to fully-attributed lines.*"])
    return "\n".join(parts)


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()) -> None:

    if "get_cost_summary" not in skip:

        @mcp.tool()
        async def get_cost_summary(
            redact_partial: bool = False,
            instance: str = "",
        ) -> str:
            """Get a summary of server costs from {$COST_MONTH} macros.

            Args:
                redact_partial: When True, drop product/provider rows
                    where some servers in the group have no cost data.
                    Recomputes the grand total from kept rows and
                    suppresses the "Servers without cost" datapoint.
                    Intended for externally-shared artifacts (board
                    decks, partner readouts) where partial-coverage
                    metadata reveals process maturity rather than the
                    metric the audience cares about. Default: False.
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                from zbbx_mcp.classify import classify_host as _classify_host
                from zbbx_mcp.classify import detect_provider

                client = resolver.resolve(instance)

                hosts, macros = await asyncio.gather(
                    client.call("host.get", {
                        "output": ["hostid", "host"],
                        "selectGroups": ["name"],
                        "selectInterfaces": ["ip"],
                        "filter": {"status": "0"},
                    }),
                    client.call("usermacro.get", {
                        "output": ["hostid", "value"],
                        "filter": {"macro": "{$COST_MONTH}"},
                    }),
                )

                host_map = {h["hostid"]: h for h in hosts}
                cost_map = {}
                for m in macros:
                    try:
                        cost_map[m["hostid"]] = float(m["value"])
                    except (ValueError, TypeError):
                        pass

                # Aggregate priced servers by product and provider
                prod_costs: dict[str, dict] = {}
                prov_costs: dict[str, dict] = {}
                for hid, cost in cost_map.items():
                    h = host_map.get(hid)
                    if not h:
                        continue
                    prod, tier = _classify_host(h.get("groups", []))
                    ip = host_ip(h)
                    provider = detect_provider(ip) if ip else "No IP"

                    key = f"{prod} / {tier}"
                    p = prod_costs.setdefault(key, {"count": 0, "total": 0.0})
                    p["count"] += 1
                    p["total"] += cost

                    pv = prov_costs.setdefault(provider, {"count": 0, "total": 0.0})
                    pv["count"] += 1
                    pv["total"] += cost

                # Total server counts per group (priced + unpriced).
                # Only consumed when redact_partial fires, but cheap to
                # always compute and keeps render-side logic uniform.
                prod_totals: dict[str, int] = {}
                prov_totals: dict[str, int] = {}
                for h in hosts:
                    prod, tier = _classify_host(h.get("groups", []))
                    ip = host_ip(h)
                    provider = detect_provider(ip) if ip else "No IP"
                    pkey = f"{prod} / {tier}"
                    prod_totals[pkey] = prod_totals.get(pkey, 0) + 1
                    prov_totals[provider] = prov_totals.get(provider, 0) + 1

                return _render_cost_summary(
                    prod_costs=prod_costs,
                    prov_costs=prov_costs,
                    prod_totals=prod_totals,
                    prov_totals=prov_totals,
                    costed=len(cost_map),
                    total_hosts=len(hosts),
                    redact_partial=redact_partial,
                )
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"


    if "get_cost_gaps" not in skip:

        @mcp.tool()
        async def get_cost_gaps(
            max_results: int = 30,
            instance: str = "",
        ) -> str:
            """Servers without cost data, grouped by provider and product.

            Args:
                max_results: Max rows (default: 30)
                instance: Zabbix instance name (optional)
            """
            try:
                from zbbx_mcp.classify import classify_host as _classify_host
                from zbbx_mcp.classify import detect_provider

                client = resolver.resolve(instance)
                hosts, macros = await asyncio.gather(
                    client.call("host.get", {
                        "output": ["hostid", "host"],
                        "selectGroups": ["name"],
                        "selectInterfaces": ["ip"],
                        "filter": {"status": "0"},
                    }),
                    client.call("usermacro.get", {
                        "output": ["hostid"],
                        "filter": {"macro": "{$COST_MONTH}"},
                    }),
                )

                has_cost = {m["hostid"] for m in macros}
                total = len(hosts)
                costed = sum(1 for h in hosts if h["hostid"] in has_cost)

                gaps: dict[str, dict] = {}
                no_ip = 0
                for h in hosts:
                    if h["hostid"] in has_cost:
                        continue
                    ip = host_ip(h)
                    if not ip:
                        no_ip += 1
                        continue
                    prod, tier = _classify_host(h.get("groups", []))
                    prov = detect_provider(ip)
                    key = f"{prov} / {prod}"
                    entry = gaps.setdefault(key, {"count": 0, "hosts": []})
                    entry["count"] += 1
                    if len(entry["hosts"]) < 3:
                        entry["hosts"].append(h["host"])

                lines = [
                    f"**Cost gaps: {total - costed} without cost** ({costed}/{total} covered)\n",
                    "| Provider / Product | Missing | Sample Hosts |",
                    "|-------------------|---------|-------------|",
                ]
                for key in sorted(gaps, key=lambda k: -gaps[k]["count"])[:max_results]:
                    g = gaps[key]
                    lines.append(f"| {key} | {g['count']} | {', '.join(g['hosts'])} |")

                if no_ip:
                    lines.append(f"\n*{no_ip} hosts without IP (monitoring) — no cost expected*")
                return "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"


    if "get_cost_efficiency" not in skip:

        @mcp.tool()
        async def get_cost_efficiency(
            max_results: int = 20,
            instance: str = "",
        ) -> str:
            """Cost per Gbps by country and provider — find overpay and waste.

            Args:
                max_results: Max rows (default: 20)
                instance: Zabbix instance name (optional)
            """
            try:
                from zbbx_mcp.classify import detect_provider
                from zbbx_mcp.data import extract_country, fetch_traffic_map

                client = resolver.resolve(instance)
                hosts, macros = await asyncio.gather(
                    client.call("host.get", {
                        "output": ["hostid", "host"],
                        "selectGroups": ["name"],
                        "selectInterfaces": ["ip"],
                        "filter": {"status": "0"},
                    }),
                    client.call("usermacro.get", {
                        "output": ["hostid", "value"],
                        "filter": {"macro": "{$COST_MONTH}"},
                    }),
                )

                cost_map = {}
                for m in macros:
                    try:
                        cost_map[m["hostid"]] = float(m["value"])
                    except (ValueError, TypeError):
                        pass

                traffic_map = await fetch_traffic_map(client, list(cost_map.keys()))

                by_country: dict[str, dict] = {}
                by_provider: dict[str, dict] = {}
                waste = []

                for hid, cost in cost_map.items():
                    h = next((x for x in hosts if x["hostid"] == hid), None)
                    if not h:
                        continue
                    traffic_mbps = traffic_map.get(hid, 0)
                    traffic_gbps = traffic_mbps / 1000
                    cc = extract_country(h["host"])
                    ip = host_ip(h)
                    prov = detect_provider(ip) if ip else "?"

                    if cc:
                        c = by_country.setdefault(cc, {"servers": 0, "cost": 0, "traffic": 0})
                        c["servers"] += 1
                        c["cost"] += cost
                        c["traffic"] += traffic_gbps

                    p = by_provider.setdefault(prov, {"servers": 0, "cost": 0, "traffic": 0})
                    p["servers"] += 1
                    p["cost"] += cost
                    p["traffic"] += traffic_gbps

                    if traffic_mbps < 1 and cost > 50:
                        waste.append((h["host"], cc, prov, cost))

                lines = [f"**Cost Efficiency** ({len(cost_map)} servers)\n"]
                lines.append("**By Country (most expensive per Gbps first):**")
                lines.append("| Country | Servers | $/mo | Gbps | $/Gbps |")
                lines.append("|---------|---------|------|------|--------|")
                sorted_cc = sorted(by_country.items(), key=lambda x: -(x[1]["cost"] / max(x[1]["traffic"], 0.001)))
                for cc, c in sorted_cc[:max_results]:
                    if c["traffic"] < 0.01:
                        lines.append(f"| {cc} | {c['servers']} | ${c['cost']:,.0f} | idle | N/A |")
                    else:
                        per = c["cost"] / c["traffic"]
                        lines.append(f"| {cc} | {c['servers']} | ${c['cost']:,.0f} | {c['traffic']:.1f} | ${per:,.0f} |")

                lines.append("\n**By Provider:**")
                lines.append("| Provider | Servers | $/mo | Gbps | $/Gbps |")
                lines.append("|----------|---------|------|------|--------|")
                for prov in sorted(by_provider, key=lambda p: -by_provider[p]["cost"])[:15]:
                    p = by_provider[prov]
                    if p["traffic"] < 0.01:
                        lines.append(f"| {prov} | {p['servers']} | ${p['cost']:,.0f} | idle | N/A |")
                    else:
                        per = p["cost"] / p["traffic"]
                        lines.append(f"| {prov} | {p['servers']} | ${p['cost']:,.0f} | {p['traffic']:.1f} | ${per:,.0f} |")

                if waste:
                    waste.sort(key=lambda w: -w[3])
                    total_waste = sum(w[3] for w in waste)
                    lines.append(f"\n**Waste: {len(waste)} servers paying ${total_waste:,.0f}/mo with 0 traffic:**")
                    lines.append("| Server | Country | Provider | $/mo |")
                    lines.append("|--------|---------|----------|------|")
                    for host, cc, prov, cost in waste[:10]:
                        lines.append(f"| {host} | {cc or '?'} | {prov} | ${cost:.0f} |")
                    if len(waste) > 10:
                        lines.append(f"*+{len(waste) - 10} more*")

                return "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"
