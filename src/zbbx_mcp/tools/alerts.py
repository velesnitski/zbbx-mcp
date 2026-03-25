"""Alert notification history."""

import time as _time

import httpx

from zbbx_mcp.resolver import InstanceResolver
from zbbx_mcp.formatters import _ts

ALERT_STATUS = {"0": "Not sent", "1": "Sent", "2": "Failed", "3": "New"}
ALERT_TYPES = {"0": "Message", "1": "Remote command"}


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()) -> None:

    if "get_alerts" not in skip:

        @mcp.tool()
        async def get_alerts(
            host_id: str = "",
            time_from: str = "",
            time_till: str = "",
            status: str = "",
            max_results: int = 50,
            instance: str = "",
        ) -> str:
            """Get alert notification history (messages sent by Zabbix).

            Args:
                host_id: Filter by host ID (optional)
                time_from: Start time as Unix timestamp (optional)
                time_till: End time as Unix timestamp (optional)
                status: Filter by status: 'sent', 'failed', or '' for all (optional)
                max_results: Maximum number of results (default: 50)
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)
                params = {
                    "output": ["alertid", "alerttype", "clock", "error",
                               "message", "retries", "sendto", "status", "subject"],
                    "sortfield": "clock",
                    "sortorder": "DESC",
                    "limit": max_results,
                }
                if host_id:
                    params["hostids"] = [host_id]
                if time_from:
                    params["time_from"] = int(time_from)
                if time_till:
                    params["time_till"] = int(time_till)
                if status == "sent":
                    params["filter"] = {"status": "1"}
                elif status == "failed":
                    params["filter"] = {"status": "2"}

                data = await client.call("alert.get", params)

                if not data:
                    return "No alerts found."

                lines = []
                for a in data:
                    atype = ALERT_TYPES.get(a.get("alerttype", "0"), "?")
                    astatus = ALERT_STATUS.get(a.get("status", "0"), "?")
                    clock = _ts(a.get("clock", "0"))
                    subject = a.get("subject", "")[:80]
                    sendto = a.get("sendto", "")[:40]
                    error = ""
                    if a.get("error"):
                        error = f"\n  Error: {a['error'][:100]}"
                    lines.append(
                        f"- **[{astatus}]** {subject}\n"
                        f"  {atype} → {sendto} | {clock} "
                        f"(alertid: {a.get('alertid', '?')}){error}"
                    )

                header = f"**Found: {len(data)} alerts**"
                if len(data) >= max_results:
                    header += f" (showing last {max_results})"
                return f"{header}\n\n" + "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error querying Zabbix: {e}"

    if "get_alert_summary" not in skip:

        @mcp.tool()
        async def get_alert_summary(
            hours: int = 24,
            instance: str = "",
        ) -> str:
            """Get alert summary for the last N hours — counts by status and top subjects.

            Args:
                hours: Number of hours to look back (default: 24)
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)
                time_from = int(_time.time()) - (hours * 3600)

                data = await client.call("alert.get", {
                    "output": ["alertid", "status", "subject", "alerttype"],
                    "time_from": time_from,
                    "sortfield": "clock",
                    "sortorder": "DESC",
                    "limit": 1000,
                })

                if not data:
                    return f"No alerts in the last {hours} hours."

                # Count by status
                status_counts: dict[str, int] = {}
                subject_counts: dict[str, int] = {}
                for a in data:
                    s = ALERT_STATUS.get(a.get("status", "0"), "?")
                    status_counts[s] = status_counts.get(s, 0) + 1
                    subj = a.get("subject", "Unknown")[:60]
                    subject_counts[subj] = subject_counts.get(subj, 0) + 1

                parts = [
                    f"**Alert Summary (last {hours}h): {len(data)} alerts**\n",
                    "## By Status\n",
                    "| Status | Count |",
                    "|--------|-------|",
                ]
                for s, c in sorted(status_counts.items(), key=lambda x: -x[1]):
                    parts.append(f"| {s} | {c} |")

                parts.extend([
                    "\n## Top Subjects\n",
                    "| Subject | Count |",
                    "|---------|-------|",
                ])
                for subj, c in sorted(subject_counts.items(), key=lambda x: -x[1])[:15]:
                    parts.append(f"| {subj} | {c} |")

                return "\n".join(parts)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error querying Zabbix: {e}"
