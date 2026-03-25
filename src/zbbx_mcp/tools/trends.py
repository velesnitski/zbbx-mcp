"""Batch trend tools: historical metrics and per-server dashboards."""

import httpx

from zbbx_mcp.resolver import InstanceResolver
from zbbx_mcp.data import fetch_trends_batch, extract_country
from zbbx_mcp.classify import classify_host as _classify_host, detect_provider


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()):

    if "get_trends_batch" not in skip:

        @mcp.tool()
        async def get_trends_batch(
            country: str = "",
            product: str = "",
            group: str = "",
            metrics: str = "cpu,traffic,load",
            period: str = "7d",
            max_results: int = 50,
            instance: str = "",
        ) -> str:
            """Get trend data (avg/peak/min) for multiple servers and metrics.

            Shows historical averages instead of point-in-time snapshots.
            Uses batch API calls (3 total) regardless of server count.

            Args:
                country: Filter by country code in hostname (optional)
                product: Filter by product name (optional)
                group: Filter by Zabbix host group (optional)
                metrics: Comma-separated metrics: cpu, traffic, load, memory (default: cpu,traffic,load)
                period: Time period: 1d, 7d, 30d (default: 7d)
                max_results: Maximum servers (default: 50)
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)

                # Get and filter hosts
                params = {
                    "output": ["hostid", "host"],
                    "selectGroups": ["name"],
                    "filter": {"status": "0"},
                    "sortfield": "host",
                }
                if group:
                    groups = await client.call("hostgroup.get", {
                        "output": ["groupid"], "filter": {"name": [group]},
                    })
                    if not groups:
                        return f"Host group '{group}' not found."
                    params["groupids"] = [g["groupid"] for g in groups]

                hosts = await client.call("host.get", params)

                filtered_ids = []
                for h in hosts:
                    prod, _ = _classify_host(h.get("groups", []))
                    if product and product.lower() not in (prod or "").lower():
                        continue
                    if country and country.lower() not in h.get("host", "").lower():
                        continue
                    filtered_ids.append(h["hostid"])
                    if len(filtered_ids) >= max_results:
                        break

                if not filtered_ids:
                    return "No servers match the filters."

                metric_list = [m.strip() for m in metrics.split(",") if m.strip()]
                trend_rows, host_map = await fetch_trends_batch(
                    client, filtered_ids, metric_list, period,
                )

                if not trend_rows:
                    return f"No trend data for the last {period}."

                # Format units by metric
                units = {"cpu": "%", "traffic": "Mbps", "load": "", "memory": "GB"}

                parts = [
                    f"**Trends ({period}) for {len(set(r.hostid for r in trend_rows))} servers**\n",
                    "| Server | Metric | Avg | Peak | Min | Current | Trend |",
                    "|--------|--------|-----|------|-----|---------|-------|",
                ]
                for r in trend_rows:
                    u = units.get(r.metric, "")
                    parts.append(
                        f"| {r.hostname} | {r.metric} | "
                        f"{r.avg} {u} | {r.peak} {u} | {r.min_val} {u} | "
                        f"{r.current} {u} | {r.trend_dir} |"
                    )

                return "\n".join(parts)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

    if "get_server_dashboard" not in skip:

        @mcp.tool()
        async def get_server_dashboard(
            host: str,
            period: str = "7d",
            instance: str = "",
        ) -> str:
            """Get a per-server dashboard showing daily metric trends.

            Shows daily avg/peak for CPU, traffic, and load over the period.

            Args:
                host: Hostname or host ID
                period: Time period: 1d, 7d, 30d (default: 7d)
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)

                # Resolve hostname
                if not host.isdigit():
                    lookup = await client.call("host.get", {
                        "output": ["hostid", "host"],
                        "filter": {"host": [host]},
                    })
                    if not lookup:
                        lookup = await client.call("host.get", {
                            "output": ["hostid", "host"],
                            "search": {"host": host, "name": host},
                            "searchByAny": True, "searchWildcardsEnabled": True,
                            "limit": 1,
                        })
                    if not lookup:
                        return f"Host '{host}' not found."
                    hostid = lookup[0]["hostid"]
                    hostname = lookup[0]["host"]
                else:
                    hostid = host
                    h = await client.call("host.get", {
                        "hostids": [hostid], "output": ["host"],
                    })
                    hostname = h[0]["host"] if h else hostid

                trend_rows, _ = await fetch_trends_batch(
                    client, [hostid], ["cpu", "traffic", "load", "memory"], period,
                )

                if not trend_rows:
                    return f"No trend data for '{hostname}' in the last {period}."

                parts = [
                    f"# Dashboard: {hostname} (last {period})\n",
                ]

                units = {"cpu": "%", "traffic": "Mbps", "load": "", "memory": "GB"}
                for r in trend_rows:
                    u = units.get(r.metric, "")
                    parts.append(
                        f"**{r.metric.title()}:** "
                        f"avg {r.avg} {u} | peak {r.peak} {u} | "
                        f"min {r.min_val} {u} | current {r.current} {u} | "
                        f"trend: {r.trend_dir}"
                    )

                return "\n".join(parts)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"
