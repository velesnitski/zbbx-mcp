import httpx

from zbbx_mcp.resolver import InstanceResolver


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()):

    if "get_templates" not in skip:

        @mcp.tool()
        async def get_templates(
            search: str = "",
            host_id: str = "",
            max_results: int = 50,
            instance: str = "",
        ) -> str:
            """Get Zabbix templates.

            Args:
                search: Search pattern for template name (optional)
                host_id: Get templates linked to this host (optional)
                max_results: Maximum number of results (default: 50)
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)
                params = {
                    "output": ["templateid", "host", "name", "description"],
                    "selectHosts": ["hostid", "host"],
                    "selectItems": "count",
                    "selectTriggers": "count",
                    "sortfield": "host",
                    "limit": max_results,
                }
                if search:
                    params["search"] = {"host": search, "name": search}
                    params["searchWildcardsEnabled"] = True
                    params["searchByAny"] = True
                if host_id:
                    params["hostids"] = [host_id]

                data = await client.call("template.get", params)

                if not data:
                    return "No templates found."

                lines = []
                for t in data:
                    host_count = len(t.get("hosts", []))
                    items = t.get("items", "?")
                    triggers = t.get("triggers", "?")
                    desc = ""
                    if t.get("description"):
                        desc = f"\n  {t['description'][:100]}"
                    lines.append(
                        f"- **{t.get('host', '?')}** — "
                        f"{items} items, {triggers} triggers, "
                        f"linked to {host_count} hosts "
                        f"(id: {t.get('templateid', '?')}){desc}"
                    )

                header = f"**Found: {len(data)} templates**"
                if len(data) >= max_results:
                    header += f" (showing first {max_results}, more may exist)"
                return f"{header}\n\n" + "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error querying Zabbix: {e}"

    if "link_template" not in skip:

        @mcp.tool()
        async def link_template(
            host_id: str,
            template_id: str,
            instance: str = "",
        ) -> str:
            """Link a template to a host.

            Args:
                host_id: Host ID to link the template to
                template_id: Template ID to link
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)
                await client.call("host.update", {
                    "hostid": host_id,
                    "templates": [{"templateid": template_id}],
                })
                return f"Template {template_id} linked to host {host_id}."
            except (httpx.HTTPError, ValueError) as e:
                return f"Error linking template: {e}"

    if "unlink_template" not in skip:

        @mcp.tool()
        async def unlink_template(
            host_id: str,
            template_id: str,
            clear: bool = False,
            instance: str = "",
        ) -> str:
            """Unlink a template from a host.

            Args:
                host_id: Host ID to unlink the template from
                template_id: Template ID to unlink
                clear: Also remove items/triggers inherited from template (default: False)
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)
                params = {"hostid": host_id}
                if clear:
                    params["templates_clear"] = [{"templateid": template_id}]
                else:
                    params["templates_clear"] = [{"templateid": template_id}]

                await client.call("host.update", params)
                action = "unlinked and cleared" if clear else "unlinked"
                return f"Template {template_id} {action} from host {host_id}."
            except (httpx.HTTPError, ValueError) as e:
                return f"Error unlinking template: {e}"
