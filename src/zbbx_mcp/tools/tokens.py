"""API token hygiene — the surface that did not exist (ADR 112).

A Zabbix token audit had to bypass this server entirely and call ``token.get``
by hand, because no tool covered API tokens. What that audit found is exactly
what a routine tool would have surfaced without anyone going looking: expired
tokens still present, tokens with no expiry at all, one unused for over a year,
and a token named ``test`` on a personal account.

Tokens are the one object here whose *absence of use* is the finding. A token
nobody has touched in a year is not idle capacity, it is an unrevoked key — so
this tool ranks by risk rather than by name, and says plainly what makes each
row risky.

``token.get`` is Super-admin-only unless a role grants it. When the call fails
the tool says so rather than rendering an empty table, because "no tokens" and
"you may not list tokens" are the same picture otherwise (ADR 103).

The pure core (``classify_token``, ``token_risk``) is unit tested; the async
tool wires the API into it.
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass, field

import httpx

from zbbx_mcp.resolver import InstanceResolver

DEFAULT_STALE_DAYS = 90

# Flag vocabulary, worst first — the order they are ranked in.
EXPIRED = "expired"              # past expiry and still enabled
NEVER_EXPIRES = "never expires"  # no expiry set at all
NEVER_USED = "never used"        # issued and never once authenticated
STALE = "stale"                  # not used within the staleness window
DISABLED = "disabled"            # switched off — present but inert


@dataclass
class TokenInfo:
    name: str
    owner: str
    flags: list[str] = field(default_factory=list)
    idle_days: int | None = None
    expires_in_days: int | None = None


def classify_token(
    row: dict, now: int, *, stale_days: int = DEFAULT_STALE_DAYS
) -> TokenInfo:
    """Flags for one ``token.get`` row. Pure.

    ``lastaccess`` and ``expires_at`` are 0 for "never" in Zabbix, which is the
    trap: 0 sorts as the oldest possible timestamp, so a naive age calculation
    makes a never-used token look like the *most* recently used one, or an
    unexpiring token look long expired. Both are treated as their own state.
    """
    name = str(row.get("name", "") or "?")
    owner = str(row.get("_owner", "") or row.get("userid", "") or "?")
    info = TokenInfo(name=name, owner=owner)

    def _int(key: str) -> int:
        try:
            return int(row.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0

    if str(row.get("status", "0")) == "1":
        info.flags.append(DISABLED)

    expires = _int("expires_at")
    if expires == 0:
        info.flags.append(NEVER_EXPIRES)
    else:
        info.expires_in_days = (expires - now) // 86400
        if expires < now:
            info.flags.append(EXPIRED)

    last = _int("lastaccess")
    if last == 0:
        info.flags.append(NEVER_USED)
    else:
        info.idle_days = max(0, (now - last) // 86400)
        if info.idle_days >= stale_days:
            info.flags.append(STALE)
    return info


def token_risk(info: TokenInfo) -> int:
    """Sort key — lower is worse. Pure.

    A disabled token cannot be used, so whatever else is true of it, it ranks
    below every live one; ranking it by its other flags would push real keys
    off the top of the list.
    """
    if DISABLED in info.flags:
        return 90
    if EXPIRED in info.flags:
        return 0          # still present, past its own deadline
    if NEVER_EXPIRES in info.flags and NEVER_USED in info.flags:
        return 1          # a permanent key nobody has ever needed
    if NEVER_EXPIRES in info.flags and STALE in info.flags:
        return 2
    if NEVER_USED in info.flags:
        return 3
    if NEVER_EXPIRES in info.flags:
        return 4
    if STALE in info.flags:
        return 5
    return 50


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()) -> None:

    if "get_api_tokens" not in skip:

        @mcp.tool()
        async def get_api_tokens(
            stale_days: int = DEFAULT_STALE_DAYS,
            only_risky: bool = True,
            max_results: int = 50,
            instance: str = "",
        ) -> str:
            """Audit Zabbix API tokens — expiry, last use, and owner.

            Tokens are the one object whose *absence of use* is the finding: a
            token nobody has touched in a year is not idle capacity, it is an
            unrevoked key. Rows are ranked by risk — expired-but-present first,
            then permanent keys never used, then stale ones.

            Needs `token.get`, which is Super admin only unless a role grants
            it. A denied call is reported as denied, never as an empty list.

            Args:
                stale_days: Days without use before a token counts as stale
                    (default: 90)
                only_risky: Hide tokens with no flags at all (default: True)
                max_results: Max rows shown (default: 50)
                instance: Zabbix instance (optional)
            """
            try:
                client = resolver.resolve(instance)
                try:
                    rows = await client.call("token.get", {
                        "output": ["tokenid", "name", "userid", "lastaccess",
                                   "status", "expires_at"],
                    })
                except (httpx.HTTPError, ValueError, KeyError) as e:
                    return (
                        f"Cannot list API tokens — `token.get` failed "
                        f"({type(e).__name__}). It is Super-admin-only unless "
                        "the role grants it, so this is a permissions answer, "
                        "NOT 'there are no tokens'."
                    )
                if not rows:
                    return "No API tokens exist on this instance."

                owners: dict[str, str] = {}
                uids = sorted({str(r.get("userid", "")) for r in rows if r.get("userid")})
                if uids:
                    try:
                        users = await client.call("user.get", {
                            "userids": uids, "output": ["userid", "username"],
                        })
                        owners = {str(u["userid"]): u.get("username", "")
                                  for u in users or []}
                    except (httpx.HTTPError, ValueError, KeyError):
                        owners = {}   # names are a nicety; ids still identify

                now = int(_time.time())
                infos = [
                    classify_token(
                        {**r, "_owner": owners.get(str(r.get("userid", "")), "")},
                        now, stale_days=stale_days)
                    for r in rows
                ]
                total = len(infos)
                shown = [i for i in infos if i.flags] if only_risky else list(infos)
                shown.sort(key=lambda i: (token_risk(i), i.name.lower()))

                counts: dict[str, int] = {}
                for i in infos:
                    for f in i.flags:
                        counts[f] = counts.get(f, 0) + 1
                summary = ", ".join(
                    f"{counts[f]} {f}" for f in
                    (EXPIRED, NEVER_EXPIRES, NEVER_USED, STALE, DISABLED)
                    if counts.get(f)
                ) or "no flags raised"
                header = f"**API tokens** ({total} total; {summary})\n"
                if not shown:
                    return header + "\nEvery token has an expiry and recent use."

                parts = [
                    header,
                    "| Token | Owner | Last used | Expires | Flags |",
                    "|-------|-------|-----------|---------|-------|",
                ]
                for i in shown[:max_results]:
                    used = ("never" if NEVER_USED in i.flags
                            else f"{i.idle_days}d ago")
                    if NEVER_EXPIRES in i.flags:
                        exp = "**never**"
                    elif i.expires_in_days is not None and i.expires_in_days < 0:
                        exp = f"{abs(i.expires_in_days)}d ago"
                    else:
                        exp = f"in {i.expires_in_days}d"
                    parts.append(
                        f"| {i.name} | {i.owner} | {used} | {exp} | "
                        f"{', '.join(i.flags)} |"
                    )
                if len(shown) > max_results:
                    parts.append(f"\n*{len(shown) - max_results} more omitted*")
                parts.append(
                    f"\n_Stale = unused for {stale_days}d or more. A token that "
                    "never expires and has never been used is a permanent key "
                    "nobody needed — those rank first._"
                )
                return "\n".join(parts)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"
