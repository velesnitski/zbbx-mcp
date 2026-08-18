"""Diff a sibling reporting pipeline's published facts against live Zabbix.

Two independently-maintained systems run overlapping analytics against the
*same* Zabbix instance, with findings hand-ported one way. Nothing verified
they agree, so a divergence could sit in a scheduled report indefinitely: two
authoritative-looking numbers in two documents is a worse failure than one
number that is obviously wrong.

The reporting side publishes its comparable figures to a JSON file precisely so
this side is a **diff rather than a re-derivation** (its ADR 0057). This module
is the other half.

The load-bearing design choice is **what NOT to compare.** A naive version
would diff every field it recognises and would immediately cry wolf, because
some quantities carry the same name on both sides and are computed to different
definitions on purpose — most notably uptime/SLA, which is period-integrated
there and point-in-time here (ADR 097). An invariant that fires on correct data
is an invariant nobody reads, which is the failure mode this whole exercise
exists to prevent. So each field is classified:

- **strict** — provably identical definitions (host population and country
  resolution, both derived from the same host list with the same helpers).
  A mismatch here is a real defect on one side.
- **advisory** — same subject, definitions not guaranteed to match, or the
  thresholds that produced the number are not published. Reported side by side
  and explicitly NOT judged.

Drift is separated from divergence: the fleet legitimately changes between the
report's run and ours, so a small delta on a count is `DRIFT`, not a defect.
Because the facts file carries no timestamp, its age is taken from the file
mtime and always stated — an undated snapshot cannot support a strict verdict.
"""

from __future__ import annotations

import json
import os
import time as _time
from dataclasses import dataclass

import httpx

from zbbx_mcp.classify import classify_host as _classify_host
from zbbx_mcp.data import extract_country, fetch_enabled_hosts
from zbbx_mcp.resolver import InstanceResolver
from zbbx_mcp.utils import confined_input_path

# Products whose hosts are named by role and legitimately carry no country.
# Mirrors the reporting side exactly: if the two sets disagree, both systems
# compute "hosts missing a country" over different populations and the diff
# reports a defect that does not exist.
COUNTRYLESS_PRODUCTS = frozenset({"infrastructure", "monitoring", "unknown", ""})

# Verdicts.
MATCH = "MATCH"
DRIFT = "DRIFT"
POP_DRIFT = "POP_DRIFT"  # aggregate counts moved but per-country resolution agrees
DIVERGE = "DIVERGE"
ADVISORY = "ADVISORY"
MISSING = "MISSING"

# Aggregate host-population counts. When per-country resolution provably agrees
# (every compared country matches), a difference in THESE is fleet drift or a
# naming lag between the two runs — not a resolution defect — so they are
# downgraded from DIVERGE to POP_DRIFT (ADR 101 addendum). `countries` is NOT
# here: a change in the DISTINCT-country count is itself a resolution signal.
_AGGREGATE_FIELDS = frozenset({
    "total_hosts", "country_host_sum", "countryless_by_design",
    "blank_country_hosts",
})

# Fields we can judge, because both sides derive them from the same host list
# with the same two helpers.
_STRICT_FIELDS = (
    "total_hosts",
    "countries",
    "country_host_sum",
    "countryless_by_design",
    "blank_country_hosts",
)


def adapt_reported_facts(raw: dict) -> tuple[dict, list[str]]:
    """Normalise a published facts file to the field names judged here.

    The reporting side publishes a per-country snapshot keyed by country code
    with a ``_meta`` block, not the flat field names this tool judges. Without
    a translation every run reports "nothing comparable" — truthfully, but the
    check then never compares anything, which is worse than a wrong answer
    because it looks like a pass.

    Only quantities with provably identical definitions are mapped. The
    reporting side's server count scopes differently from a host count here,
    so it is deliberately NOT mapped onto ``total_hosts``; it is returned as a
    note instead. Pure.
    """
    if "_meta" not in raw:
        return raw, []                       # already flat, or an unknown shape
    meta = raw.get("_meta") or {}
    per_country = {k: v for k, v in raw.items()
                   if k != "_meta" and isinstance(v, dict)}
    out: dict = {}
    notes: list[str] = []

    # Same definition on both sides: how many distinct countries resolved.
    if "total_countries" in meta:
        out["countries"] = meta["total_countries"]
    elif per_country:
        out["countries"] = len(per_country)

    if "total_servers" in meta:
        notes.append(
            f"reported total_servers={meta['total_servers']} is not compared: "
            "it scopes to servers, not to the host population counted here")
    return out, notes


