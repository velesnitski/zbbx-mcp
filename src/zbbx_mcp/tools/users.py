"""Zabbix user management."""

import httpx

from zbbx_mcp.resolver import InstanceResolver


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()) -> None:

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
                # NB: the pre-5.2 `type` field (and `rows_per_page`) were removed
                # from the user object — user *types* became role-based (`roleid`).
                # Requesting them makes user.get reject the whole call with -32602
                # (ADR 085). Role names are resolved separately via role.get.
                params = {
                    "output": ["userid", "username", "name", "surname",
                               "autologin", "autologout", "roleid",
                               "attempt_failed", "attempt_clock"],
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

                # Resolve roleid -> role name (roles replaced the old user types).
                role_ids = sorted({u.get("roleid") for u in data if u.get("roleid")})
                role_names: dict[str, str] = {}
                if role_ids:
                    try:
                        roles = await client.call("role.get", {
                            "output": ["roleid", "name"], "roleids": role_ids,
                        })
                        role_names = {
                            r["roleid"]: (r.get("name") or r["roleid"]) for r in roles
                        }
                    except (httpx.HTTPError, ValueError):
                        role_names = {}  # best-effort; fall back to the raw roleid

                lines = []
                for u in data:
                    rid = u.get("roleid") or ""
                    utype = role_names.get(rid) or (f"role {rid}" if rid else "?")
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
