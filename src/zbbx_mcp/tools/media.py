"""Zabbix media types and actions (notification channels and alert rules)."""

import httpx

from zbbx_mcp.resolver import InstanceResolver

MEDIA_TYPES = {
    "0": "Email", "1": "Script", "2": "SMS", "4": "Webhook",
}

ACTION_EVENTSOURCE = {
    "0": "Triggers", "1": "Discovery", "2": "Autoregistration",
    "3": "Internal", "4": "Service",
}


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()):

    if "get_media_types" not in skip:

        @mcp.tool()
        async def get_media_types(instance: str = "") -> str:
            """Get Zabbix media types (notification channels: email, SMS, webhooks, etc.).

            Args:
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)
                data = await client.call("mediatype.get", {
                    "output": ["mediatypeid", "name", "type", "status",
                               "description", "maxsessions", "maxattempts"],
                    "selectUsers": ["userid", "username"],
                    "sortfield": "name",
                })

                if not data:
                    return "No media types found."

                lines = []
                for m in data:
                    mtype = MEDIA_TYPES.get(m.get("type", ""), f"Type {m.get('type', '?')}")
                    status = "Enabled" if m.get("status") == "0" else "Disabled"
                    user_count = len(m.get("users", []))
                    desc = ""
                    if m.get("description"):
                        desc = f"\n  {m['description'][:100]}"
                    lines.append(
                        f"- **{m.get('name', '?')}** ({mtype}) [{status}]\n"
                        f"  Users: {user_count} | "
                        f"Max attempts: {m.get('maxattempts', '?')} | "
                        f"mediatypeid: {m.get('mediatypeid', '?')}{desc}"
                    )

                return f"**Found: {len(data)} media types**\n\n" + "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error querying Zabbix: {e}"

    if "get_actions" not in skip:

        @mcp.tool()
        async def get_actions(
            eventsource: int = -1,
            instance: str = "",
        ) -> str:
            """Get Zabbix actions (alert rules that define when and how to notify).

            Args:
                eventsource: Filter by source: 0=triggers, 1=discovery, 2=autoregistration, 3=internal, 4=service (-1 for all)
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)
                params = {
                    "output": ["actionid", "name", "status", "eventsource",
                               "esc_period", "pause_suppressed"],
                    "selectOperations": ["operationtype", "esc_period", "esc_step_from", "esc_step_to"],
                    "selectFilter": "extend",
                    "sortfield": "name",
                }
                if eventsource >= 0:
                    params["filter"] = {"eventsource": eventsource}

                data = await client.call("action.get", params)

                if not data:
                    return "No actions found."

                op_types = {
                    "0": "Send message", "1": "Remote command",
                    "2": "Add host", "3": "Remove host",
                    "4": "Add to group", "5": "Remove from group",
                    "6": "Link template", "7": "Unlink template",
                    "8": "Enable host", "9": "Disable host",
                    "10": "Set inventory mode",
                }

                lines = []
                for a in data:
                    source = ACTION_EVENTSOURCE.get(a.get("eventsource", "0"), "?")
                    status = "Enabled" if a.get("status") == "0" else "Disabled"
                    ops = a.get("operations", [])
                    op_summary = []
                    for op in ops:
                        otype = op_types.get(op.get("operationtype", ""), "?")
                        op_summary.append(otype)

                    lines.append(
                        f"- **{a.get('name', '?')}** [{status}] (source: {source})\n"
                        f"  Operations: {', '.join(op_summary) or 'none'} | "
                        f"actionid: {a.get('actionid', '?')}"
                    )

                return f"**Found: {len(data)} actions**\n\n" + "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error querying Zabbix: {e}"
