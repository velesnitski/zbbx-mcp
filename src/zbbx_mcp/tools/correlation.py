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
    build_parent_map,
    canonical_host_name,
    extract_country,
    filter_suppressed,
    host_ip,
)
from zbbx_mcp.formatters import format_age, normalize_problem_name
from zbbx_mcp.resolver import InstanceResolver

# Cluster grouping levels, ordered narrowest to broadest.
_AUTO_LEVELS: tuple[str, ...] = ("subnet24", "subnet16", "provider")

_SEV_LABELS = {0: "Info", 1: "Info", 2: "Warning", 3: "Average", 4: "High", 5: "Disaster"}


# _format_age moved to zbbx_mcp.formatters.format_age (output formatter).

# Interface names ignored for tunnel-counting purposes (kernel/system, not relays).
_IGNORED_IFACES: frozenset[str] = frozenset({"lo", "docker0"})

# Regex fallback for physical-NIC names not in the curated TRAFFIC_IN_KEYS list.
# Catches unused secondary NICs (eno3, enp130s0f0, USB ethernet enxMAC, etc.)
# that would otherwise be misbucketed as tunnels by the exclusion logic.
_PHYSICAL_NIC_RE = re.compile(r"^(?:eno|enp|enx|eth|ens|bond|ppp|wlan)\d")


def _iface_from_key(key: str) -> str:
    """Extract the interface name from a `net.if.in[<iface>]` / `net.if.out[<iface>]` key."""
    for pre in ("net.if.in[", "net.if.out["):
        if key.startswith(pre) and key.endswith("]"):
            return key[len(pre):-1]
    return ""


def _split_iface_metrics(
    in_items: list[dict],
    out_items: list[dict],
    physical_keys: frozenset[str],
) -> dict[str, dict]:
    """Bucket per-host net.if.in / net.if.out items into physical vs tunnel.

    Physical = exact match on the curated physical-interface key list (or a
    kernel physical-NIC naming pattern). Tunnel = any other interface that is
    not loopback/docker. Outbound is tracked on the physical NIC only — it is
    the discriminator between a genuine forwarding failure (physical out << in)
    and a healthy NAT-mode relay that forwards through the physical NIC
    (out ≈ in) with its tunnel interfaces idle by design.

    Returns:
        {hostid: {"physical_bps": float (in), "physical_out_bps": float,
                  "tunnel_bps": float (in), "tunnel_count": int,
                  "tunnel_names": list[str]}}
    """
    per_host: dict[str, dict] = {}

    def _slot(hid: str) -> dict:
        return per_host.setdefault(
            hid,
            {"physical_bps": 0.0, "physical_out_bps": 0.0, "tunnel_bps": 0.0,
             "tunnel_count": 0, "tunnel_names": []},
        )

    def _accum(items: list[dict], direction: str) -> None:
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
            slot = _slot(hid)
            physical = key in physical_keys or bool(_PHYSICAL_NIC_RE.match(iface))
            if direction == "in":
                if physical:
                    slot["physical_bps"] += val
                else:
                    slot["tunnel_bps"] += val
                    slot["tunnel_count"] += 1
                    slot["tunnel_names"].append(iface)
            elif physical:
                slot["physical_out_bps"] += val

    _accum(in_items, "in")
    _accum(out_items, "out")
    return per_host


# A relay forwards what it receives. Only when physical outbound is below this
# fraction of physical inbound is traffic "arriving but not relayed" — a real
# forwarding failure. Healthy NAT-mode relays sit near 1.0 (out ≈ in).
_OUT_IN_RATIO = 0.1


