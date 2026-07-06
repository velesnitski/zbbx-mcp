"""Self-introspection tool: read the local analytics log and summarise tool usage.

Every call to every registered tool already writes a JSONL line to
``~/.zbbx-mcp/analytics.log`` (override with ``ZABBIX_ANALYTICS_FILE``).
Each line carries the tool name, parameters with sensitive values
scrubbed, duration in milliseconds, an ok/error status, and the
response size.

``get_telemetry_summary`` reads that file and reports per-tool call
counts, error rates, and latency percentiles — the evidence needed to
decide which tools are pulling weight, which to leave in
``ZABBIX_TIER=core``, and which to deprecate.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from zbbx_mcp.resolver import InstanceResolver


def _default_log_path() -> Path:
    """Resolve the analytics log path the same way setup_logging() does."""
    return Path(
        os.environ.get(
            "ZABBIX_ANALYTICS_FILE",
            str(Path.home() / ".zbbx-mcp" / "analytics.log"),
        )
    )


def _summarise_records(
    records: list[dict],
    *,
    since_ts: float | None = None,
) -> list[dict]:
    """Group analytics records by tool and compute summary stats.

    Each input dict needs ``tool``, ``status``, ``duration_ms``. Records
    older than ``since_ts`` (Unix seconds) are dropped. Returns a list
    sorted by call count descending.
    """
    per_tool: dict[str, dict] = {}
    for r in records:
        if since_ts is not None:
            ts_raw = r.get("ts") or r.get("@timestamp")
            if ts_raw:
                try:
                    # ISO 8601 with Z suffix
                    if isinstance(ts_raw, str) and ts_raw.endswith("Z"):
                        import datetime as _dt
                        when = _dt.datetime.strptime(
                            ts_raw, "%Y-%m-%dT%H:%M:%SZ",
                        ).replace(tzinfo=_dt.timezone.utc).timestamp()
                    else:
                        when = float(ts_raw)
                    if when < since_ts:
                        continue
                except (ValueError, TypeError):
                    pass
        tool = r.get("tool", "?")
        slot = per_tool.setdefault(
            tool,
            {
                "tool": tool,
                "calls": 0,
                "errors": 0,
                "duration_total": 0,
                "duration_max": 0,
                "response_size_total": 0,
            },
        )
        slot["calls"] += 1
        if r.get("status") != "ok":
            slot["errors"] += 1
        try:
            d = int(r.get("duration_ms", 0) or 0)
        except (ValueError, TypeError):
            d = 0
        slot["duration_total"] += d
        if d > slot["duration_max"]:
            slot["duration_max"] = d
        try:
            slot["response_size_total"] += int(r.get("response_size", 0) or 0)
        except (ValueError, TypeError):
            pass

    rows: list[dict] = []
    for slot in per_tool.values():
        calls = slot["calls"]
        rows.append({
            "tool": slot["tool"],
            "calls": calls,
            "errors": slot["errors"],
            "error_pct": round(100.0 * slot["errors"] / calls, 1) if calls else 0.0,
            "avg_ms": round(slot["duration_total"] / calls, 1) if calls else 0,
            "max_ms": slot["duration_max"],
            "avg_response_chars": round(slot["response_size_total"] / calls) if calls else 0,
            "response_chars_total": slot["response_size_total"],
        })
    rows.sort(key=lambda r: -r["calls"])
    return rows


_CHARS_PER_TOKEN = 4  # conventional rough estimate for English/markdown


def _token_footer(rows: list[dict]) -> str:
    """One-line token-cost estimate across all summarised tools (ADR 073).

    Turns the accumulated response sizes into the answer to "are we
    token-effective" — total chars, estimated tokens (~4 chars/token),
    and the per-call average. Returns "" when there is nothing to count.
    """
    total_chars = sum(r.get("response_chars_total", 0) for r in rows)
    total_calls = sum(r.get("calls", 0) for r in rows)
    if not total_chars or not total_calls:
        return ""
    est_tokens = round(total_chars / _CHARS_PER_TOKEN)
    return (
        f"Σ responses: {total_chars:,} chars ≈ {est_tokens:,} tokens "
        f"(~{round(est_tokens / total_calls)} tokens/call, est. "
        f"{_CHARS_PER_TOKEN} chars/token)"
    )


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()) -> None:

    if "get_telemetry_summary" not in skip:

        @mcp.tool()
        async def get_telemetry_summary(
            hours: int = 0,
            top: int = 30,
            log_path: str = "",
        ) -> str:
            """Summarise local tool-call telemetry from the analytics log.

            Reads ``~/.zbbx-mcp/analytics.log`` (or ``ZABBIX_ANALYTICS_FILE``)
            and reports per-tool call count, error rate, and latency.

            Args:
                hours: Look back this many hours; 0 = all records (default: 0)
                top: Max rows to render (default: 30)
                log_path: Override the log file path (default: use env)
            """
            path = Path(log_path) if log_path else _default_log_path()
            if not path.exists():
                return (
                    f"No analytics log at {path}. The log is written automatically "
                    f"per tool call; if it is missing, the server has not been "
                    f"used yet or ZABBIX_ANALYTICS_FILE points elsewhere."
                )

            since_ts = None
            if hours > 0:
                since_ts = time.time() - hours * 3600

            records: list[dict] = []
            parse_errors = 0
            try:
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            records.append(json.loads(line))
                        except json.JSONDecodeError:
                            parse_errors += 1
            except OSError as e:
                return f"Error reading analytics log: {e}"

            if not records:
                return f"Analytics log is empty: {path}"

            summary = _summarise_records(records, since_ts=since_ts)
            if not summary:
                window = f"last {hours}h" if hours > 0 else "all time"
                return f"No tool calls recorded in {window}."

            total_calls = sum(r["calls"] for r in summary)
            total_errors = sum(r["errors"] for r in summary)
            unique_tools = len(summary)
            window = f"last {hours}h" if hours > 0 else "all time"

            lines = [
                f"**Telemetry summary** ({total_calls} calls across "
                f"{unique_tools} tools, {total_errors} errors, {window})\n",
                "| Tool | Calls | Errors | Err % | Avg ms | Max ms | Avg chars |",
                "|------|------:|-------:|------:|-------:|-------:|----------:|",
            ]
            for row in summary[:top]:
                lines.append(
                    f"| {row['tool']} | {row['calls']} | {row['errors']} | "
                    f"{row['error_pct']:.1f} | {row['avg_ms']:.1f} | "
                    f"{row['max_ms']} | {row['avg_response_chars']} |"
                )
            if len(summary) > top:
                lines.append(f"\n*{len(summary) - top} more tools omitted*")
            footer = _token_footer(summary)
            if footer:
                lines.append(f"\n{footer}")
            if parse_errors:
                lines.append(f"\n*{parse_errors} malformed line(s) skipped*")
            return "\n".join(lines)
