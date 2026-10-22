import httpx

from zbbx_mcp.resolver import InstanceResolver
from zbbx_mcp.rollback import TEMPLATE_LINK, Action


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()) -> None:

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
            """Link a template to a host, keeping the templates it already has.

            Args:
                host_id: Host ID to link the template to
                template_id: Template ID to link
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)
                # host.massadd adds; `templates` on host.update would REPLACE
                # the host's whole template list.
                await client.call("host.massadd", {
                    "hosts": [{"hostid": host_id}],
                    "templates": [{"templateid": template_id}],
                })
                client.journal(Action.CREATE, TEMPLATE_LINK, template_id,
                               {"hostid": host_id, "templateid": template_id},
                               f"Linked template {template_id} to host {host_id}")
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
                clear: Also delete the items/triggers inherited from the template,
                    with their history (default: False keeps them on the host).
                    A clear cannot be undone.
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)
                key = "templateids_clear" if clear else "templateids"
                await client.call("host.massremove", {"hostids": [host_id], key: [template_id]})
                client.journal(Action.DELETE, TEMPLATE_LINK, template_id,
                               {"hostid": host_id, "templateid": template_id, "cleared": clear},
                               f"Unlinked template {template_id} from host {host_id}"
                               + (" (cleared)" if clear else ""))
                if clear:
                    return (
                        f"Template {template_id} unlinked and cleared from host {host_id}. "
                        "Its inherited items, triggers and their history were deleted; "
                        "this cannot be undone (a rollback relinks the template, but the "
                        "history is gone)."
                    )
                return (f"Template {template_id} unlinked from host {host_id} "
                        "(inherited items and triggers kept).")
            except (httpx.HTTPError, ValueError) as e:
                return f"Error unlinking template: {e}"
