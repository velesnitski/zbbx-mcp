"""Web scenario (HTTP check) monitoring — URL status, response times."""

from __future__ import annotations

import httpx

from zbbx_mcp.resolver import InstanceResolver


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()) -> None:

    if "get_web_scenarios" not in skip:

        @mcp.tool()
        async def get_web_scenarios(
            host_id: str = "",
            search: str = "",
            max_results: int = 30,
            instance: str = "",
        ) -> str:
            """List web scenarios (HTTP checks) with their status and response times.

            Args:
                host_id: Filter by host ID (optional)
                search: Search by scenario name or URL (optional)
                max_results: Maximum results (default: 30)
                instance: Zabbix instance name (optional)
            """
            try:
                client = resolver.resolve(instance)
                params = {
                    "output": ["httptestid", "name", "delay", "status", "nextcheck"],
                    "selectHosts": ["hostid", "host"],
                    "selectSteps": ["name", "url", "status_codes", "no"],
                    "sortfield": "name",
                    "limit": max_results,
                }
                if host_id:
                    params["hostids"] = [host_id]
                if search:
                    q = search if "*" in search else f"*{search}*"
                    params["search"] = {"name": q}
                    params["searchWildcardsEnabled"] = True

                data = await client.call("httptest.get", params)

                if not data:
                    return "No web scenarios found."

                lines = []
                for ws in data:
                    status = "Enabled" if ws.get("status") == "0" else "Disabled"
                    host = ws["hosts"][0]["host"] if ws.get("hosts") else "?"
                    steps = ws.get("steps", [])
                    urls = [s.get("url", "") for s in steps[:3]]
                    url_str = ", ".join(urls)[:80]
                    lines.append(
                        f"- **{ws.get('name', '?')}** [{status}] — {host}\n"
                        f"  ID: {ws.get('httptestid', '?')} | "
                        f"Interval: {ws.get('delay', '?')} | "
                        f"Steps: {len(steps)} | URLs: {url_str}"
                    )

                header = f"**{len(data)} web scenarios**"
                if len(data) >= max_results:
                    header += f" (showing {max_results})"
                return f"{header}\n\n" + "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

    if "get_web_scenario_status" not in skip:

        @mcp.tool()
        async def get_web_scenario_status(
            search: str = "",
            only_failed: bool = False,
            max_results: int = 30,
            instance: str = "",
        ) -> str:
            """Check web scenario health — response times, failures, status codes.

            Args:
                search: Search by scenario name or URL (optional)
                only_failed: Show only failed scenarios (default: False)
                max_results: Maximum results (default: 30)
                instance: Zabbix instance name (optional)
            """
            try:
                client = resolver.resolve(instance)

                # Get all web scenarios
                params = {
                    "output": ["httptestid", "name", "status"],
                    "selectHosts": ["hostid", "host"],
                    "selectSteps": ["httpstepid", "name", "url", "status_codes"],
                    "filter": {"status": "0"},  # enabled only
                    "sortfield": "name",
                }
                if search:
                    q = search if "*" in search else f"*{search}*"
                    params["search"] = {"name": q}
                    params["searchWildcardsEnabled"] = True

                scenarios = await client.call("httptest.get", params)
                if not scenarios:
                    return "No enabled web scenarios found."

                # Get test items for response time and status
                test_ids = [s["httptestid"] for s in scenarios]
                items = await client.call("item.get", {
                    "hostids": [s["hosts"][0]["hostid"] for s in scenarios if s.get("hosts")],
                    "output": ["itemid", "hostid", "key_", "lastvalue", "lastclock"],
                    "search": {"key_": "web.test"},
                    "searchWildcardsEnabled": True,
                    "limit": len(test_ids) * 5,
                })

                # Map items by hostid
                host_items: dict[str, dict[str, str]] = {}
                for it in items:
                    hid = it["hostid"]
                    key = it.get("key_", "")
                    host_items.setdefault(hid, {})[key] = it.get("lastvalue", "")

                lines = []
                failed_count = 0
                for ws in scenarios:
                    host = ws["hosts"][0] if ws.get("hosts") else {}
                    hostname = host.get("host", "?")
                    hid = host.get("hostid", "")
                    name = ws.get("name", "?")
                    steps = ws.get("steps", [])

                    # Find response time and fail items
                    h_items = host_items.get(hid, {})
                    resp_time = ""
                    fail_status = ""
                    for k, v in h_items.items():
                        if f"web.test.time[{name}" in k:
                            try:
                                resp_time = f"{float(v):.2f}s"
                            except (ValueError, TypeError):
                                pass
                        if f"web.test.fail[{name}" in k:
                            fail_status = v

                    is_failed = fail_status not in ("", "0")
                    if only_failed and not is_failed:
                        continue
                    if is_failed:
                        failed_count += 1

                    status_badge = "FAIL" if is_failed else "OK"
                    urls = [s.get("url", "")[:60] for s in steps[:2]]
                    url_str = ", ".join(urls)

                    lines.append(
                        f"| {name[:40]} | {hostname} | {status_badge} | "
                        f"{resp_time or 'N/A'} | {url_str} |"
                    )

                shown = lines[:max_results]
                header_lines = [
                    f"**Web Scenario Health:** {len(scenarios)} scenarios, {failed_count} failed\n",
                    "| Scenario | Host | Status | Response | URLs |",
                    "|----------|------|--------|----------|------|",
                ]
                result = "\n".join(header_lines + shown)
                if len(lines) > max_results:
                    result += f"\n\n*{len(lines) - max_results} more omitted*"
                return result
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"
