"""Zabbix user management."""

import httpx

from zbbx_mcp.resolver import InstanceResolver

USER_TYPES = {"1": "User", "2": "Admin", "3": "Super admin"}


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()):

    if "get_users" not in skip:

        @mcp.tool()
        async def get_users(
            search: str = "",
            instance: str = "",
        ) -> str:
            """Get Zabbix users with their roles and groups.

            Args:
                search: Search pattern for username or name (optional)
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)
                params = {
                    "output": ["userid", "username", "name", "surname",
                               "autologin", "autologout", "type", "attempt_failed",
                               "attempt_clock", "rows_per_page"],
                    "selectUsrgrps": ["usrgrpid", "name"],
                    "selectMedias": ["mediatypeid", "sendto", "active"],
                    "sortfield": "username",
                }
                if search:
                    params["search"] = {"username": search, "name": search, "surname": search}
                    params["searchWildcardsEnabled"] = True
                    params["searchByAny"] = True

                data = await client.call("user.get", params)

                if not data:
                    return "No users found."

                lines = []
                for u in data:
                    utype = USER_TYPES.get(u.get("type", "1"), "?")
                    fullname = f"{u.get('name', '')} {u.get('surname', '')}".strip()
                    groups = ", ".join(g["name"] for g in u.get("usrgrps", []))
                    medias = []
                    for m in u.get("medias", []):
                        status = "active" if m.get("active") == "0" else "disabled"
                        medias.append(f"{m.get('sendto', '?')} ({status})")
                    media_str = ", ".join(medias) if medias else "none"

                    failed = ""
                    if u.get("attempt_failed", "0") != "0":
                        failed = f" [FAILED: {u['attempt_failed']} attempts]"

                    lines.append(
                        f"- **{u.get('username', '?')}** ({utype}){failed}\n"
                        f"  Name: {fullname or 'N/A'} | "
                        f"Groups: {groups or 'none'}\n"
                        f"  Media: {media_str} | "
                        f"userid: {u.get('userid', '?')}"
                    )

                return f"**Found: {len(data)} users**\n\n" + "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error querying Zabbix: {e}"
