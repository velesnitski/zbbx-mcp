"""The tool owns its output budget (ADR 142).

The server truncates every response at ``ZABBIX_RESPONSE_BUDGET`` characters.
A cut made there is invisible to both sides: the tool has already returned,
so it cannot say what fell off, and the caller sees a table that simply
stops. A cut the tool makes itself is a fact it can state — which hosts are
missing and how to ask for exactly those.

This module is the one place the budget is read (``response_budget``), the
whole-host fitter the trend tools render through (``fit_host_blocks``), and
the shared host-list parsing that the ``hosts=`` parameter needs to mean the
same thing on every tool that carries it.

Pure: no I/O beyond one environment read, no client, importable from any tool.
"""

from __future__ import annotations

import os
from collections.abc import Iterable

from zbbx_mcp.classify import classify_host

#: The server's default when ``ZABBIX_RESPONSE_BUDGET`` is unset.
DEFAULT_RESPONSE_BUDGET = 6000

#: Characters a tool leaves unused below the server budget. The server only
#: appends its ``[truncated N chars]`` marker once a response is already over
#: budget, so a tool that stops inside ``render_budget()`` is never cut; the
#: headroom is the margin that keeps a later, slightly larger server wrapper
#: from eating into a response the tool believed fitted.
RENDER_HEADROOM = 200

#: How many omitted host names the closing line spells out. Enough to paste
#: straight back into ``hosts=``; beyond that the count says "narrow the filter".
OMITTED_NAMES_MAX = 12

#: How many distinct product / tier labels a no-match message lists.
LABELS_MAX = 10


def response_budget() -> int:
    """The response budget the server enforces, from the same setting.

    ``0`` or a negative value disables truncation, exactly as the server
    reads it. An unparsable value falls back to the default rather than
    raising, because a misconfigured budget must not take every tool down.
    """
    raw = os.environ.get("ZABBIX_RESPONSE_BUDGET", "")
    if not raw.strip():
        return DEFAULT_RESPONSE_BUDGET
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_RESPONSE_BUDGET


def render_budget() -> int:
    """Characters a tool may render before the server would cut it.

    ``0`` means unlimited (the server has truncation disabled). Never less
    than the headroom itself, so a tiny configured budget still renders the
    header and the closing line rather than nothing.
    """
    budget = response_budget()
    if budget <= 0:
        return 0
    return max(budget - RENDER_HEADROOM, RENDER_HEADROOM)


def omitted_hosts_line(
    omitted: list[str], total: int, max_names: int = OMITTED_NAMES_MAX,
) -> str:
    """The explicit closing line naming what a budget cut left out.

    ``N of M hosts not shown: a, b, c[, +K more]. Request them with
    hosts=a,b,c or narrow the filter.`` — the named list is exactly what the
    caller pastes back, so the follow-up call cannot mis-target.
    """
    if not omitted:
        return ""
    shown = omitted[:max(1, max_names)]
    names = ", ".join(shown)
    if len(omitted) > len(shown):
        names += f", +{len(omitted) - len(shown)} more"
    return (
        f"{len(omitted)} of {total} hosts not shown: {names}. "
        f"Request them with hosts={','.join(shown)} or narrow the filter."
    )


def fit_host_blocks(
    header: str,
    blocks: list[tuple[str, str]],
    budget: int,
) -> tuple[str, list[str]]:
    """Render whole host blocks, in order, until ``budget`` would be exceeded.

    ``blocks`` is ``[(host_name, rendered_lines)]`` — every line of one host in
    one string, so a host is shown with all its metrics or not at all. A
    block is kept only if it fits *together with* the closing line that names
    the hosts after it, which guarantees the closing line itself always fits.
    ``budget <= 0`` means unlimited.

    Returns ``(text, omitted_host_names)``; ``omitted`` is empty when every
    block fitted, and then the text carries no closing line.
    """
    total = len(blocks)
    everything = len(header) + sum(1 + len(text) for _, text in blocks)
    if budget <= 0 or everything <= budget:
        return "\n".join([header, *(text for _, text in blocks)]), []

    kept: list[str] = []
    used = len(header)
    for i, (_, text) in enumerate(blocks):
        rest = [name for name, _ in blocks[i + 1:]]
        footer = omitted_hosts_line(rest, total)
        need = used + 1 + len(text) + (2 + len(footer) if footer else 0)
        if need > budget:
            break
        kept.append(text)
        used += 1 + len(text)

    omitted = [name for name, _ in blocks[len(kept):]]
    text = "\n".join([header, *kept])
    if omitted:
        # Once a block has been kept the closing line is known to fit; when
        # nothing fits at all, name fewer hosts rather than let the server
        # cut the one line that says what is missing.
        for max_names in range(OMITTED_NAMES_MAX, 0, -1):
            footer = omitted_hosts_line(omitted, total, max_names)
            if len(text) + 2 + len(footer) <= budget or max_names == 1:
                break
        text += "\n\n" + footer
    return text, omitted


# --- hosts= : a named set is a set, not a filter (v1.16.64, ADR 142) --------


def parse_host_list(hosts: str) -> list[str]:
    """``"a, b,,c"`` → ``["a", "b", "c"]``: exact names, order kept, no repeats."""
    seen: list[str] = []
    for raw in (hosts or "").split(","):
        name = raw.strip()
        if name and name not in seen:
            seen.append(name)
    return seen


def missing_hosts_note(wanted: list[str], known: Iterable[str]) -> tuple[list[str], str]:
    """Which requested names are not enabled hosts, and the note that says so.

    Returns ``(missing_sorted, note)``; the note is ``""`` when nothing is
    missing and otherwise ends in a newline so it can prefix a table.
    """
    missing = sorted(set(wanted) - set(known))
    if not missing:
        return [], ""
    note = (
        f"_{len(missing)} of {len(wanted)} requested host(s) are not enabled "
        f"hosts in Zabbix: {', '.join(missing)}_\n"
    )
    return missing, note


def all_names_unknown_message(note: str) -> str:
    """When every requested name is unknown, the answer is about the names."""
    return note.strip("_\n") + "."


def effective_cap(max_results: int, wanted: list[str]) -> int:
    """A caller who names the hosts has chosen the set; a cap cannot trim it."""
    return max(max_results, len(wanted))


# --- an empty match must say what exists ---------------------------------------


def _label_list(labels: set[str]) -> str:
    ordered = sorted(labels)
    shown = ordered[:LABELS_MAX]
    text = ", ".join(shown) if shown else "none"
    if len(ordered) > len(shown):
        text += f", +{len(ordered) - len(shown)} more"
    return text


def no_match_message(hosts: list[dict]) -> str:
    """``No servers match the filters.`` plus what the enabled hosts do carry.

    Lists the distinct product and tier labels present (sorted, up to
    ``LABELS_MAX`` each), so a caller can tell a misspelled label from a set
    that is genuinely empty. Filters are exact (ADR 137/140), which is what
    makes the listing actionable: a label shown here matches as written.
    """
    if not hosts:
        return "No servers match the filters: no enabled hosts in scope."
    products: set[str] = set()
    tiers: set[str] = set()
    for h in hosts:
        prod, tier = classify_host(h.get("groups", []))
        if prod:
            products.add(prod)
        if tier:
            tiers.add(tier)
    return (
        "No servers match the filters. "
        f"Among {len(hosts)} enabled hosts, product labels present: "
        f"{_label_list(products)}; tier labels present: {_label_list(tiers)}. "
        "Filters match a label exactly."
    )