def internal_consistency(raw: dict) -> list[str]:
    """Contradictions inside the published facts, independent of live data.

    A file that disagrees with itself cannot be reconciled against anything,
    and the disagreement is visible without querying Zabbix at all — so it is
    worth saying before any comparison is attempted. Pure.
    """
    if "_meta" not in raw:
        return []
    meta = raw.get("_meta") or {}
    per_country = {k: v for k, v in raw.items()
                   if k != "_meta" and isinstance(v, dict)}
    out: list[str] = []

    total = meta.get("total_servers")
    summed = sum(int(v.get("servers", 0) or 0) for v in per_country.values())
    if isinstance(total, int) and per_country and total != summed:
        out.append(f"`_meta.total_servers` is {total} but the per-country "
                   f"blocks sum to {summed} — the file disagrees with itself")

    declared = meta.get("total_countries")
    if isinstance(declared, int) and per_country and declared != len(per_country):
        out.append(f"`_meta.total_countries` is {declared} but there are "
                   f"{len(per_country)} country blocks")

    up = sum(int(v.get("vpn_up", 0) or 0) for v in per_country.values())
    tot = sum(int(v.get("vpn_total", 0) or 0) for v in per_country.values())
    if tot and up > tot:
        out.append(f"availability numerator exceeds its denominator "
                   f"({up} up of {tot}) — any percentage derived from this "
                   "file can exceed 100%")
    return out


@dataclass
class DiffRow:
    """One compared quantity."""

    field: str
    reported: object
    live: object
    verdict: str
    note: str = ""


def build_live_facts(hosts: list[dict]) -> dict:
    """Compute the host-population facts from a live host list.

    Deliberately mirrors the reporting side's shape and scoping so the result
    is directly comparable. A host with no derivable country counts as a gap
    only where a country is *expected*; role-named infrastructure is tallied
    separately rather than reported as missing data. Pure.
    """
    countries: dict[str, int] = {}
    blank: list[str] = []
    countryless_ok = 0

    for h in hosts:
        name = h.get("host", "") or ""
        cc = extract_country(name)
        if cc:
            countries[cc] = countries.get(cc, 0) + 1
            continue
        product, _tier = _classify_host(h.get("groups", []) or [])
        if str(product or "").lower() in COUNTRYLESS_PRODUCTS:
            countryless_ok += 1
        else:
            blank.append(name)

    return {
        "total_hosts": len(hosts),
        "countries": len(countries),
        "country_host_sum": sum(countries.values()) + len(blank) + countryless_ok,
        "countryless_by_design": countryless_ok,
        "blank_country_hosts": len(blank),
        "blank_country_sample": sorted(blank)[:5],
        "top_countries": dict(
            sorted(countries.items(), key=lambda kv: -kv[1])[:10]
        ),
    }


def _classify_count(reported, live, drift_tolerance: int) -> tuple[str, str]:
    """Verdict for a pair of counts, separating drift from divergence."""
    if reported is None:
        return MISSING, "not published by the reporting side"
    if live is None:
        return MISSING, "not computed here"
    try:
        delta = int(live) - int(reported)
    except (TypeError, ValueError):
        return (MATCH, "") if reported == live else (DIVERGE, "non-numeric mismatch")
    if delta == 0:
        return MATCH, ""
    if abs(delta) <= drift_tolerance:
        return DRIFT, f"{delta:+d} — within expected change between runs"
    return DIVERGE, f"{delta:+d} — beyond tolerance, one side is wrong"


