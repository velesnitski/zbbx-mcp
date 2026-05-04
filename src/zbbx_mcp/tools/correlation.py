"""Host correlation tools — wave outages and idle-relay detection.

These tools surface a class of failures the per-host views miss:
- A relay where the management NIC carries traffic but every other (tunnel)
  interface reads zero bytes/sec — host is up but routing nothing.
- A wave of independent host alerts inside a short window on the same /24
  or hostgroup — likely a shared-infrastructure event rather than N
  unrelated incidents.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timezone

import httpx

from zbbx_mcp.classify import detect_provider
from zbbx_mcp.data import (
    STATUS_ENABLED,
    TRAFFIC_IN_KEYS,
    extract_country,
    host_ip,
)
from zbbx_mcp.formatters import normalize_problem_name
from zbbx_mcp.resolver import InstanceResolver

# Cluster grouping levels, ordered narrowest to broadest.
_AUTO_LEVELS: tuple[str, ...] = ("subnet24", "subnet16", "provider")

_SEV_LABELS = {0: "Info", 1: "Info", 2: "Warning", 3: "Average", 4: "High", 5: "Disaster"}

# Interface names ignored for tunnel-counting purposes (kernel/system, not relays).
_IGNORED_IFACES: frozenset[str] = frozenset({"lo", "docker0"})

# Regex fallback for physical-NIC names not in the curated TRAFFIC_IN_KEYS list.
# Catches unused secondary NICs (eno3, enp130s0f0, USB ethernet enxMAC, etc.)
# that would otherwise be misbucketed as tunnels by the exclusion logic.
_PHYSICAL_NIC_RE = re.compile(r"^(?:eno|enp|enx|eth|ens|bond|ppp|wlan)\d")


def _iface_from_key(key: str) -> str:
    """Extract the interface name from a `net.if.in[<iface>]` item key."""
    if not key.startswith("net.if.in[") or not key.endswith("]"):
        return ""
    return key[len("net.if.in["):-1]


def _split_iface_metrics(
    items: list[dict],
    physical_keys: frozenset[str],
) -> dict[str, dict]:
    """Bucket per-host net.if.in items into physical vs tunnel.

    Physical = exact match on the curated physical-interface key list.
    Tunnel   = any other net.if.in[*] that is not loopback/docker.

    Returns:
        {hostid: {"physical_bps": float, "tunnel_bps": float,
                  "tunnel_count": int, "tunnel_names": list[str]}}
    """
    per_host: dict[str, dict] = {}
    for it in items:
        key = it.get("key_", "")
        iface = _iface_from_key(key)
        if not iface:
            continue
        if iface in _IGNORED_IFACES or iface.startswith("docker") or iface.startswith("br-"):
            continue
        try:
            val = float(it.get("lastvalue", "0") or 0)
        except (ValueError, TypeError):
            val = 0.0
        hid = it.get("hostid")
        if not hid:
            continue
        slot = per_host.setdefault(
            hid,
            {"physical_bps": 0.0, "tunnel_bps": 0.0, "tunnel_count": 0, "tunnel_names": []},
        )
        if key in physical_keys or _PHYSICAL_NIC_RE.match(iface):
            slot["physical_bps"] += val
        else:
            slot["tunnel_bps"] += val
            slot["tunnel_count"] += 1
            slot["tunnel_names"].append(iface)
    return per_host


def _find_idle_relays(
    per_host: dict[str, dict],
    min_mgmt_kbps: float,
) -> list[tuple[str, float, int, list[str]]]:
    """Return [(hostid, mgmt_kbps, tunnel_count, sample_tunnels), ...].

    A relay is "idle" when:
    - At least one tunnel-class interface exists (else it is not a relay).
    - The aggregate physical-NIC throughput is at or above min_mgmt_kbps.
    - Every tunnel interface reports zero bytes/sec.
    """
    out: list[tuple[str, float, int, list[str]]] = []
    for hid, data in per_host.items():
        if data["tunnel_count"] == 0:
            continue
        if data["tunnel_bps"] > 0:
            continue
        mgmt_kbps = data["physical_bps"] / 1000.0
        if mgmt_kbps < min_mgmt_kbps:
            continue
        sample = sorted(set(data["tunnel_names"]))[:4]
        out.append((hid, mgmt_kbps, data["tunnel_count"], sample))
    out.sort(key=lambda r: -r[1])
    return out


def _cluster_problems(
    records: list[dict],
    window_sec: int,
    min_hosts: int,
) -> list[dict]:
    """Greedy time-window clustering of problem records sharing a group key.

    Each record needs: clock (int), hostid (str), host (str), name (str),
    severity (int), key (str — subnet or hostgroup name).

    Within each key, records are sorted by clock and grouped into maximal
    runs whose first→last span is ≤ window_sec. A run becomes a cluster only
    when it covers ≥ min_hosts distinct hostids.
    """
    per_key: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        per_key[r["key"]].append(r)

    clusters: list[dict] = []
    for key, r_list in per_key.items():
        r_list.sort(key=lambda r: r["clock"])
        i = 0
        n = len(r_list)
        while i < n:
            j = i
            while j + 1 < n and r_list[j + 1]["clock"] - r_list[i]["clock"] <= window_sec:
                j += 1
            bucket = r_list[i:j + 1]
            uniq_hosts = {r["hostid"] for r in bucket}
            if len(uniq_hosts) >= min_hosts:
                clusters.append({
                    "key": key,
                    "host_count": len(uniq_hosts),
                    "hosts": sorted({r["host"] for r in bucket}),
                    "start": bucket[0]["clock"],
                    "end": bucket[-1]["clock"],
                    "events": len(bucket),
                    "problems": sorted({r["name"][:60] for r in bucket}),
                    "max_severity": max(r["severity"] for r in bucket),
                })
                i = j + 1
            else:
                i += 1
    clusters.sort(key=lambda c: (-c["host_count"], -c["max_severity"]))
    return clusters


def _subnet24(ip: str) -> str:
    """Return the /24 CIDR for an IPv4 address, or '' for non-IPv4."""
    if not ip or "." not in ip:
        return ""
    parts = ip.split(".")
    if len(parts) != 4:
        return ""
    return ".".join(parts[:3]) + ".0/24"


def _subnet16(ip: str) -> str:
    """Return the /16 CIDR for an IPv4 address, or '' for non-IPv4."""
    if not ip or "." not in ip:
        return ""
    parts = ip.split(".")
    if len(parts) != 4:
        return ""
    return ".".join(parts[:2]) + ".0.0/16"


def _group_key(
    level: str,
    *,
    ip: str = "",
    hostgroup: str = "",
    provider: str = "",
) -> str:
    """Return the cluster grouping key at the requested level, or '' if N/A.

    Level vocabulary:
        subnet24    — host's /24
        subnet16    — host's /16
        provider    — hosting provider (ASN proxy via PROVIDER_CIDRS)
        hostgroup   — first hostgroup name

    'auto' is handled at the caller level by trying levels narrowest-first.
    """
    if level == "subnet24":
        return _subnet24(ip)
    if level == "subnet16":
        return _subnet16(ip)
    if level == "provider":
        # detect_provider() returns 'Other' / 'Unknown' for IPs we cannot map.
        # Treat those as no key — clustering on 'Other' would lump unrelated hosts.
        if not provider or provider in {"Other", "Unknown"}:
            return ""
        return provider
    if level == "hostgroup":
        return hostgroup
    return ""


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()) -> None:

    if "get_idle_relays" not in skip:

        @mcp.tool()
        async def get_idle_relays(
            min_mgmt_kbps: int = 100,
            max_results: int = 50,
            country: str = "",
            instance: str = "",
        ) -> str:
            """Find relay hosts where mgmt NIC has traffic but tunnel interfaces are at zero.

            NAT-mode relays (no tunnels by design) may show as false positives;
            cross-check the architecture before acting. See ADR 010, 015.

            Args:
                min_mgmt_kbps: Floor on aggregate physical-NIC throughput (default: 100)
                max_results: Maximum results (default: 50)
                country: 2-letter country code filter (optional)
                instance: Zabbix instance name (optional)
            """
            try:
                client = resolver.resolve(instance)
                hosts = await client.call("host.get", {
                    "output": ["hostid", "host", "name"],
                    "selectInterfaces": ["ip"],
                    "filter": {"status": STATUS_ENABLED},
                })
                if country:
                    cc = country.upper()
                    hosts = [h for h in hosts if extract_country(h.get("host", "")) == cc]
                if not hosts:
                    return "No hosts found."

                host_ids = [h["hostid"] for h in hosts]
                host_map = {h["hostid"]: h for h in hosts}

                items = await client.call("item.get", {
                    "hostids": host_ids,
                    "output": ["hostid", "key_", "lastvalue"],
                    "search": {"key_": "net.if.in["},
                    "filter": {"status": "0"},
                })

                physical_keys = frozenset(TRAFFIC_IN_KEYS)
                per_host = _split_iface_metrics(items, physical_keys)
                idle = _find_idle_relays(per_host, float(min_mgmt_kbps))

                if not idle:
                    return f"No idle relays found ({len(per_host)} hosts inspected)."

                shown = idle[:max_results]
                lines = [
                    f"**{len(idle)} idle relays** "
                    f"(mgmt ≥ {min_mgmt_kbps} kbps, all tunnel interfaces at 0 bps)\n",
                    "| Host | IP | Mgmt kbps | Idle tunnels | Sample |",
                    "|------|----|-----------|--------------|--------|",
                ]
                for hid, mgmt_kbps, tun_count, sample in shown:
                    h = host_map.get(hid, {})
                    ip = host_ip(h)
                    sample_str = ", ".join(sample) if sample else "—"
                    lines.append(
                        f"| {h.get('host', '?')} | {ip} | "
                        f"{mgmt_kbps:.1f} | {tun_count} | {sample_str} |"
                    )
                if len(idle) > max_results:
                    lines.append(f"\n*{len(idle) - max_results} more omitted*")
                return "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"

    if "get_outage_clusters" not in skip:

        @mcp.tool()
        async def get_outage_clusters(
            window_min: int = 30,
            min_hosts: int = 3,
            group_by: str = "auto",
            min_severity: int = 3,
            max_clusters: int = 10,
            instance: str = "",
        ) -> str:
            """Cluster outages on hosts sharing a network or group key.

            Args:
                window_min: Time window in minutes for clustering (default: 30)
                min_hosts: Minimum unique hosts per cluster (default: 3)
                group_by: "subnet24" / "subnet16" / "provider" / "hostgroup" /
                          "auto" (default: "auto" — narrowest level with hits wins)
                min_severity: Minimum severity 0-5 (default: 3 = Average)
                max_clusters: Max clusters to render (default: 10)
                instance: Zabbix instance name (optional)
            """
            # Backwards-compat alias for the original parameter value.
            if group_by == "subnet":
                group_by = "subnet24"
            valid = {"subnet24", "subnet16", "provider", "hostgroup", "auto"}
            try:
                if group_by not in valid:
                    return (
                        f"Invalid group_by: {group_by!r}. "
                        f"Use one of {sorted(valid)}."
                    )
                client = resolver.resolve(instance)
                # NB Zabbix 6.4 rejects sortfield="clock" on problem.get. Use the
                # accepted "eventid" sort and re-order by clock in Python after
                # the fetch — eventid is monotone with creation time so this only
                # matters for the LIMIT cutoff, where eventid sort is fine.
                problems = await client.call("problem.get", {
                    "output": ["eventid", "name", "severity", "clock"],
                    "severities": list(range(min_severity, 6)),
                    "sortfield": "eventid",
                    "sortorder": "DESC",
                    "limit": 2000,
                    "recent": True,
                })
                problems.sort(key=lambda p: -int(p.get("clock", 0)))
                if not problems:
                    return f"No active problems (severity >= {min_severity})."

                event_ids = [p["eventid"] for p in problems]
                events = await client.call("event.get", {
                    "output": ["eventid"],
                    "selectHosts": ["hostid"],
                    "eventids": event_ids,
                })
                event_hosts = {
                    e["eventid"]: [h["hostid"] for h in e.get("hosts", [])]
                    for e in events
                }

                all_host_ids = sorted({hid for ids in event_hosts.values() for hid in ids})
                if not all_host_ids:
                    return "No host-bound problems found."

                hosts_meta = await client.call("host.get", {
                    "hostids": all_host_ids,
                    "output": ["hostid", "host"],
                    "selectInterfaces": ["ip"],
                    "selectGroups": ["name"],
                })
                host_meta = {h["hostid"]: h for h in hosts_meta}

                # Pre-compute every level's key per host so auto-fallback is cheap.
                host_keys: dict[str, dict[str, str]] = {}
                for hid, hm in host_meta.items():
                    ip = host_ip(hm)
                    provider = detect_provider(ip) if ip else ""
                    groups = [g.get("name", "") for g in hm.get("groups", []) if g.get("name")]
                    hostgroup = groups[0] if groups else ""
                    host_keys[hid] = {
                        "subnet24": _group_key("subnet24", ip=ip),
                        "subnet16": _group_key("subnet16", ip=ip),
                        "provider": _group_key("provider", provider=provider),
                        "hostgroup": _group_key("hostgroup", hostgroup=hostgroup),
                    }

                def _build_records(level: str) -> list[dict]:
                    out: list[dict] = []
                    for p in problems:
                        for hid in event_hosts.get(p["eventid"], []):
                            hm = host_meta.get(hid)
                            if not hm:
                                continue
                            key = host_keys.get(hid, {}).get(level, "")
                            if not key:
                                continue
                            raw_name = p.get("name", "?")
                            host_label = hm.get("host", "")
                            out.append({
                                "clock": int(p.get("clock", 0)),
                                "hostid": hid,
                                "host": host_label,
                                "name": normalize_problem_name(raw_name, host_label) or raw_name,
                                "severity": int(p.get("severity", 0)),
                                "key": key,
                            })
                    return out

                # Auto-fallback: try narrowest level first; broaden until we hit clusters.
                levels_to_try = _AUTO_LEVELS if group_by == "auto" else (group_by,)
                clusters: list[dict] = []
                effective_level = group_by
                for level in levels_to_try:
                    records = _build_records(level)
                    if not records:
                        continue
                    clusters = _cluster_problems(records, window_min * 60, min_hosts)
                    if clusters:
                        effective_level = level
                        break

                if not clusters:
                    return (
                        f"No outage clusters (window={window_min}m, "
                        f"min_hosts={min_hosts}, group_by={group_by})."
                    )

                shown = clusters[:max_clusters]
                level_note = (
                    f"by {effective_level} (auto)"
                    if group_by == "auto" and effective_level != "auto"
                    else f"by {group_by}"
                )
                lines = [
                    f"**{len(clusters)} outage clusters** "
                    f"(window {window_min}m, ≥{min_hosts} hosts, {level_note})\n",
                ]
                for idx, c in enumerate(shown, 1):
                    t0 = datetime.fromtimestamp(c["start"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                    t1 = datetime.fromtimestamp(c["end"], timezone.utc).strftime("%H:%M")
                    sev = _SEV_LABELS.get(c["max_severity"], "?")
                    sample_hosts = ", ".join(c["hosts"][:5])
                    more = f" +{len(c['hosts']) - 5}" if len(c["hosts"]) > 5 else ""
                    sample_problems = "; ".join(c["problems"][:3])
                    lines.append(
                        f"### Cluster {idx} — {c['key']}\n"
                        f"- **Hosts:** {c['host_count']} ({sample_hosts}{more})\n"
                        f"- **When:** {t0} → {t1} ({c['events']} events)\n"
                        f"- **Max severity:** {sev}\n"
                        f"- **Problems:** {sample_problems}\n"
                    )
                if len(clusters) > max_clusters:
                    lines.append(f"\n*{len(clusters) - max_clusters} more clusters omitted*")
                return "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"
