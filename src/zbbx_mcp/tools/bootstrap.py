"""Draft the deployment-specific range files that ADR 120 and ADR 122 expect.

`detect_provider` and `resolve_datacenter` both answer from a built-in table
first and a configured one ahead of it. The built-in halves are generic on
purpose: the provider table is generated from public routing data, and the
datacenter table is empty. The accurate halves are per-deployment.

These tools write the first draft of those files, so a new install does not
have to assemble them by hand. Both write a **draft** rather than the live
file: reverse DNS is a hint, and a wrong name is reported as confidently as a
right one, so the output is meant to be read before it is used.
"""

from __future__ import annotations

import asyncio
import collections
import ipaddress
import json
import socket

import httpx

from zbbx_mcp.classify import detect_provider, resolve_datacenter
from zbbx_mcp.resolver import InstanceResolver
from zbbx_mcp.utils import confined_output_path

# Facility codes that commonly appear in reverse-DNS names. A starting point,
# not an authority — an unmatched block keeps a placeholder rather than a guess.
DC_CODES: dict[str, str] = {
    "gra": "Gravelines, FR", "rbx": "Roubaix, FR", "sbg": "Strasbourg, FR",
    "bhs": "Beauharnois, CA", "waw": "Warsaw, PL", "lim": "Limburg, DE",
    "eri": "Erith, GB", "hil": "Hillsboro, US", "vin": "Vint Hill, US",
    "fsn": "Falkenstein, DE", "nbg": "Nuremberg, DE", "hel": "Helsinki, FI",
    "ash": "Ashburn, US", "fra": "Frankfurt, DE", "ams": "Amsterdam, NL",
    "lon": "London, GB", "par": "Paris, FR", "sto": "Stockholm, SE",
    "mad": "Madrid, ES", "mil": "Milan, IT", "zrh": "Zurich, CH",
    "sin": "Singapore, SG", "nrt": "Tokyo, JP", "syd": "Sydney, AU",
    "gru": "Sao Paulo, BR", "iad": "Ashburn, US", "dfw": "Dallas, US",
    "lax": "Los Angeles, US", "ord": "Chicago, US", "sea": "Seattle, US",
}


def city_from_ptr(name: str) -> str:
    """A city for a reverse-DNS name, or "" when nothing matches. Pure."""
    if not name:
        return ""
    labels = name.lower().split(".")
    for code in sorted(DC_CODES, key=len, reverse=True):
        for label in labels:
            if code in label:
                return DC_CODES[code]
    return ""


def rdns_label(name: str) -> str:
    """The operator-ish tail of a reverse-DNS name, or "". Pure.

    `clients.your-server.de` names the operator better than a placeholder.
    """
    if not name:
        return ""
    parts = name.lower().split(".")
    return ".".join(parts[-3:]) if len(parts) >= 3 else ".".join(parts)


def group_blocks(pairs: list[tuple[str, str]], prefix: int,
                 unresolved_only: bool, *,
                 routable_only: bool = True) -> dict[str, list[str]]:
    """`{block: [hostname, ...]}` grouped by prefix. Pure.

    ``unresolved_only`` keeps just the addresses the current tables cannot
    name, which is what makes the draft additive rather than a rewrite.

    ``routable_only`` drops anything Python calls non-global — private space,
    loopback, and (deliberately) the RFC 5737 documentation ranges, which
    Python also reports as non-global. That last one means a caller cannot
    exercise this with the fixture addresses ADR 119 mandates, so the flag
    exists for tests. Production callers leave it on.
    """
    blocks: dict[str, list[str]] = collections.defaultdict(list)
    for host, ip in pairs:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if routable_only and not addr.is_global:
            continue
        if unresolved_only and detect_provider(ip) not in ("Other", "Unknown"):
            continue
        blocks[str(ipaddress.ip_network(f"{ip}/{prefix}", strict=False))].append(host)
    return dict(blocks)


async def _host_addresses(client) -> list[tuple[str, str]]:
    hosts = await client.call("host.get", {
        "output": ["host"], "selectInterfaces": ["ip"], "filter": {"status": "0"},
    })
    out = []
    for h in hosts:
        for iface in h.get("interfaces") or []:
            ip = (iface.get("ip") or "").strip()
            if ip and ip != "127.0.0.1":
                out.append((str(h.get("host", "")), ip))
    return out