def compare_facts(
    reported: dict,
    live: dict,
    *,
    drift_tolerance: int = 2,
) -> list[DiffRow]:
    """Diff published facts against live ones. Pure.

    Only ``_STRICT_FIELDS`` receive a judgement. Everything else recognised is
    surfaced as ADVISORY with the reason it cannot be judged, so the output
    never implies agreement it has not established.
    """
    rows: list[DiffRow] = []

    for field in _STRICT_FIELDS:
        verdict, note = _classify_count(
            reported.get(field), live.get(field), drift_tolerance
        )
        rows.append(DiffRow(field, reported.get(field), live.get(field), verdict, note))

    # Per-country counts: compare only the countries both sides listed. The
    # published list is truncated to its top entries, so a country absent
    # there is not evidence of anything.
    rep_top = reported.get("top_countries") or {}
    live_top = live.get("top_countries") or {}
    for cc in sorted(set(rep_top) & set(live_top)):
        verdict, note = _classify_count(rep_top[cc], live_top[cc], drift_tolerance)
        rows.append(DiffRow(f"country[{cc}]", rep_top[cc], live_top[cc], verdict, note))

    # Subjects that share a name but not a definition. Judging these would
    # manufacture divergence on correct data (ADR 101).
    if reported.get("premium_sla"):
        sla = reported["premium_sla"]
        rows.append(
            DiffRow(
                "premium_sla.weighted_pct",
                sla.get("weighted_pct"),
                None,
                ADVISORY,
                "period-integrated there vs point-in-time here — not comparable",
            )
        )
    if reported.get("erosion"):
        ero = reported["erosion"]
        rows.append(
            DiffRow(
                "erosion.eroding",
                ero.get("eroding"),
                None,
                ADVISORY,
                "thresholds that produced this are not published — re-run "
                "detect_traffic_erosion with matching parameters to compare",
            )
        )

    # Discriminator (ADR 101 addendum, found by dogfooding the tool live): if
    # EVERY compared per-country count matches and the distinct-country count
    # matches, country resolution provably agrees between the two sides — so a
    # difference in the aggregate population counts cannot be a resolution
    # defect. It is fleet drift or a naming lag between the runs. Downgrading
    # keeps the tool from crying wolf ("one side is wrong") on exactly the
    # benign case it exists to distinguish. A per-country mismatch, or no
    # per-country evidence at all, leaves the DIVERGE verdict untouched.
    if _resolution_agrees(rows):
        for r in rows:
            if r.field in _AGGREGATE_FIELDS and r.verdict == DIVERGE:
                r.verdict = POP_DRIFT
                r.note = (
                    r.note.split(" — ")[0]
                    + " — but every per-country count matches, so resolution "
                    "agrees; this is fleet drift / naming lag between the runs, "
                    "not a defect"
                )

    return rows


def _resolution_agrees(rows: list[DiffRow]) -> bool:
    """True when the two sides provably resolve countries the same way.

    Requires at least one compared per-country count (no evidence is not
    agreement), every such count matching, and the distinct-country total
    matching. Scoped to the published top-N, so it cannot certify the long
    tail — but combined with each side's internal country_host_sum
    reconciliation it is strong evidence, and it is exactly the signal that
    separates population drift from a resolution defect. Pure.
    """
    country_rows = [r for r in rows if r.field.startswith("country[")]
    if not country_rows or any(r.verdict != MATCH for r in country_rows):
        return False
    countries_row = next((r for r in rows if r.field == "countries"), None)
    return countries_row is not None and countries_row.verdict == MATCH


