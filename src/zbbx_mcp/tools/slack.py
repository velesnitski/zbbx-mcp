"""Ad-hoc Slack messaging via webhook for sending reports and alerts."""

import asyncio
import json
import os
from datetime import datetime, timezone

import httpx

from zbbx_mcp.resolver import InstanceResolver

SLACK_WEBHOOK_ENV = "SLACK_WEBHOOK_URL"


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()) -> None:

    if "send_slack_message" not in skip:

        @mcp.tool()
        async def send_slack_message(
            text: str,
            channel: str = "",
            blocks: str = "",
        ) -> str:
            """Send a message to Slack via webhook.

            Args:
                text: Message text (markdown supported)
                channel: Override channel (optional)
                blocks: Block Kit JSON string (optional)
            """
            url = os.environ.get(SLACK_WEBHOOK_ENV, "")
            if not url:
                return f"No Slack webhook URL. Set {SLACK_WEBHOOK_ENV} environment variable."

            payload: dict = {"text": text}
            if channel:
                payload["channel"] = channel
            if blocks:
                try:
                    payload["blocks"] = json.loads(blocks)
                except json.JSONDecodeError:
                    return "Invalid blocks JSON."

            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.post(url, json=payload)
                    if resp.status_code == 200 and resp.text == "ok":
                        return "Message sent to Slack."
                    return f"Slack returned: {resp.status_code} {resp.text[:200]}"
            except httpx.HTTPError as e:
                return f"Error sending to Slack: {e}"

    if "send_slack_report" not in skip:

        @mcp.tool()
        async def send_slack_report(
            title: str = "Zabbix Server Report",
            product: str = "",
            include_problems: bool = True,
            include_high_cpu: bool = True,
            cpu_threshold: float = 80.0,
            instance: str = "",
        ) -> str:
            """Generate and send a Zabbix infrastructure summary to Slack.

            Args:
                title: Report title (default: 'Zabbix Server Report')
                product: Filter by product (optional)
                include_problems: Include problems section (default: True)
                include_high_cpu: Include high CPU section (default: True)
                cpu_threshold: CPU % threshold (default: 80)
                instance: Zabbix instance (optional)
            """
            from zbbx_mcp.classify import classify_host as _classify_host

            url = os.environ.get(SLACK_WEBHOOK_ENV, "")
            if not url:
                return f"No Slack webhook URL. Set {SLACK_WEBHOOK_ENV} environment variable."

            try:
                client = resolver.resolve(instance)

                # Parallel fetch: hosts + problems + CPU metrics
                tasks = [
                    client.call("host.get", {
                        "output": ["hostid", "host", "status"],
                        "selectGroups": ["name"],
                        "selectInterfaces": ["ip"],
                        "filter": {"status": "0"},
                    }),
                ]
                if include_problems:
                    tasks.append(client.call("problem.get", {
                        "output": ["eventid", "name", "severity"],
                        "sortfield": ["eventid"],
                        "sortorder": ["DESC"],
                        "recent": True,
                        "limit": 100,
                    }))

                results = await asyncio.gather(*tasks)
                hosts = results[0]
                problems = results[1] if include_problems and len(results) > 1 else []

                # Product summary
                prod_counts: dict[str, dict] = {}
                host_map = {}
                for h in hosts:
                    prod, tier = _classify_host(h.get("groups", []))
                    if not prod or prod == "Unknown":
                        continue
                    if product and product.lower() not in prod.lower():
                        continue
                    key = f"{prod} / {tier}"
                    prod_counts.setdefault(key, {"count": 0})
                    prod_counts[key]["count"] += 1
                    host_map[h["hostid"]] = h
                    h["_product"] = prod
                    h["_tier"] = tier

                # CPU metrics for high-CPU section
                high_cpu_lines = []
                if include_high_cpu and host_map:
                    cpu_items = await client.call("item.get", {
                        "hostids": list(host_map.keys()),
                        "output": ["hostid", "lastvalue"],
                        "filter": {"key_": "system.cpu.util[,idle]"},
                    })
                    for item in cpu_items:
                        try:
                            cpu = round(100 - float(item["lastvalue"]), 1)
                        except (ValueError, TypeError):
                            continue
                        if cpu >= cpu_threshold:
                            h = host_map.get(item["hostid"])
                            if h:
                                ip = next((i["ip"] for i in h.get("interfaces", []) if i.get("ip") != "127.0.0.1"), "")
                                high_cpu_lines.append(
                                    f"• `{h['host']}` ({h.get('_product', '')}/{h.get('_tier', '')}) "
                                    f"— *{cpu}%* ({ip})"
                                )
                    high_cpu_lines.sort()

                # Build Slack blocks
                blocks = [
                    {"type": "header", "text": {"type": "plain_text", "text": title}},
                    {"type": "section", "text": {"type": "mrkdwn", "text": f"*{len(host_map)} servers* across {len(prod_counts)} product tiers"}},
                ]

                # Product summary as a compact section
                prod_text = "\n".join(
                    f"• {k}: *{v['count']}*"
                    for k, v in sorted(prod_counts.items(), key=lambda x: -x[1]["count"])[:15]
                )
                if prod_text:
                    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Products:*\n{prod_text}"}})

                # Problems summary
                if problems:
                    sev_names = {"0": "NC", "1": "Info", "2": "Warn", "3": "Avg", "4": "High", "5": "Disaster"}
                    sev_counts: dict[str, int] = {}
                    for p in problems:
                        s = sev_names.get(p.get("severity", "0"), "?")
                        sev_counts[s] = sev_counts.get(s, 0) + 1

                    prob_text = " | ".join(f"*{s}*: {c}" for s, c in
                        sorted(sev_counts.items(), key=lambda x: -x[1]))
                    blocks.append({"type": "divider"})
                    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f":warning: *Active Problems ({len(problems)}):*\n{prob_text}"}})

                # High CPU
                if high_cpu_lines:
                    blocks.append({"type": "divider"})
                    cpu_text = "\n".join(high_cpu_lines[:10])
                    if len(high_cpu_lines) > 10:
                        cpu_text += f"\n_...and {len(high_cpu_lines) - 10} more_"
                    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f":fire: *High CPU (>{cpu_threshold}%):*\n{cpu_text}"}})

                # Footer
                now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"Generated by zbbx-mcp at {now}"}]})

                # Send
                payload = {"text": title, "blocks": blocks}
                async with httpx.AsyncClient(timeout=10) as http:
                    resp = await http.post(url, json=payload)
                    if resp.status_code == 200 and resp.text == "ok":
                        summary = f"Report sent to Slack: {len(host_map)} servers"
                        if problems:
                            summary += f", {len(problems)} problems"
                        if high_cpu_lines:
                            summary += f", {len(high_cpu_lines)} high CPU"
                        return summary
                    return f"Slack returned: {resp.status_code} {resp.text[:200]}"
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"
