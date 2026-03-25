import httpx

from zbbx_mcp.resolver import InstanceResolver

MACRO_TYPES = {"0": "Text", "1": "Secret", "2": "Vault"}


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()) -> None:

    if "get_host_macros" not in skip:

        @mcp.tool()
        async def get_host_macros(
            host_id: str,
            instance: str = "",
        ) -> str:
            """Get user macros for a specific host.

            Args:
                host_id: Zabbix host ID
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)
                data = await client.call("usermacro.get", {
                    "hostids": [host_id],
                    "output": ["hostmacroid", "macro", "value", "type", "description"],
                    "sortfield": "macro",
                })

                if not data:
                    return "No host macros found."

                lines = []
                for m in data:
                    mtype = MACRO_TYPES.get(m.get("type", "0"), "?")
                    value = m.get("value", "")
                    if m.get("type") == "1":
                        value = "******"
                    desc = f" — {m['description']}" if m.get("description") else ""
                    lines.append(
                        f"- `{m.get('macro', '?')}` = {value} ({mtype}){desc}"
                    )

                return f"**Found: {len(data)} host macros**\n\n" + "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error querying Zabbix: {e}"

    if "get_global_macros" not in skip:

        @mcp.tool()
        async def get_global_macros(instance: str = "") -> str:
            """Get global Zabbix macros.

            Args:
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)
                data = await client.call("usermacro.get", {
                    "globalmacro": True,
                    "output": ["globalmacroid", "macro", "value", "type", "description"],
                    "sortfield": "macro",
                })

                if not data:
                    return "No global macros found."

                lines = []
                for m in data:
                    mtype = MACRO_TYPES.get(m.get("type", "0"), "?")
                    value = m.get("value", "")
                    if m.get("type") == "1":
                        value = "******"
                    desc = f" — {m['description']}" if m.get("description") else ""
                    lines.append(
                        f"- `{m.get('macro', '?')}` = {value} ({mtype}){desc}"
                    )

                return f"**Found: {len(data)} global macros**\n\n" + "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error querying Zabbix: {e}"

    if "set_host_macro" not in skip:

        @mcp.tool()
        async def set_host_macro(
            host_id: str,
            macro: str,
            value: str,
            description: str = "",
            macro_type: int = 0,
            instance: str = "",
        ) -> str:
            """Create or update a host macro. If the macro exists, it will be updated.

            Args:
                host_id: Zabbix host ID
                macro: Macro name (e.g., '{$MY_MACRO}')
                value: Macro value
                description: Macro description (optional)
                macro_type: 0=text, 1=secret, 2=vault (default: 0)
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)

                # Check if macro already exists
                existing = await client.call("usermacro.get", {
                    "hostids": [host_id],
                    "filter": {"macro": macro},
                    "output": ["hostmacroid"],
                })

                if existing:
                    mid = existing[0]["hostmacroid"]
                    await client.snapshot_and_record("update", "usermacro", mid, f"Updated macro {macro} on host {host_id}")
                    params = {
                        "hostmacroid": mid,
                        "value": value,
                        "type": macro_type,
                    }
                    if description:
                        params["description"] = description
                    await client.call("usermacro.update", params)
                    return f"Host macro `{macro}` updated on host {host_id}."
                else:
                    params = {
                        "hostid": host_id,
                        "macro": macro,
                        "value": value,
                        "type": macro_type,
                    }
                    if description:
                        params["description"] = description
                    result = await client.call("usermacro.create", params)
                    mid = result.get("hostmacroids", ["?"])[0]
                    client.record_create("usermacro", mid, f"Created macro {macro} on host {host_id}")
                    return f"Host macro `{macro}` created on host {host_id}."
            except (httpx.HTTPError, ValueError) as e:
                return f"Error setting macro: {e}"

    if "delete_host_macro" not in skip:

        @mcp.tool()
        async def delete_host_macro(
            host_id: str,
            macro: str,
            instance: str = "",
        ) -> str:
            """Delete a host macro.

            Args:
                host_id: Zabbix host ID
                macro: Macro name (e.g., '{$MY_MACRO}')
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)
                existing = await client.call("usermacro.get", {
                    "hostids": [host_id],
                    "filter": {"macro": macro},
                    "output": ["hostmacroid"],
                })

                if not existing:
                    return f"Macro `{macro}` not found on host {host_id}."

                mid = existing[0]["hostmacroid"]
                await client.snapshot_and_record("delete", "usermacro", mid, f"Deleted macro {macro} from host {host_id}")
                await client.call("usermacro.delete", [mid])
                return f"Macro `{macro}` deleted from host {host_id}."
            except (httpx.HTTPError, ValueError) as e:
                return f"Error deleting macro: {e}"