def summarize(rows: list[DiffRow]) -> tuple[str, dict[str, int]]:
    """Overall verdict plus per-verdict counts. Pure."""
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.verdict] = counts.get(r.verdict, 0) + 1
    if counts.get(DIVERGE):
        overall = "DIVERGENCE — the two systems disagree beyond drift"
    elif counts.get(POP_DRIFT):
        overall = (
            "country resolution agrees; host population drifted between the "
            "runs (fleet change / naming lag) — not a defect"
        )
    elif counts.get(DRIFT):
        overall = "consistent (small drift only)"
    elif counts.get(MATCH):
        overall = "consistent"
    else:
        overall = "nothing comparable was published"
    return overall, counts


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()) -> None:

    if "compare_report_facts" not in skip:

        @mcp.tool()
        async def compare_report_facts(
            facts_path: str = "",
            drift_tolerance: int = 2,
            instance: str = "",
        ) -> str:
            """Diff a reporting pipeline's published facts against live Zabbix.

            Reads the JSON figures a sibling reporting run publishes and
            compares them with the same quantities computed here, so a
            divergence between two independently-maintained systems reading one
            Zabbix instance is caught deliberately instead of by someone
            noticing two different numbers in two documents.

            Only quantities with provably identical definitions are judged;
            same-named figures computed to different definitions (uptime/SLA)
            are shown side by side and marked not-comparable rather than
            diffed, so the check cannot cry wolf on correct data.

            Args:
                facts_path: Path to the published facts JSON. Defaults to
                    $ZABBIX_CROSSCHECK_FACTS. Confined to the allowed roots.
                drift_tolerance: Count delta tolerated as normal change
                    between the two runs (default: 2)
                instance: Zabbix instance (optional)
            """
            try:
                path = facts_path or os.environ.get("ZABBIX_CROSSCHECK_FACTS", "")
                if not path:
                    return (
                        "No facts file given. Pass `facts_path`, or set "
                        "ZABBIX_CROSSCHECK_FACTS to the JSON the reporting run "
                        "publishes."
                    )
                try:
                    resolved = confined_input_path(path)
                except ValueError as e:
                    return f"Cannot read facts file: {e}"

                with open(resolved, encoding="utf-8") as fh:
                    reported = json.load(fh)
                if not isinstance(reported, dict):
                    return "Facts file must contain a JSON object."

                # Contradictions inside the file are visible without any live
                # data, and a file that disagrees with itself cannot be
                # reconciled against anything.
                self_notes = internal_consistency(reported)
                reported, scope_notes = adapt_reported_facts(reported)

                age_note = ""
                try:
                    age_h = (_time.time() - os.path.getmtime(resolved)) / 3600
                    # The published facts carry no timestamp of their own, so
                    # the file's own age is the only staleness signal available
                    # — and an undated snapshot cannot support a strict verdict.
                    age_note = f"\n\n_Facts file is {age_h:.1f}h old (mtime)._"
                    if age_h > 48:
                        age_note += (
                            " **Older than 48h — differences below are as likely "
                            "to be fleet change as disagreement.**"
                        )
                except OSError:
                    pass

                client = resolver.resolve(instance)
                hosts = await fetch_enabled_hosts(client)
                live = build_live_facts(hosts)

                rows = compare_facts(
                    reported, live, drift_tolerance=max(0, int(drift_tolerance))
                )
                overall, counts = summarize(rows)

                tally = ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
                parts = [
                    f"**Report cross-check: {overall}**",
                    f"_{tally}_\n",
                    "| Field | Reported | Live | Verdict | Note |",
                    "|-------|---------:|-----:|---------|------|",
                ]
                _order = {DIVERGE: 0, POP_DRIFT: 1, DRIFT: 2, MISSING: 3, ADVISORY: 4, MATCH: 5}
                for r in sorted(rows, key=lambda x: _order.get(x.verdict, 9)):
                    rep = "–" if r.reported is None else r.reported
                    liv = "–" if r.live is None else r.live
                    parts.append(
                        f"| {r.field} | {rep} | {liv} | {r.verdict} | {r.note} |"
                    )

                diverging = [r for r in rows if r.verdict == DIVERGE]
                if diverging:
                    parts.append(
                        "\n**Investigate:** "
                        + ", ".join(r.field for r in diverging)
                        + ". A per-country count disagreeing means one side's "
                        "country resolution is wrong — check which. (Aggregate "
                        "counts alone are drift; see POP_DRIFT rows.)"
                    )

                pop = [r for r in rows if r.verdict == POP_DRIFT]
                if pop:
                    parts.append(
                        "\n_Population drift only: every per-country count matched, "
                        "so both sides resolve countries identically — the "
                        + ", ".join(r.field for r in pop)
                        + " difference is fleet change / naming lag between the "
                        "runs, not a defect. Likely stale facts file; re-publish "
                        "to confirm._"
                    )

                # Show BOTH samples when either side has blank-country hosts, so
                # "investigate blank_country_hosts" is actionable — the live
                # sample names hosts you can actually fix now.
                rep_s = reported.get("blank_country_sample") or []
                live_s = live.get("blank_country_sample") or []
                if rep_s or live_s:
                    bits = []
                    if live_s:
                        bits.append(f"live: {', '.join(map(str, live_s[:5]))}")
                    if rep_s:
                        bits.append(f"reported: {', '.join(map(str, rep_s[:3]))}")
                    parts.append(
                        "\n_Hosts with no derivable country (name it in Zabbix "
                        "inventory or fix the convention) — " + "; ".join(bits) + "._"
                    )

                if self_notes:
                    parts.append("\n**The published facts disagree with "
                                 "themselves** (visible without live data):")
                    parts += [f"- {n}" for n in self_notes]
                if scope_notes:
                    parts += ["\n_" + n + "._" for n in scope_notes]

                return "\n".join(parts) + age_note
            except (httpx.HTTPError, ValueError, OSError, json.JSONDecodeError) as e:
                return f"Error: {e}"
