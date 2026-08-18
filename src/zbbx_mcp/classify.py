"""Host classification (product/tier) and hosting provider detection.

Standalone module with no dependencies on tools/ to avoid circular imports.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re

_log = logging.getLogger(__name__)

__all__ = [
    "classify_host", "detect_provider", "resolve_datacenter", "PROVIDER_CIDRS",
    "is_test_host",
]


# --- Test/staging host detection (ADR 080) -------------------------------
# A fleet's non-production boxes are identifiable two ways, and BOTH must be
# honoured because neither is reliable alone:
#
#   * the host name  (`<x>-test-<y>`, `test-<x>`, `<x>-test`)
#   * a group name   (`tier_test`, `edge_test`, `PROD test hosts`)
#
# Group membership alone is not enough: a test box is routinely left sitting in
# a *production* group, so a group check would miss it entirely. Name alone is
# not enough either: a box may be correctly grouped but blandly named. The rule
# is therefore the union of the two, with the *same* pattern applied to each.
#
# Token-bounded on purpose — a bare "test" substring would also swallow
# `latest`, `contest`, `fastest`, `attestation`. Separators include whitespace
# (group names use spaces) and dots (FQDN-style names), and the token accepts
# trailing digits: numbered test boxes (`x-test2-y`) are still test boxes.
# Without those two, `x-test2-y` and `a.test.b` passed as production here
# while the sibling reporting pipeline excluded them — a determinism split
# (ADR 081). Override with ZABBIX_TEST_NAME_RE.
_TEST_RE_DEFAULT = r"(?:^|[-_.\s])test\d*(?:[-_.\s]|$)"
_TEST_RE: re.Pattern[str] | None = None


def _test_re() -> re.Pattern[str]:
    global _TEST_RE
    if _TEST_RE is None:
        raw = os.environ.get("ZABBIX_TEST_NAME_RE", "").strip() or _TEST_RE_DEFAULT
        try:
            _TEST_RE = re.compile(raw, re.IGNORECASE)
        except re.error:
            _TEST_RE = re.compile(_TEST_RE_DEFAULT, re.IGNORECASE)
    return _TEST_RE


def is_test_host(host: dict) -> bool:
    """True if ``host`` is a test/staging box rather than production.

    Checks the host name **and** every group name against the same pattern
    (see above for why both are needed). ``host`` is a Zabbix host record;
    groups may be ``[{"name": ...}]`` or plain strings. Pure.
    """
    rx = _test_re()
    name = str(host.get("host") or host.get("name") or "")
    if name and rx.search(name):
        return True
    for g in host.get("groups") or []:
        gname = g.get("name", "") if isinstance(g, dict) else str(g)
        if gname and rx.search(gname):
            return True
    return False


_SKIP_GROUPS = {
    "Templates", "Templates/Applications", "Templates/Databases",
    "Discovered hosts",
}

_PRODUCT_MAP: dict[str, tuple] | None = None


def _load_product_map() -> dict[str, tuple]:
    """Load product map from ZABBIX_PRODUCT_MAP env var."""
    raw = os.environ.get("ZABBIX_PRODUCT_MAP", "")
    if not raw:
        return {}

    try:
        if os.path.isfile(raw):
            if not raw.endswith(".json"):
                return {}
            with open(raw) as f:
                data = json.load(f)
        else:
            data = json.loads(raw)

        result = {}
        for group, mapping in data.items():
            if mapping in (["skip"], [None, None]):
                result[group] = (None, None)
            else:
                result[group] = (mapping[0], mapping[1])
        return result
    except (json.JSONDecodeError, IndexError, TypeError):
        return {}


def get_product_map() -> dict[str, tuple]:
    """Lazy-load product map on first use."""
    global _PRODUCT_MAP
    if _PRODUCT_MAP is None:
        _PRODUCT_MAP = _load_product_map()
    return _PRODUCT_MAP


# A host whose groups classify to one of these is not making a product claim —
# it is either infrastructure or unclassified. Only such a host is eligible for
# the template fallback below; a real product group always wins.
NON_SERVING_PRODUCTS = frozenset({"infrastructure", "monitoring", "unknown"})

_TEMPLATE_PRODUCT_MAP: dict[str, str] | None = None


def get_template_product_map() -> dict[str, str]:
    """``{template name: product-group name}`` from the environment.

    Empty by default, which disables template-fallback classification
    entirely — including the extra ``selectParentTemplates`` on the host
    fetch, so an unconfigured deployment pays nothing for it.

    This is configuration rather than a hardcoded table on purpose. The
    sibling reporting pipeline can hardcode its own allow-list because it is
    private and single-tenant; this server is neither, so which template
    implies which product belongs to the operator, next to the product map
    (ADR 115).

    Accepts JSON (``{"tpl": "group"}``) or ``tpl:group,tpl2:group2``.
    """
    global _TEMPLATE_PRODUCT_MAP
    if _TEMPLATE_PRODUCT_MAP is not None:
        return _TEMPLATE_PRODUCT_MAP
    raw = os.environ.get("ZABBIX_TEMPLATE_PRODUCT_MAP", "").strip()
    out: dict[str, str] = {}
    if raw:
        try:
            if raw.startswith("{"):
                out = {str(k): str(v)
                       for k, v in json.loads(raw).items() if k and v}
            else:
                for pair in raw.split(","):
                    if ":" in pair:
                        k, v = pair.split(":", 1)
                        if k.strip() and v.strip():
                            out[k.strip()] = v.strip()
        except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
            out = {}
    _TEMPLATE_PRODUCT_MAP = out
    return out


def template_product_group(
    groups: list[dict], templates: list[dict]
) -> str | None:
    """The product group a host's TEMPLATE implies, or None.

    Returns a group name only when the host's groups do not already answer.
    A template is the deploy's own statement of what a machine runs, which is
    the one signal that separates families sharing a mixed group — but it is
    weaker evidence than an explicit product group, so groups win whenever
    they classify to anything serving (ADR 115).
    """
    tmap = get_template_product_map()
    if not tmap:
        return None
    product, _tier = classify_host(groups)
    if (product or "").lower() not in NON_SERVING_PRODUCTS:
        return None
    for t in templates or []:
        mapped = tmap.get(str(t.get("name", "")))
        if mapped:
            return mapped
    return None


def classify_host(groups: list[dict]) -> tuple[str, str]:
    """Classify a host into (product, tier) based on its groups.

    If ZABBIX_PRODUCT_MAP is configured, uses explicit mapping.
    Otherwise, uses the first non-skip group name as both product and tier.
    """
    pmap = get_product_map()
    if pmap:
        for g in groups:
            gname = g.get("name", "")
            if gname in pmap:
                product, tier = pmap[gname]
                if product:
                    return product, tier
        return "Unknown", "Unknown"

    for g in groups:
        gname = g.get("name", "")
        if gname and gname not in _SKIP_GROUPS:
            return gname, "Default"
    return "Unknown", "Unknown"


def unmapped_group_counts(
    group_sets: list[list[str]],
    pmap: dict[str, tuple],
) -> list[tuple[str, int]]:
    """Count which group names leave hosts unclassified (ADR 058).

    ``group_sets`` is one list of group names per Unknown-classified host.
    Returns ``(group_name, host_count)`` sorted by count desc, name asc —
    excluding names already in ``pmap`` (mapped or explicitly skipped:
    those are intentional, not gaps). Hosts with no groups at all are
    counted under ``"(no groups)"``. Pure helper.
    """
    counts: dict[str, int] = {}
    for names in group_sets:
        gaps = [n for n in names if n and n not in pmap]
        if not gaps and not names:
            gaps = ["(no groups)"]
        for n in gaps:
            counts[n] = counts.get(n, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))



# --- Provider allocation table ------------------------------------------
# GENERATED, not curated. `data/provider_cidrs.json` is derived from the public
# prefix-to-AS dataset at <https://iptoasn.com> by `scripts/gen_provider_cidrs.py`,
# which anyone can re-run to reproduce it byte for byte.
#
# A hand-maintained table is a snapshot of whatever its authors happened to
# know: it cannot be complete, it goes stale from the day it ships, and a range
# recorded wrongly does not fail loudly — it attributes an address to the wrong
# provider, confidently, in output someone acts on. Deriving it from routing
# data removes all three problems. Deployments with better information still
# override it entirely via ZABBIX_PROVIDER_CIDRS (ADR 120).
_DATA_FILE = os.path.join(os.path.dirname(__file__), "data", "provider_cidrs.json")


def _load_provider_cidrs() -> dict[str, list[str]]:
    try:
        with open(_DATA_FILE, encoding="utf-8") as fh:
            return {str(k): [str(c) for c in v] for k, v in json.load(fh).items()}
    except (OSError, json.JSONDecodeError, TypeError, AttributeError):
        # Packaged data is missing or unreadable — a build defect. Degrade to
        # "Other" for everything rather than taking the server down, but say so:
        # silently resolving every address as unrecognised looks identical to a
        # deployment that genuinely uses no known provider.
        _log.warning("provider table unreadable at %s — detect_provider will "
                     "answer 'Other' until it is restored", _DATA_FILE)
        return {}


PROVIDER_CIDRS: dict[str, list[str]] = _load_provider_cidrs()

# Pre-compiled network objects sorted by prefix length (most specific first).
# ipaddress.ip_network() returns IPv4Network | IPv6Network; in practice we
# only feed IPv4 CIDRs but the annotation matches the call's actual return.
_IpNet = ipaddress.IPv4Network | ipaddress.IPv6Network
_PROVIDER_NETS: list[tuple[str, _IpNet]] = sorted(
    [
        (prov, ipaddress.ip_network(cidr, strict=False))
        for prov, cidrs in PROVIDER_CIDRS.items()
        for cidr in cidrs
    ],
    key=lambda x: -x[1].prefixlen,
)


_EXTRA_PROVIDER_NETS: list[tuple[str, _IpNet]] | None = None


def get_extra_provider_nets() -> list[tuple[str, _IpNet]]:
    """Operator-supplied provider ranges, most specific first.

    ``ZABBIX_PROVIDER_CIDRS`` holds a JSON object — a file path or inline —
    of ``{"Provider": ["a.b.c.d/n", ...]}``. Entries are searched *before* the
    built-in table, so a more precise local mapping wins, and unparseable
    input yields nothing rather than a partial merge.

    The built-in table is hand-maintained and cannot be complete: there is no
    registry of every hosting provider, allocations move between them, and a
    range recorded wrongly does not fail loudly — it attributes an address to
    the wrong provider, confidently, in output someone acts on.

    A deployment always has better information than the package does. It knows
    its own address space exactly and can point at an authoritative dataset,
    so the specific half of this mapping belongs in its configuration and the
    built-in half stays a generic default (ADR 120).
    """
    global _EXTRA_PROVIDER_NETS
    if _EXTRA_PROVIDER_NETS is not None:
        return _EXTRA_PROVIDER_NETS
    raw = os.environ.get("ZABBIX_PROVIDER_CIDRS", "").strip()
    nets: list[tuple[str, _IpNet]] = []
    if raw:
        try:
            if os.path.isfile(raw):
                with open(raw) as fh:
                    data = json.load(fh)
            else:
                data = json.loads(raw)
            for prov, cidrs in dict(data).items():
                for cidr in cidrs:
                    nets.append(
                        (str(prov), ipaddress.ip_network(str(cidr), strict=False))
                    )
        except (json.JSONDecodeError, OSError, TypeError, ValueError, AttributeError):
            nets = []          # unusable config disables the override entirely
    nets.sort(key=lambda x: -x[1].prefixlen)
    _EXTRA_PROVIDER_NETS = nets
    return nets


def detect_provider(ip_str: str) -> str:
    """Detect hosting provider from IP address using known CIDR ranges.

    Operator-supplied ranges are consulted first, then the built-in table.
    """
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return "Unknown"
    for provider, network in get_extra_provider_nets():
        if addr in network:
            return provider
    for provider, network in _PROVIDER_NETS:
        if addr in network:
            return provider
    return "Other"


# Maps specific CIDR ranges to datacenter cities. More specific ranges
# override broader provider ranges. Built from provider allocation docs.

# Datacenter city ranges. Empty by default: this mapping is deployment
# specific, so it is supplied through ZABBIX_DATACENTER_CIDRS rather than
# compiled in (ADR 122).
DATACENTER_CIDRS: dict[str, list[tuple[str, str]]] = {}

# Pre-compile datacenter networks (most specific first)
_DC_NETS: list[tuple[str, str, _IpNet]] = sorted(
    [
        (prov, city, ipaddress.ip_network(cidr, strict=False))
        for prov, mappings in DATACENTER_CIDRS.items()
        for cidr, city in mappings
    ],
    key=lambda x: -x[2].prefixlen,
)


def resolve_datacenter(ip_str: str) -> tuple[str, str]:
    """Resolve IP to (provider, datacenter_city). Returns ("Unknown", "") on failure."""
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return "Unknown", ""
    # Try specific datacenter mapping first
    for provider, city, network in _DC_NETS:
        if addr in network:
            return provider, city
    # Fall back to provider-only detection
    for provider, network in _PROVIDER_NETS:
        if addr in network:
            return provider, ""
    return "Other", ""
