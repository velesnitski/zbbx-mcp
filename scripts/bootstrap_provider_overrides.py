#!/usr/bin/env python3
"""Draft a `ZABBIX_PROVIDER_CIDRS` override file from your own Zabbix.

**Why this exists.** The built-in table is generated from public routing data
by taking the largest blocks of the largest autonomous systems. That makes it
reproducible and free of any curation — but measured against a real fleet it
resolves very little, because a fleet sits on specific blocks that are rarely
among any AS's biggest, and often on mid-tier providers that never make the
cut. Raising the cut-off does not fix it: multiplying the table size several
times over moves coverage by a few percent.

Accurate provider detection therefore comes from the operator, not the package
(ADR 120). This script writes the first draft of that file:

1. reads every host's address from your Zabbix,
2. keeps the ones the current table cannot resolve,
3. groups them into `/16` blocks, largest first,
4. emits JSON with a placeholder name per block, and the hostnames that fall
   in it as a hint for what to call it.

You then replace each placeholder with the provider's name — that part needs a
human, because only you can look up who announces the block.

**Relationship to the `identify_providers` tool.** That tool answers the same
question interactively and is the better choice when you just want to look: it
prints a table of unresolved prefixes with a reverse-DNS hint per prefix. This
script exists for the one thing the tool does not do — write the file. It uses
the same reverse-DNS approach, so the two should agree.

(The lookup helper is currently duplicated between them. Worth extracting into
a shared module the next time either is touched.)

**The output describes your infrastructure. Keep it out of version control.**

Usage::

    export ZABBIX_URL=... ZABBIX_TOKEN=...
    python scripts/bootstrap_provider_overrides.py -o ~/zbbx-provider-cidrs.json
    python scripts/bootstrap_provider_overrides.py -o ~/x.json --wire   # + config
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


async def collect(url: str, token: str) -> list[tuple[str, str]]:
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
    out = []
    for h in hosts:
        for iface in h.get("interfaces") or []:
            ip = (iface.get("ip") or "").strip()
            if ip:
                out.append((str(h.get("host", "")), ip))
    return out


def rdns_label(ip: str) -> str:
    """The registrable-ish tail of a reverse-DNS name, or "".

    `clients.your-server.de` and `clients.gthost.com` name the operator far
    better than a placeholder does, and this is exactly what the
    `identify_providers` tool surfaces. A prefix with no PTR simply keeps its
    placeholder — an unnamed entry is still a usable range.
    """
    try:
        parts = socket.gethostbyaddr(ip)[0].split(".")
    except (socket.herror, socket.gaierror, OSError):
        return ""
    return ".".join(parts[-3:]) if len(parts) >= 3 else ".".join(parts)


def unresolved(pairs: list[tuple[str, str]]) -> dict[str, list[str]]:
    """`/16` block → hostnames, for addresses the current table cannot name."""
    blocks: dict[str, list[str]] = collections.defaultdict(list)
    for host, ip in pairs:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if not addr.is_global:
            continue
        if detect_provider(ip) not in ("Other", "Unknown"):
            continue
        net = ipaddress.ip_network(f"{ip}/16", strict=False)
        blocks[str(net)].append(host)
    return blocks


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", type=pathlib.Path, required=True,
                    help="where to write the draft (keep it out of the repo)")
    ap.add_argument("--min-hosts", type=int, default=2,
                    help="skip blocks with fewer hosts than this (default 2)")
    ap.add_argument("--wire", action="store_true",
                    help="also point every zabbix entry in ~/.claude.json at "
                         "the file (a timestamped backup is written first)")
    args = ap.parse_args()

    url, token = os.environ.get("ZABBIX_URL"), os.environ.get("ZABBIX_TOKEN")
    if not url or not token:
        print("ZABBIX_URL and ZABBIX_TOKEN must be set", file=sys.stderr)
        return 2

    pairs = asyncio.run(collect(url, token))
    blocks = unresolved(pairs)
    kept = {b: hs for b, hs in blocks.items() if len(hs) >= args.min_hosts}
    ordered = sorted(kept.items(), key=lambda kv: -len(kv[1]))

    draft: dict[str, list[str]] = {}
    for i, (block, hosts) in enumerate(ordered, 1):
        sample = str(ipaddress.ip_network(block).network_address + 1)
        name = rdns_label(sample)
        # A PTR names the operator; without one, the placeholder carries the
        # hostnames so you can still tell which block is which.
        label = name or f"RENAME-ME-{i:02d} ({len(hosts)} hosts: {', '.join(sorted(hosts)[:3])})"
        draft.setdefault(label, [])
        if block not in draft[label]:
            draft[label].append(block)

    if args.out.exists():
        print(f"{args.out} exists — refusing to overwrite", file=sys.stderr)
        return 1
    args.out.write_text(json.dumps(draft, indent=1, sort_keys=True) + "\n")

    total = sum(len(h) for h in kept.values())
    print(f"{len(pairs)} addresses read; {total} unresolved across {len(kept)} /16 blocks")
    print(f"draft written to {args.out}")
    named = sum(1 for k in draft if not k.startswith("RENAME-ME-"))
    print(f"{named} of {len(draft)} entries named from reverse DNS")
    print("\nNext: check the names, replace any RENAME-ME-* key, then")
    print(f'  export ZABBIX_PROVIDER_CIDRS={args.out}')

    if args.wire:
        wire(args.out)
    return 0


def wire(target: pathlib.Path) -> None:
    """Point every zabbix server entry in ~/.claude.json at ``target``."""
    import datetime
    cfg = pathlib.Path.home() / ".claude.json"
    if not cfg.exists():
        print(f"{cfg} not found — set ZABBIX_PROVIDER_CIDRS by hand", file=sys.stderr)
        return
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")  # noqa: DTZ005
    backup = cfg.with_suffix(f".json.bak-{stamp}")
    backup.write_text(cfg.read_text())
    data = json.loads(cfg.read_text())
    touched = []

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if (("zabbix" in k.lower() or "zbbx" in k.lower())
                        and isinstance(v, dict) and isinstance(v.get("env"), dict)):
                    v["env"]["ZABBIX_PROVIDER_CIDRS"] = str(target)
                    touched.append(k)
                walk(v)

    walk(data)
    cfg.write_text(json.dumps(data, indent=2) + "\n")
    print(f"\nwired {len(touched)} entr(ies) in {cfg} (backup: {backup.name})")
    for t in touched:
        print("  ", t)
    print("reconnect /mcp to pick it up")


if __name__ == "__main__":
    raise SystemExit(main())
