"""Alert notification history."""

import time as _time

import httpx

from zbbx_mcp.formatters import _ts
from zbbx_mcp.resolver import InstanceResolver

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
            compare: bool = True,
            instance: str = "",
        ) -> str:
            """Alert summary with period-over-period trend.

            Args:
                hours: Lookback period (default: 24)
                compare: Show comparison with previous period (default: True)
                instance: Zabbix instance name (optional)
            """
            try:
                client = resolver.resolve(instance)
                now = int(_time.time())
                time_from = now - (hours * 3600)

                params = {
                    "output": ["alertid", "status", "subject", "alerttype"],
                    "sortfield": "clock",
                    "sortorder": "DESC",
                    "limit": 2000,
                }

                if compare:
                    params["time_from"] = now - (hours * 2 * 3600)
                else:
                    params["time_from"] = time_from

                data = await client.call("alert.get", params)

                current = [a for a in data if int(a.get("clock", now)) >= time_from]
                previous = [a for a in data if int(a.get("clock", now)) < time_from] if compare else []

                if not current:
                    return f"No alerts in the last {hours} hours."

                status_counts: dict[str, int] = {}
                prev_status_counts: dict[str, int] = {}
                subject_counts: dict[str, int] = {}
                for a in current:
                    s = ALERT_STATUS.get(a.get("status", "0"), "?")
                    status_counts[s] = status_counts.get(s, 0) + 1
                    subj = a.get("subject", "Unknown")[:60]
                    subject_counts[subj] = subject_counts.get(subj, 0) + 1
                for a in previous:
                    s = ALERT_STATUS.get(a.get("status", "0"), "?")
                    prev_status_counts[s] = prev_status_counts.get(s, 0) + 1

                trend = ""
                if compare and previous:
                    delta = len(current) - len(previous)
                    arrow = "+" if delta > 0 else ""
                    trend = f" ({arrow}{delta} vs prev {hours}h)"

                parts = [f"**Alerts (last {hours}h): {len(current)}{trend}**\n"]
                if compare and previous:
                    parts.extend([
                        "| Status | Current | Previous | Δ |",
                        "|--------|--------:|---------:|--:|",
                    ])
                    for s in sorted(set(status_counts) | set(prev_status_counts),
                                    key=lambda k: -status_counts.get(k, 0)):
                        cur = status_counts.get(s, 0)
                        prv = prev_status_counts.get(s, 0)
                        d = cur - prv
                        arrow = "+" if d > 0 else ""
                        parts.append(f"| {s} | {cur} | {prv} | {arrow}{d} |")
                else:
                    parts.extend(["| Status | Count |", "|--------|-------|"])
                    for s, c in sorted(status_counts.items(), key=lambda x: -x[1]):
                        parts.append(f"| {s} | {c} |")

                parts.extend([
                    "\n**Top subjects:**",
                    "| Subject | Count |",
                    "|---------|-------|",
                ])
                for subj, c in sorted(subject_counts.items(), key=lambda x: -x[1])[:10]:
                    parts.append(f"| {subj} | {c} |")

                return "\n".join(parts)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error querying Zabbix: {e}"
