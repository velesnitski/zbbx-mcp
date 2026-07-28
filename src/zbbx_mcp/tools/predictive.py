"""Predictive alerting: project a metric's trend to a threshold crossing.

Split out of executive.py (ADR 097) — the module hit its size budget, and
forecasting is a distinct domain from KPI/period reporting: it regresses a
trend series rather than summarising a snapshot.
"""

from __future__ import annotations

import time as _time

import httpx

from zbbx_mcp.data import GB_DECIMAL, MB_DECIMAL, fetch_enabled_hosts
from zbbx_mcp.resolver import InstanceResolver


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()) -> None:

    # --- Predictive alerts (#99) ---

    if "get_predictive_alerts" not in skip:

        @mcp.tool()
        async def get_predictive_alerts(
            metric: str = "all",
            days_ahead: int = 30,
            max_results: int = 20,
            instance: str = "",
        ) -> str:
            """Predict upcoming problems — disk full, CPU saturation, memory exhaustion.

            Uses linear regression on 14-day trend data to project when thresholds
            will be crossed. Zero dependencies — pure math.

            Severity tiers (Disk + Memory):
                CRITICAL = current already in the danger zone AND projection < 3 days
                HIGH     = current concerning AND projection < 7 days
                WARNING  = projection < 14 days
                INFO     = projection within horizon but neither imminent nor severe

            The sanity floor (current-value gate) prevents a wall-of-CRITICAL when
            many hosts have small noisy upward slopes from low starting points —
            the projection alone is not enough; the current state has to actually
            be near failure.

            CPU keeps the simpler 2-band CRITICAL/WARNING/INFO split.

            Args:
                metric: What to predict: disk, cpu, memory, traffic, or all (default: all)
                days_ahead: Forecast horizon in days (default: 30)
                max_results: Maximum alerts (default: 20)
                instance: Zabbix instance name (optional)
            """
            try:
                client = resolver.resolve(instance)

                hosts = await fetch_enabled_hosts(client, groups=True, interfaces=False,
                                                  extra_output=["name"])
                all_ids = [h["hostid"] for h in hosts]
                host_names = {h["hostid"]: h["host"] for h in hosts}

                # Define metrics to check
                _METRICS = {
                    "disk": {
                        # Explicit wildcards: this term is used with
                        # searchWildcardsEnabled below, which makes a bare
                        # literal an EXACT match — so it fetched zero disk
                        # items and the whole disk forecast silently never
                        # ran (ADR 094 class, missed because the term lives
                        # in a config dict rather than the call site).
                        "search": {"key_": "*vfs.fs.size[*"},
                        "filter_key": "pfree",
                        "threshold": 15,  # alert when free% drops below this
                        "direction": "below",
                        "unit": "% free",
                        "label": "Disk Full",
                    },
                    "cpu": {
                        "filter": {"key_": "system.cpu.util[,idle]"},
                        "threshold": 20,  # alert when idle% drops below this (= >80% used)
                        "direction": "below",
                        "unit": "% idle",
                        "label": "CPU Saturation",
                    },
                    "memory": {
                        "filter": {"key_": "vm.memory.size[available]"},
                        "threshold": 500_000_000,  # 500 MB
                        "direction": "below",
                        "unit": "bytes avail",
                        "label": "Memory Exhaustion",
                    },
                }

                metrics_to_check = _METRICS if metric == "all" else {metric: _METRICS[metric]} if metric in _METRICS else {}
                if not metrics_to_check:
                    return f"Unknown metric '{metric}'. Use: disk, cpu, memory, or all."

                now = int(_time.time())
                time_from = now - 14 * 86400  # 14 days of trend data

                alerts = []

                for metric_name, cfg in metrics_to_check.items():
                    # Fetch items
                    params = {
                        "hostids": all_ids,
                        "output": ["itemid", "hostid", "key_", "lastvalue"],
                        "filter": {"status": "0"},
                    }
                    if "search" in cfg:
                        params["search"] = cfg["search"]
                        params["searchWildcardsEnabled"] = True
                        if "filter_key" in cfg:
                            # Post-filter by key substring
                            pass
                    if "filter" in cfg:
                        params["filter"].update(cfg["filter"])

                    items = await client.call("item.get", params)

                    # Handle disk: accept both pfree and pused items
                    if "filter_key" in cfg:
                        pfree_items = [it for it in items if "pfree" in it.get("key_", "")]
                        pused_items = [it for it in items if "pused" in it.get("key_", "")]
                        # Convert pused → pfree equivalent (100 - pused)
                        for it in pused_items:
                            try:
                                it["lastvalue"] = str(100 - float(it.get("lastvalue", 0)))
                                it["_converted"] = True
                            except (ValueError, TypeError):
                                pass
                        items = pfree_items + pused_items

                    # Deduplicate: one item per host (pick the one with lowest current value)
                    best_item: dict[str, dict] = {}
                    for it in items:
                        hid = it["hostid"]
                        try:
                            val = float(it.get("lastvalue", 0))
                        except (ValueError, TypeError):
                            continue
                        if hid not in best_item or val < float(best_item[hid].get("lastvalue", 0)):
                            best_item[hid] = it

                    if not best_item:
                        continue

                    # Fetch 7-day trends for these items
                    item_ids = [it["itemid"] for it in best_item.values()]
                    # Batch to avoid oversized requests
                    all_trends = []
                    for i in range(0, len(item_ids), 200):
                        chunk = item_ids[i:i + 200]
                        trends = await client.call("trend.get", {
                            "itemids": chunk,
                            "time_from": time_from,
                            "output": ["itemid", "clock", "value_avg"],
                            "limit": len(chunk) * 24 * 14,
                        })
                        all_trends.extend(trends)

                    # Group trends by item
                    item_trends: dict[str, list[tuple[int, float]]] = {}
                    for t in all_trends:
                        iid = t["itemid"]
                        try:
                            item_trends.setdefault(iid, []).append(
                                (int(t["clock"]), float(t["value_avg"]))
                            )
                        except (ValueError, TypeError):
                            pass

                    # Linear regression per host
                    threshold = cfg["threshold"]
                    direction = cfg["direction"]

                    for hid, it in best_item.items():
                        iid = it["itemid"]
                        points = sorted(item_trends.get(iid, []))
                        # A `pused` item had only its lastvalue flipped to
                        # free-%; its TREND stayed used-%, so the slope ran
                        # opposite to `current`. A filling disk then showed a
                        # positive slope and was dropped by the "not declining"
                        # guard (no alert), while a disk being freed projected
                        # a fake exhaustion date. Flip the series to match the
                        # value it is regressed against (ADR 097).
                        if it.get("_converted"):
                            points = [(c, 100.0 - v) for c, v in points]
                        # Min samples raised 5 → 14: trend data is hourly-aggregated and
                        # 5 buckets is too noisy for a stable 14-day regression. 14 keeps
                        # the slope grounded; brand-new hosts (< 14 trend points) fall
                        # through silently rather than producing junk predictions.
                        if len(points) < 14:
                            continue

                        try:
                            current = float(it.get("lastvalue", 0))
                        except (ValueError, TypeError):
                            continue

                        # Simple linear regression: least squares
                        n = len(points)
                        x_vals = [(p[0] - points[0][0]) / 86400 for p in points]  # days
                        y_vals = [p[1] for p in points]
                        x_mean = sum(x_vals) / n
                        y_mean = sum(y_vals) / n
                        num = sum((x - x_mean) * (y - y_mean) for x, y in zip(x_vals, y_vals, strict=False))
                        den = sum((x - x_mean) ** 2 for x in x_vals)
                        if den == 0:
                            continue
                        slope = num / den  # units per day

                        # Project days to threshold
                        if direction == "below":
                            if slope >= 0:
                                continue  # not declining
                            days_to = 0 if current <= threshold else (threshold - current) / slope
                        else:  # above
                            if slope <= 0:
                                continue  # not growing
                            days_to = 0 if current >= threshold else (threshold - current) / slope

                        if days_to < 0 or days_to > days_ahead:
                            continue

                        # Format current value for display
                        if metric_name == "memory":
                            curr_display = f"{current / GB_DECIMAL:.1f} GB"
                            rate_display = f"{abs(slope) / MB_DECIMAL:.0f} MB/day"
                        elif metric_name == "disk":
                            curr_display = f"{current:.1f}%"
                            rate_display = f"{abs(slope):.2f}%/day"
                        elif metric_name == "cpu":
                            curr_display = f"{100 - current:.1f}% used"
                            rate_display = f"{abs(slope):.2f}%/day"
                        else:
                            curr_display = f"{current:.1f}"
                            rate_display = f"{abs(slope):.2f}/day"

                        hostname = host_names.get(hid, hid)
                        # Sanity-floored severity (see tool docstring). Disk + Memory
                        # require BOTH an imminent projection AND a concerning current
                        # value before earning CRITICAL — kills the wall-of-CRITICAL
                        # noise where a low starting point + small noisy slope projects
                        # disaster within the week.
                        if metric_name == "disk":
                            # `current` is free %; danger floor: ≤ 30% free (= ≥ 70% used)
                            if days_to < 3 and current <= 30:
                                severity = "CRITICAL"
                            elif days_to < 7 and current <= 50:
                                severity = "HIGH"
                            elif days_to < 14:
                                severity = "WARNING"
                            else:
                                severity = "INFO"
                        elif metric_name == "memory":
                            # `current` is bytes available; floors at 1 GB / 2 GB
                            if days_to < 3 and current <= GB_DECIMAL:
                                severity = "CRITICAL"
                            elif days_to < 7 and current <= 2 * GB_DECIMAL:
                                severity = "HIGH"
                            elif days_to < 14:
                                severity = "WARNING"
                            else:
                                severity = "INFO"
                        else:
                            # CPU and any other metric — simpler 2-band; tiering CPU
                            # via a "current idle %" floor doesn't add much signal
                            # because CPU swings are short-window and the linear
                            # projection is a weaker fit than for disk/RAM.
                            if days_to < 7:
                                severity = "CRITICAL"
                            elif days_to < 14:
                                severity = "WARNING"
                            else:
                                severity = "INFO"

                        alerts.append({
                            "severity": severity,
                            "label": cfg["label"],
                            "host": hostname,
                            "current": curr_display,
                            "rate": rate_display,
                            "days": round(days_to, 1),
                            "days_raw": days_to,
                        })

                if not alerts:
                    return f"No predicted issues within {days_ahead} days."

                # Collapse cluster duplicates: same base hostname + metric + near-identical
                # current/rate values. Cluster secondaries (e.g. "srv-us901 us903") share
                # underlying hardware and produce identical trend data.
                raw_count = len(alerts)
                groups: dict[str, list] = {}
                for a in alerts:
                    base = a["host"].split()[0]
                    key = f"{base}|{a['label']}|{a['current']}|{a['rate']}"
                    groups.setdefault(key, []).append(a)

                deduped = []
                for _key, members in groups.items():
                    rep = members[0]
                    if len(members) > 1:
                        names = [m["host"].split()[-1] for m in members[1:]]
                        rep = {**rep, "host": f"{rep['host']} (+{len(members) - 1}: {', '.join(names[:3])})"}
                    deduped.append(rep)

                deduped.sort(key=lambda a: a["days_raw"])
                shown = deduped[:max_results]
                collapsed = raw_count - len(deduped)

                header_suffix = f" ({collapsed} cluster duplicates collapsed)" if collapsed else ""
                lines = [f"**{len(deduped)} predicted issues**{header_suffix} (next {days_ahead} days)\n"]
                lines.append("| Severity | Issue | Server | Current | Rate | Days Left |")
                lines.append("|----------|-------|--------|---------|------|----------|")
                for a in shown:
                    # Severity is already one of CRITICAL / HIGH / WARNING / INFO
                    # (set by the sanity-floored classifier). Render it directly —
                    # the previous ternary only knew CRITICAL/WARNING and silently
                    # collapsed the HIGH tier to INFO, burying real near-term risks.
                    lines.append(
                        f"| {a['severity']} | {a['label']} | {a['host']} | "
                        f"{a['current']} | {a['rate']} | {a['days']} |"
                    )

                crit = sum(1 for a in deduped if a["severity"] == "CRITICAL")
                high = sum(1 for a in deduped if a["severity"] == "HIGH")
                warn = sum(1 for a in deduped if a["severity"] == "WARNING")
                if crit:
                    lines.append(f"\n**{crit} CRITICAL** — act now (≤3 days)")
                if high:
                    lines.append(f"**{high} HIGH** — act this week (≤7 days)")
                if warn:
                    lines.append(f"**{warn} WARNING** — within 2 weeks")
                if len(deduped) > max_results:
                    lines.append(f"*{len(deduped) - max_results} more omitted*")

                return "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"
