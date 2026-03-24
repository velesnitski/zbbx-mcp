import httpx

from zbbx_mcp.resolver import InstanceResolver


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()):

    if "export_configuration" not in skip:

        @mcp.tool()
        async def export_configuration(
            host_ids: str = "",
            template_ids: str = "",
            group_ids: str = "",
            format: str = "yaml",
            instance: str = "",
        ) -> str:
            """Export Zabbix configuration (hosts, templates, or groups).

            Args:
                host_ids: Comma-separated host IDs to export (optional)
                template_ids: Comma-separated template IDs to export (optional)
                group_ids: Comma-separated host group IDs to export (optional)
                format: Export format: yaml, xml, or json (default: yaml)
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)
                options = {}
                if host_ids:
                    options["hosts"] = [h.strip() for h in host_ids.split(",")]
                if template_ids:
                    options["templates"] = [t.strip() for t in template_ids.split(",")]
                if group_ids:
                    options["host_groups"] = [g.strip() for g in group_ids.split(",")]

                if not options:
                    return "At least one of host_ids, template_ids, or group_ids is required."

                result = await client.call("configuration.export", {
                    "options": options,
                    "format": format,
                })

                if not result:
                    return "No configuration data returned."

                # Truncate if very large
                if isinstance(result, str) and len(result) > 10000:
                    return f"```{format}\n{result[:10000]}\n```\n\n*... truncated ({len(result)} chars total)*"
                return f"```{format}\n{result}\n```"
            except (httpx.HTTPError, ValueError) as e:
                return f"Error exporting configuration: {e}"

    if "import_configuration" not in skip:

        @mcp.tool()
        async def import_configuration(
            source: str,
            format: str = "yaml",
            create_new: bool = True,
            update_existing: bool = True,
            instance: str = "",
        ) -> str:
            """Import Zabbix configuration from YAML/XML/JSON string.

            Args:
                source: Configuration data string (YAML, XML, or JSON)
                format: Format of the source: yaml, xml, or json (default: yaml)
                create_new: Create new objects (default: True)
                update_existing: Update existing objects (default: True)
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)

                rules_val = {"createMissing": create_new, "updateExisting": update_existing}
                rules = {
                    "hosts": rules_val,
                    "templates": rules_val,
                    "host_groups": rules_val,
                    "template_groups": rules_val,
                    "items": rules_val,
                    "triggers": rules_val,
                    "graphs": rules_val,
                    "valueMaps": rules_val,
                }

                await client.call("configuration.import", {
                    "format": format,
                    "source": source,
                    "rules": rules,
                })

                return "Configuration imported successfully."
            except (httpx.HTTPError, ValueError) as e:
                return f"Error importing configuration: {e}"
