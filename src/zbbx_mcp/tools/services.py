import httpx

from zbbx_mcp.resolver import InstanceResolver
from zbbx_mcp.formatters import _ts

SERVICE_STATUS = {
    "-1": "OK",
    "0": "Not classified", "1": "Information", "2": "Warning",
    "3": "Average", "4": "High", "5": "Disaster",
}


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()):

    if "get_services" not in skip:

        @mcp.tool()
        async def get_services(
            search: str = "",
            max_results: int = 50,
            instance: str = "",
        ) -> str:
            """Get Zabbix services (business service monitoring).

            Args:
                search: Search pattern for service name (optional)
                max_results: Maximum number of results (default: 50)
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)
                params = {
                    "output": ["serviceid", "name", "status", "algorithm",
                               "sortorder", "description", "created_at"],
                    "selectChildren": ["serviceid", "name"],
                    "selectParents": ["serviceid", "name"],
                    "selectProblemTags": "extend",
                    "sortfield": "sortorder",
                    "limit": max_results,
                }
                if search:
                    params["search"] = {"name": search}
                    params["searchWildcardsEnabled"] = True

                data = await client.call("service.get", params)

                if not data:
                    return "No services found."

                algorithms = {
                    "0": "Set status to OK",
                    "1": "Most critical of children",
                    "2": "Most critical if all have problems",
                }

                lines = []
                for s in data:
                    status = SERVICE_STATUS.get(s.get("status", "-1"), "?")
                    algo = algorithms.get(s.get("algorithm", "1"), "?")
                    children = len(s.get("children", []))
                    parents = len(s.get("parents", []))
                    desc = ""
                    if s.get("description"):
                        desc = f"\n  {s['description'][:100]}"
                    lines.append(
                        f"- **{s.get('name', '?')}** [Status: {status}]\n"
                        f"  Algorithm: {algo} | "
                        f"Children: {children} | Parents: {parents} "
                        f"(id: {s.get('serviceid', '?')}){desc}"
                    )

                return f"**Found: {len(data)} services**\n\n" + "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error querying Zabbix: {e}"

    if "get_sla" not in skip:

        @mcp.tool()
        async def get_sla(
            search: str = "",
            instance: str = "",
        ) -> str:
            """Get Zabbix SLA definitions.

            Args:
                search: Search pattern for SLA name (optional)
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)
                params = {
                    "output": ["slaid", "name", "slo", "period",
                               "effective_date", "timezone", "status", "description"],
                    "selectServiceTags": "extend",
                    "sortfield": "name",
                }
                if search:
                    params["search"] = {"name": search}
                    params["searchWildcardsEnabled"] = True

                data = await client.call("sla.get", params)

                if not data:
                    return "No SLAs found."

                periods = {"0": "Daily", "1": "Weekly", "2": "Monthly", "3": "Quarterly", "4": "Annually"}
                lines = []
                for s in data:
                    period = periods.get(s.get("period", "2"), "?")
                    slo = s.get("slo", "?")
                    status = "Enabled" if s.get("status") == "1" else "Disabled"
                    tags = s.get("service_tags", [])
                    tag_str = ", ".join(f"{t.get('tag', '')}={t.get('value', '')}" for t in tags)
                    lines.append(
                        f"- **{s.get('name', '?')}** — SLO: {slo}% ({period}) [{status}]\n"
                        f"  Tags: {tag_str or 'none'} | "
                        f"id: {s.get('slaid', '?')}"
                    )

                return f"**Found: {len(data)} SLAs**\n\n" + "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error querying Zabbix: {e}"
