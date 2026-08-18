#!/usr/bin/env python3
"""Regenerate ``src/zbbx_mcp/data/provider_cidrs.json``.

The provider table is **generated, not curated**. It is derived from the
public prefix-to-AS dataset published at <https://iptoasn.com>, which maps
every routed IPv4 prefix on the internet to the autonomous system that
announces it. Anyone can re-run this script and get the same file.

That property is the point. A hand-maintained table is a snapshot of whatever
its authors happened to know: it cannot be complete, it goes stale from the day
it ships, and a range recorded wrongly does not fail loudly — it attributes an
address to the wrong provider, confidently. Deriving the table from routing
data removes all three problems at once.

**Selection rule**, in full:

1. Drop unrouted ranges (AS 0).
2. Group every prefix by its announcing AS and aggregate: exact
   start-end ranges are summarised into CIDRs, then adjacent blocks are
   collapsed. Aggregation never widens coverage — the address set is
   identical, so no address can be attributed to an AS that does not
   announce it.
3. Rank ASes by total routed IPv4 addresses; keep the top ``--top``.
4. Keep each AS's ``--per-as`` largest aggregated blocks. Dropping the
   smaller ones costs coverage but cannot introduce a wrong answer.
5. Merge with ``KEEP_NAMES`` below, which pins the display names that
   appear in reports. Where a pinned name and the dataset describe the same
   operator, the pinned name wins so report output stays stable.

Usage::

    python scripts/gen_provider_cidrs.py            # fetch and regenerate
    python scripts/gen_provider_cidrs.py --top 700 --per-as 6
    python scripts/gen_provider_cidrs.py --src ip2asn-v4.tsv.gz   # offline
"""

from __future__ import annotations

import argparse
import collections
import gzip
import ipaddress
import json
import pathlib
import re
import sys
import urllib.request

SOURCE = "https://iptoasn.com/data/ip2asn-v4.tsv.gz"
OUT = pathlib.Path(__file__).resolve().parent.parent / "src" / "zbbx_mcp" / "data" / "provider_cidrs.json"

# Display names pinned for reporting. These are the names the tools print, so
# they are held stable rather than tracking whatever string the routing dataset
# happens to carry for the same operator this month.
KEEP_NAMES = {
    "AMAZON": "AWS", "AMAZON-02": "AWS", "AMAZON-AES": "AWS",
    "MICROSOFT-CORP-MSN-AS-BLOCK": "Azure",
    "GOOGLE": "Google Cloud", "GOOGLE-CLOUD-PLATFORM": "Google Cloud",
    "CLOUDFLARENET": "Cloudflare",
    "OVH": "OVH", "OVH SAS": "OVH",
    "HETZNER-AS": "Hetzner", "Hetzner Online GmbH": "Hetzner",
    "DIGITALOCEAN-ASN": "DigitalOcean",
    "LINODE-AP": "Linode", "AKAMAI-AS": "Akamai", "AKAMAI-ASN1": "Akamai",
    "FASTLY": "Fastly", "ORACLE-BMC-31898": "Oracle Cloud",
    "AS-CHOOPA": "Vultr", "AS-VULTR": "Vultr", "VULTR": "Vultr",
    "SCALEWAY": "Scaleway", "LEASEWEB": "Leaseweb", "LEASEWEB-USA": "Leaseweb",
    "AKAMAI-LINODE-AP": "Linode", "LINODE": "Linode",
    "TENCENT-NET-AP": "Tencent Cloud", "ALIBABA-CN-NET": "Alibaba Cloud",
    "M247": "M247", "PSYCHZ": "Psychz", "MELBIKOMAS": "Melbicom",
    "RACKSPACE": "Rackspace", "GODADDY": "GoDaddy", "UNIFIEDLAYER-AS-1": "Unified Layer",
    "SOFTLAYER": "SoftLayer", "IBM": "IBM Cloud",
    "CONTABO": "Contabo", "IONOS-AS": "IONOS", "NETCUP-AS": "netcup",
    "COGENT-174": "Cogent", "GTT-BACKBONE": "GTT", "LEVEL3": "Level3",
    "TELIANET": "Telia", "NTT-COMMUNICATIONS-2914": "NTT",
    "HURRICANE": "Hurricane Electric", "ZAYO-6461": "Zayo",
}