async def _ptr_map(ips: list[str]) -> dict[str, str]:
    """One reverse lookup per address, off the event loop."""
    def _one(ip: str) -> str:
        try:
            return socket.gethostbyaddr(ip)[0]
        except (socket.herror, socket.gaierror, OSError):
            return ""
    loop = asyncio.get_running_loop()
    names = await asyncio.gather(
        *(loop.run_in_executor(None, _one, ip) for ip in ips))
    return dict(zip(ips, names, strict=True))


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()):

    if "build_provider_overrides" not in skip:

        @mcp.tool()
        async def build_provider_overrides(
            output_path: str,
            min_hosts: int = 2,
            instance: str = "",
        ) -> str:
            """Draft a ZABBIX_PROVIDER_CIDRS file from this Zabbix's own hosts.

            Groups addresses the current table cannot name into /16 blocks and
            names each from reverse DNS where a PTR exists. Review the names,
            then point ZABBIX_PROVIDER_CIDRS at the file.

            Args:
                output_path: Where to write the JSON draft
                min_hosts: Skip blocks with fewer hosts than this (default: 2)
                instance: Zabbix instance name (optional)
            """
            try:
                path = confined_output_path(output_path)
                client = resolver.resolve(instance)
                pairs = await _host_addresses(client)
                blocks = {b: h for b, h in group_blocks(pairs, 16, True).items()
                          if len(h) >= min_hosts}
                if not blocks:
                    return ("Every address already resolves to a provider — "
                            "nothing to add.")
                ordered = sorted(blocks.items(), key=lambda kv: -len(kv[1]))
                samples = [str(ipaddress.ip_network(b).network_address + 1)
                           for b, _ in ordered]
                ptrs = await _ptr_map(samples)

                draft: dict[str, list[str]] = {}
                named = 0
                for i, ((block, hosts), sample) in enumerate(
                        zip(ordered, samples, strict=True), 1):
                    label = rdns_label(ptrs.get(sample, ""))
                    if label:
                        named += 1
                    else:
                        label = (f"RENAME-ME-{i:02d} ({len(hosts)} hosts: "
                                 f"{', '.join(sorted(hosts)[:3])})")
                    draft.setdefault(label, [])
                    if block not in draft[label]:
                        draft[label].append(block)

                with open(path, "w") as fh:
                    json.dump(draft, fh, indent=1, sort_keys=True)
                total = sum(len(h) for h in blocks.values())
                return (f"Wrote {path}\n\n"
                        f"- {total} unresolved addresses in {len(blocks)} /16 blocks\n"
                        f"- {named} of {len(draft)} entries named from reverse DNS; "
                        "the rest are RENAME-ME\n\n"
                        "Check the names, then set `ZABBIX_PROVIDER_CIDRS` to this "
                        "path and reconnect. The file describes your "
                        "infrastructure — keep it out of version control.")
            except (httpx.HTTPError, ValueError, OSError) as e:
                return f"Error: {e}"

    if "build_datacenter_overrides" not in skip:

        @mcp.tool()
        async def build_datacenter_overrides(
            output_path: str,
            min_hosts: int = 2,
            instance: str = "",
        ) -> str:
            """Draft a ZABBIX_DATACENTER_CIDRS file from this Zabbix's own hosts.

            Groups addresses with no city into /24 blocks and proposes one from
            reverse DNS, which commonly carries a facility code. Blocks that
            match nothing are marked UNKNOWN for you to fill in.

            Args:
                output_path: Where to write the JSON draft
                min_hosts: Skip blocks with fewer hosts than this (default: 2)
                instance: Zabbix instance name (optional)
            """
            try:
                path = confined_output_path(output_path)
                client = resolver.resolve(instance)
                pairs = await _host_addresses(client)
                blocks = {b: h for b, h in group_blocks(pairs, 24, False).items()
                          if len(h) >= min_hosts}
                # Only blocks that have no city yet.
                blocks = {b: h for b, h in blocks.items()
                          if not resolve_datacenter(
                              str(ipaddress.ip_network(b).network_address + 1))[1]}
                if not blocks:
                    return "Every address already resolves to a city — nothing to add."
                ordered = sorted(blocks.items(), key=lambda kv: -len(kv[1]))
                samples = [str(ipaddress.ip_network(b).network_address + 1)
                           for b, _ in ordered]
                ptrs = await _ptr_map(samples)

                draft: dict[str, list[list[str]]] = collections.defaultdict(list)
                named = 0
                for (block, _hosts), sample in zip(ordered, samples, strict=True):
                    ptr = ptrs.get(sample, "")
                    city = city_from_ptr(ptr)
                    if city:
                        named += 1
                    provider = detect_provider(sample)
                    draft[provider].append(
                        [block, city or f"UNKNOWN ({ptr or 'no PTR'})"])

                with open(path, "w") as fh:
                    json.dump(dict(draft), fh, indent=1, sort_keys=True)
                total = sum(len(v) for v in draft.values())
                return (f"Wrote {path}\n\n"
                        f"- {total} /24 blocks with no city\n"
                        f"- {named} got one from reverse DNS; the rest are UNKNOWN\n\n"
                        "Reverse DNS is a hint, not a source of truth — check the "
                        "cities before use, since a wrong one is reported as "
                        "confidently as a right one. Then set "
                        "`ZABBIX_DATACENTER_CIDRS` to this path and reconnect. "
                        "The file describes your infrastructure — keep it out of "
                        "version control.")
            except (httpx.HTTPError, ValueError, OSError) as e:
                return f"Error: {e}"
