"""Shared helpers for cost-management tools (cost imports, audits, summaries).

Extracted from the former monolithic costs.py — every cost tool consumes one or
more of these helpers, so they live in a single module to avoid cross-tool
imports of private symbols.
"""

import asyncio
import csv as _csv
import os
import re
import statistics

from zbbx_mcp.data import host_ip

_IP_RE = re.compile(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b")

# Cost source provenance tags (written into {$COST_MONTH} description).
COST_SRC_BILLING_IP = "src:billing_ip"
COST_SRC_BILLING_NAME = "src:billing_name"
COST_SRC_BILLING_TRANSLATED = "src:billing_translated"
COST_SRC_BILLING_COMPOUND = "src:billing_compound"
COST_SRC_CLUSTER_EXTRAS = "src:cluster_extras"
COST_SRC_BULK_PATTERN = "src:bulk_pattern"
COST_SRC_PRODUCT_MEDIAN = "src:product_median"
COST_SRC_PROVIDER_MEDIAN = "src:provider_median"

# Matches an existing cluster-extras description so re-runs can strip the
# prior contribution instead of stacking it on top.
_CLUSTER_EXTRAS_RE = re.compile(
    r"^\s*" + re.escape(COST_SRC_CLUSTER_EXTRAS) +
    r"\s+base\s+([\d.]+)\s*\+\s*\d+\s+extra\s+IPs?\s*\(([\d.]+)\)\s*$"
)


def _strip_prior_cluster_extras(current: float, description: str) -> float:
    """Return the true base cost, stripping any prior src:cluster_extras addition.

    If description is a previous cluster_extras write, we trust the base
    recorded there (authoritative value at the time of that write).
    Otherwise fall back to current - parsed_extras, then to current.
    """
    if not description:
        return current
    m = _CLUSTER_EXTRAS_RE.match(description)
    if not m:
        return current
    try:
        prior_base = float(m.group(1))
        return prior_base
    except (ValueError, TypeError):
        try:
            prior_extras = float(m.group(2))
            return round(current - prior_extras, 2)
        except (ValueError, TypeError):
            return current


def _cluster_new_val(
    current: float,
    existing_desc: str,
    extras: float,
    overwrite_base: float = -1.0,
) -> tuple[float, float]:
    """Return (base, new_val) for a cluster_extras update.

    overwrite_base >= 0 replaces the base directly. Otherwise we strip any
    prior cluster_extras contribution so re-runs with the same extras
    converge instead of stacking.
    """
    if overwrite_base >= 0:
        base = round(float(overwrite_base), 2)
    else:
        base = _strip_prior_cluster_extras(current, existing_desc)
    return base, round(base + extras, 2)


def _dedup_name_from_ip_entries(
    ip_costs: dict,
    in_range,
    extract_price,
) -> tuple[dict, dict]:
    """Derive a name→price map from ip_costs entries of the form
    ``{"<ip>": {"name": ..., "price": ...}}``.

    Returns ``(unique, duplicates)``:

    - ``unique`` — names that appeared with a single consistent price across
      all their ip entries.
    - ``duplicates`` — names seen with two or more distinct prices. These are
      dropped from the matchable set (caller should surface them in the
      dry-run output so the sheet can be fixed upstream).

    Rationale: without dedup, a name keyed to three different prices would
    silently bind whichever row iterated first. See ADR 009.
    """
    alternates: dict[str, list[float]] = {}
    for v in ip_costs.values():
        if not isinstance(v, dict) or not in_range(v):
            continue
        name = (v.get("name") or "").strip()
        if not name:
            continue
        alternates.setdefault(name, []).append(float(extract_price(v)))

    unique: dict[str, float] = {}
    duplicates: dict[str, list[float]] = {}
    for name, prices in alternates.items():
        distinct = sorted({round(p, 2) for p in prices})
        if len(distinct) == 1:
            unique[name] = distinct[0]
        else:
            duplicates[name] = distinct
    return unique, duplicates


def _prefix_name_match(
    name_lower: str,
    name_list: list[str],
    name_to_host: dict,
) -> dict | None:
    """Bidirectional prefix match with ambiguity-skip and digit-extension guard.

    Returns the matching host dict, or None if no safe match.

    Guards:
    - "digit extension" pairs like ``srv10`` ↔ ``srv100`` are rejected — when
      names share a root and differ only by appended digits they are almost
      always distinct hosts, not a truncation/rename of one another.
    - If multiple Zabbix names satisfy the prefix relation, the match is
      ambiguous and we skip rather than pick one arbitrarily.
    """
    if len(name_lower) < 4:
        return None
    candidates = []
    for zname in name_list:
        if zname == name_lower:
            continue
        if zname.startswith(name_lower):
            rest = zname[len(name_lower):]
        elif name_lower.startswith(zname):
            rest = name_lower[len(zname):]
        else:
            continue
        if rest and rest[0].isdigit():
            continue
        candidates.append(zname)
    if len(candidates) != 1:
        return None
    return name_to_host[candidates[0]]


async def _provider_medians(client) -> dict[str, float]:
    """Compute median {$COST_MONTH} per detected provider across costed hosts."""
    from zbbx_mcp.classify import detect_provider

    hosts, macros = await asyncio.gather(
        client.call("host.get", {
            "output": ["hostid", "host"],
            "selectInterfaces": ["ip"],
            "filter": {"status": "0"},
        }),
        client.call("usermacro.get", {
            "output": ["hostid", "value"],
            "filter": {"macro": "{$COST_MONTH}"},
        }),
    )
    costs: dict[str, float] = {}
    for m in macros:
        try:
            v = float(m.get("value") or 0)
            if v > 0:
                costs[m["hostid"]] = v
        except (ValueError, TypeError):
            pass
    bucket: dict[str, list[float]] = {}
    for h in hosts:
        if h["hostid"] not in costs:
            continue
        ip = host_ip(h)
        if not ip:
            continue
        prov = detect_provider(ip)
        bucket.setdefault(prov, []).append(costs[h["hostid"]])
    return {k: statistics.median(v) for k, v in bucket.items() if v}


def _sanity_warnings(
    matches: list[tuple],
    ip_to_host: dict,
    medians: dict[str, float],
    high_factor: float = 2.0,
    low_factor: float = 0.3,
) -> list[str]:
    """Return human-readable warnings for costs far from provider median."""
    from zbbx_mcp.classify import detect_provider

    warnings = []
    # matches: (hostname, hid, ip, cost, source)
    for name, _hid, ip, cost, _src in matches:
        if not ip:
            continue
        prov = detect_provider(ip)
        med = medians.get(prov)
        if not med:
            continue
        if cost >= med * high_factor:
            warnings.append(
                f"{name}: ${cost:.2f} is {cost / med:.1f}× {prov} median ${med:.2f}"
            )
        elif cost <= med * low_factor:
            warnings.append(
                f"{name}: ${cost:.2f} is {cost / med:.1f}× {prov} median ${med:.2f} (low)"
            )
    return warnings


def _load_billing_csv(path: str) -> list[dict]:
    """Read billing CSV with ip + billing_name + price_monthly columns.

    Tolerates column aliases (ip/ipaddress, name/billing_name/hostname,
    price/price_monthly/cost). Returns list of {ip, name, price} dicts.
    Skips invalid/reserved IPs and zero/negative prices.
    """
    rows: list[dict] = []
    with open(os.path.expanduser(path)) as f:
        reader = _csv.DictReader(f)
        for raw in reader:
            # Normalize headers
            norm = {k.strip().lower(): (v or "").strip() for k, v in raw.items() if k}
            ip = norm.get("ip") or norm.get("ipaddress") or norm.get("ip_address") or ""
            name = norm.get("billing_name") or norm.get("name") or norm.get("hostname") or ""
            price_raw = (
                norm.get("price_monthly") or norm.get("price")
                or norm.get("cost") or norm.get("cost_month") or ""
            )
            try:
                price = float(price_raw)
            except (ValueError, TypeError):
                continue
            if price <= 0 or not ip:
                continue
            # Skip reserved / bogus IPs
            octets = ip.split(".")
            if len(octets) != 4:
                continue
            try:
                a, b = int(octets[0]), int(octets[1])
            except ValueError:
                continue
            if a in (0, 10, 127, 169, 172, 192, 224, 255) and (a != 172 or 16 <= b <= 31):
                # Allow most, skip only clearly-reserved
                if a in (0, 127, 224, 255):
                    continue
            rows.append({"ip": ip, "name": name, "price": price})
    return rows