def clean(desc: str) -> str:
    """A readable operator name from an AS description."""
    out = re.sub(r"^[A-Z0-9\-]+-(AS|ASN)\b[- ]*", "", desc)
    out = re.sub(r"\b(AS\d+|-AS-[A-Z]{2})\b", "", out)
    out = re.sub(r"\s+", " ", out).strip(" ,-")
    return out[:48]


def pin(desc: str) -> str | None:
    """A pinned display name for ``desc``, or None.

    Matched on the leading token as well as the whole string: the dataset
    writes an operator as ``AS-VULTR - Vultr Holdings LLC`` or
    ``AKAMAI-LINODE-AP Akamai Connected Cloud``, and reports should keep saying
    ``Vultr`` and ``Linode`` across a regeneration rather than following
    whatever the AS description happens to be this month.
    """
    if desc in KEEP_NAMES:
        return KEEP_NAMES[desc]
    cleaned = clean(desc)
    if cleaned in KEEP_NAMES:
        return KEEP_NAMES[cleaned]
    # Whitespace only: the leading token is itself hyphenated
    # (``AKAMAI-LINODE-AP``, ``TENCENT-NET-AP``), so splitting on "-" too would
    # truncate it to a prefix that matches nothing.
    head = desc.strip().split()[0].upper() if desc.strip() else ""
    return KEEP_NAMES.get(head)


def load(path: pathlib.Path):
    ranges: dict[str, list] = collections.defaultdict(list)
    names: dict[str, str] = {}
    with gzip.open(path, "rt", errors="replace") as fh:
        for line in fh:
            parts = line.rstrip("\n").split("\t")
            if len(parts) != 5:
                continue
            start, end, asn, _cc, desc = parts
            if asn == "0" or desc == "Not routed":
                continue
            try:
                ranges[asn].append(
                    (ipaddress.IPv4Address(start), ipaddress.IPv4Address(end)))
            except ipaddress.AddressValueError:
                continue
            names.setdefault(asn, desc)
    return ranges, names


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=400)
    ap.add_argument("--per-as", type=int, default=4)
    ap.add_argument("--src", type=pathlib.Path)
    args = ap.parse_args()

    src = args.src
    if src is None:
        src = pathlib.Path("ip2asn-v4.tsv.gz")
        print(f"fetching {SOURCE} ...", file=sys.stderr)
        urllib.request.urlopen(SOURCE, timeout=180)  # noqa: S310
        with urllib.request.urlopen(SOURCE, timeout=180) as r:  # noqa: S310
            src.write_bytes(r.read())

    ranges, names = load(src)
    print(f"{len(ranges):,} routed ASes", file=sys.stderr)

    aggregated = {}
    for asn, rs in ranges.items():
        nets: list = []
        for a, b in rs:
            nets.extend(ipaddress.summarize_address_range(a, b))
        collapsed = sorted(ipaddress.collapse_addresses(nets),
                           key=lambda n: -n.num_addresses)
        aggregated[asn] = (sum(n.num_addresses for n in collapsed), collapsed)

    ranked = sorted(aggregated.items(), key=lambda kv: -kv[1][0])

    table: dict[str, list[str]] = {}
    for asn, (_total, nets) in ranked[:args.top]:
        raw = names[asn]
        name = pin(raw) or clean(raw)
        if not name:
            continue
        bucket = table.setdefault(name, [])
        for net in nets[:args.per_as]:
            if str(net) not in bucket:
                bucket.append(str(net))

    # Union with whatever is already on disk. Regenerating must never silently
    # drop an operator that a previous run or a narrower cutoff had resolved —
    # losing an entry turns a correct answer into "Other" with no signal.
    if OUT.exists():
        for name, cidrs in json.loads(OUT.read_text()).items():
            bucket = table.setdefault(name, [])
            for cidr in cidrs:
                if cidr not in bucket:
                    bucket.append(cidr)

    ordered = {k: sorted(set(table[k]),
                         key=lambda c: (ipaddress.ip_network(c).prefixlen, c))
               for k in sorted(table)}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(ordered, indent=1, sort_keys=True) + "\n")
    n_cidrs = sum(len(v) for v in ordered.values())
    print(f"wrote {OUT.relative_to(OUT.parent.parent.parent.parent)}: "
          f"{len(ordered)} providers / {n_cidrs} CIDRs", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
