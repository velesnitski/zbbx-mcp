#!/usr/bin/env python3
"""Draft a `ZABBIX_DATACENTER_CIDRS` file from your own Zabbix.

`resolve_datacenter` maps an address to `(provider, city)`. The city half is
supplied per deployment (ADR 122), and this writes the first draft of it.

How it works: many hosting providers encode the facility in the reverse-DNS
name — `…gra…` for Gravelines, `…fsn…` for Falkenstein, `…nbg…` for Nuremberg.
The script reads every host address, groups them into `/24` blocks, looks up
one PTR per block, and proposes a city when the PTR contains a known code.
Blocks whose PTR matches nothing keep a placeholder for you to fill in.

The code table below is a starting point, not an authority. A wrong guess names
the wrong city just as confidently as a right one, so **check the output before
using it** — that review is the point of emitting a draft rather than writing
the live file.

**The output describes your infrastructure. Keep it out of version control.**

Usage::

    export ZABBIX_URL=... ZABBIX_TOKEN=...
    python scripts/bootstrap_datacenter_ranges.py -o ~/zbbx-datacenter-cidrs.json
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import ipaddress
import json
import os
import pathlib
import socket
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from zbbx_mcp.classify import detect_provider  # noqa: E402
from zbbx_mcp.client import ZabbixClient  # noqa: E402

# Facility codes that commonly appear in reverse-DNS names. Substring match on
# a dot-separated label, longest first so `sbg` does not shadow `sbg5`.
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


async def collect(url: str, token: str) -> list[str]:
    client = ZabbixClient(url, token)
    try:
        hosts = await client.call("host.get", {
            "output": ["host"], "selectInterfaces": ["ip"],
            "filter": {"status": 0},
        })
    finally:
        close = getattr(client, "close", None)
        if close:
            await close()
    ips = []
    for h in hosts:
        for iface in h.get("interfaces") or []:
            ip = (iface.get("ip") or "").strip()
            if ip:
                ips.append(ip)
    return ips


def ptr(ip: str) -> str:
    try:
        return socket.gethostbyaddr(ip)[0].lower()
    except (socket.herror, socket.gaierror, OSError):
        return ""


def city_from_ptr(name: str) -> str:
    """A city for a reverse-DNS name, or "" when nothing matches."""
    labels = name.split(".")
    for code in sorted(DC_CODES, key=len, reverse=True):
        for label in labels:
            if code in label:
                return DC_CODES[code]
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", type=pathlib.Path, required=True,
                    help="where to write the draft (keep it out of the repo)")
    ap.add_argument("--min-hosts", type=int, default=2,
                    help="skip blocks with fewer hosts than this (default 2)")
    args = ap.parse_args()

    url, token = os.environ.get("ZABBIX_URL"), os.environ.get("ZABBIX_TOKEN")
    if not url or not token:
        print("ZABBIX_URL and ZABBIX_TOKEN must be set", file=sys.stderr)
        return 2
    if args.out.exists():
        print(f"{args.out} exists — refusing to overwrite", file=sys.stderr)
        return 1

    blocks: dict[str, list[str]] = collections.defaultdict(list)
    for ip in asyncio.run(collect(url, token)):
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if not addr.is_global:
            continue
        blocks[str(ipaddress.ip_network(f"{ip}/24", strict=False))].append(ip)

    draft: dict[str, list[list[str]]] = collections.defaultdict(list)
    named = 0
    for block, ips in sorted(blocks.items(), key=lambda kv: -len(kv[1])):
        if len(ips) < args.min_hosts:
            continue
        name = ptr(ips[0])
        city = city_from_ptr(name)
        if city:
            named += 1
        provider = detect_provider(ips[0])
        draft[provider].append([block, city or f"UNKNOWN ({name or 'no PTR'})"])

    args.out.write_text(json.dumps(draft, indent=1, sort_keys=True) + "\n")
    total = sum(len(v) for v in draft.values())
    print(f"{len(blocks)} /24 blocks seen; {total} written to {args.out}")
    print(f"{named} of {total} got a city from reverse DNS; the rest are marked UNKNOWN")
    print("\nCheck the cities before use — a wrong one is reported as confidently")
    print("as a right one. Then:")
    print(f"  export ZABBIX_DATACENTER_CIDRS={args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