def _find_idle_relays(
    per_host: dict[str, dict],
    min_mgmt_kbps: float,
) -> list[tuple[str, float, float, int, list[str]]]:
    """Return [(hostid, in_kbps, out_kbps, tunnel_count, sample_tunnels), ...].

    A relay is flagged as NOT forwarding only when ALL hold:
    - At least one tunnel-class interface exists (else it is not a relay).
    - Every tunnel interface reports zero bytes/sec in.
    - Physical-NIC inbound is at or above min_mgmt_kbps.
    - Physical-NIC outbound is below _OUT_IN_RATIO × inbound (receives but does
      not relay). This excludes healthy NAT-mode relays (out ≈ in) that were
      previously false-flagged by the inbound-only check.
    """
    out: list[tuple[str, float, float, int, list[str]]] = []
    for hid, data in per_host.items():
        if data["tunnel_count"] == 0:
            continue
        if data["tunnel_bps"] > 0:
            continue
        in_bps = data["physical_bps"]
        in_kbps = in_bps / 1000.0
        if in_kbps < min_mgmt_kbps:
            continue
        if in_bps <= 0 or data["physical_out_bps"] >= in_bps * _OUT_IN_RATIO:
            continue  # forwarding looks healthy (out ≈ in) — not a failure
        sample = sorted(set(data["tunnel_names"]))[:4]
        out.append((hid, in_kbps, data["physical_out_bps"] / 1000.0,
                    data["tunnel_count"], sample))
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
    when it covers ≥ min_hosts distinct **canonical** hosts — sub-hosts of
    one physical machine collapse to one entry so a multi-VIP box doesn't
    falsely satisfy the threshold (tasks.md #151, ADR 033).
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
            uniq_hosts = {canonical_host_name(r["host"]) for r in bucket}
            if len(uniq_hosts) >= min_hosts:
                clusters.append({
                    "key": key,
                    "host_count": len(uniq_hosts),
                    "hosts": sorted(uniq_hosts),
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


def subnet24(ip: str) -> str:
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
        return subnet24(ip)
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

            Flags a host only when the physical NIC receives traffic but sends
            almost none (out < 10% of in) while all tunnel interfaces read 0 —
            traffic arriving but not relayed. Healthy NAT-mode relays that
            forward through the physical NIC (out ≈ in) are excluded by the
            out-vs-in gate. See ADR 010, 015, 043.

            Args:
                min_mgmt_kbps: Floor on physical-NIC inbound throughput (default: 100)
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

                in_items = await client.call("item.get", {
                    "hostids": host_ids,
                    "output": ["hostid", "key_", "lastvalue"],
                    "search": {"key_": "net.if.in["},
                    "filter": {"status": "0"},
                })
                out_items = await client.call("item.get", {
                    "hostids": host_ids,
                    "output": ["hostid", "key_", "lastvalue"],
                    "search": {"key_": "net.if.out["},
                    "filter": {"status": "0"},
                })

                physical_keys = frozenset(TRAFFIC_IN_KEYS)
                per_host = _split_iface_metrics(in_items, out_items, physical_keys)
                idle = _find_idle_relays(per_host, float(min_mgmt_kbps))

                if not idle:
                    return (f"No forwarding failures found ({len(per_host)} hosts inspected). "
                            f"Relays with idle tunnels but balanced physical out/in are healthy "
                            f"NAT-mode and excluded.")

                shown = idle[:max_results]
                lines = [
                    f"**{len(idle)} relays not forwarding** "
                    f"(physical in ≥ {min_mgmt_kbps} kbps, out < 10% of in, tunnels at 0 bps — "
                    f"traffic arriving but not relayed)\n",
                    "| Host | IP | In kbps | Out kbps | Idle tunnels | Sample |",
                    "|------|----|---------|----------|--------------|--------|",
                ]
                for hid, in_kbps, out_kbps, tun_count, sample in shown:
                    h = host_map.get(hid, {})
                    ip = host_ip(h)
                    sample_str = ", ".join(sample) if sample else "—"
                    lines.append(
                        f"| {h.get('host', '?')} | {ip} | "
                        f"{in_kbps:.1f} | {out_kbps:.1f} | {tun_count} | {sample_str} |"
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
            max_age_hours: int = 0,
            include_suppressed: bool = False,
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
                max_age_hours: Drop problems older than this; 0 = unlimited (default: 0)
                include_suppressed: Include maintenance-suppressed problems (default: False)
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
                    "output": ["eventid", "name", "severity", "clock", "suppressed"],
                    "severities": list(range(min_severity, 6)),
                    "sortfield": "eventid",
                    "sortorder": "DESC",
                    "limit": 2000,
                    "recent": True,
                })
                problems = filter_suppressed(problems, include_suppressed)
                problems.sort(key=lambda p: -int(p.get("clock", 0)))
                if max_age_hours > 0:
                    import time as _time
                    cutoff = int(_time.time()) - max_age_hours * 3600
                    problems = [p for p in problems if int(p.get("clock", 0)) >= cutoff]
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
                # Sub-host (parent + " " + suffix) folds into its parent so a
                # single physical machine never inflates the unique-host
                # count. See ADR 022. The parent_map is built from the same
                # query — sub-hosts whose parent has no problem in this
                # window keep their own canonical id and still count once.
                parent_map = build_parent_map(list(host_meta.values()))

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
                            canonical_hid = parent_map.get(hid, hid)
                            # Group key follows the *child's* IP (sub-hosts in
                            # the same physical machine usually share a /24
                            # with the parent anyway). Display label uses the
                            # parent's hostname when available so dedup looks
                            # cohesive in the rendered output.
                            key = host_keys.get(hid, {}).get(level, "")
                            if not key:
                                continue
                            raw_name = p.get("name", "?")
                            canonical_meta = host_meta.get(canonical_hid, hm)
                            host_label = canonical_meta.get("host", hm.get("host", ""))
                            out.append({
                                "clock": int(p.get("clock", 0)),
                                "hostid": canonical_hid,
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
                import time as _time
                _now = int(_time.time())
                for idx, c in enumerate(shown, 1):
                    t0 = datetime.fromtimestamp(c["start"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                    t1 = datetime.fromtimestamp(c["end"], timezone.utc).strftime("%H:%M")
                    sev = _SEV_LABELS.get(c["max_severity"], "?")
                    sample_hosts = ", ".join(c["hosts"][:5])
                    more = f" +{len(c['hosts']) - 5}" if len(c["hosts"]) > 5 else ""
                    sample_problems = "; ".join(c["problems"][:3])
                    age = format_age(_now - c["start"])
                    lines.append(
                        f"### Cluster {idx} — {c['key']}\n"
                        f"- **Hosts:** {c['host_count']} ({sample_hosts}{more})\n"
                        f"- **When:** {t0} → {t1} (started {age} ago, {c['events']} events)\n"
                        f"- **Max severity:** {sev}\n"
                        f"- **Problems:** {sample_problems}\n"
                    )
                if len(clusters) > max_clusters:
                    lines.append(f"\n*{len(clusters) - max_clusters} more clusters omitted*")
                return "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error: {e}"
