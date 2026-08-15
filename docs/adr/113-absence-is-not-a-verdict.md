# ADR 113 — Two tools that printed "no data" as a finding

**Status**: Accepted (2026-08-15)
**Affected**: `src/zbbx_mcp/tools/geo_health.py` (health-matrix
recommendation), `src/zbbx_mcp/tools/diagnose.py` (traffic baseline note,
`group_records` disclosure), `tests/test_diagnose.py`.
**Extends**: ADR 103, 107, 110, 111.

## Context

A live incident — users reporting a tunnel that connects but passes no
traffic — was investigated with these tools, and **both of them misled the
investigation before any real cause was examined**.

**1. The health matrix reported unmeasured as broken.** `_status()` returns
`"N/A"` when nothing was checked. The recommendation line then built its
working-protocol list by testing `"OK" in s or "PARTIAL" in s`; `"N/A"`
contains neither, so the list came back empty and the row printed
**`ALL DEGRADED`** — the most alarming state on the board. In the live run,
roughly a third of the country rows said `ALL DEGRADED` purely for having no data, and the
one country users confirmed was *working* was among them.

A weaker form of the same bug sat one branch further down: with two protocols
fine and the third unmeasured, the row read `Proto 1 / Proto 2 only`, which
asserts the third is broken when nobody looked.

**2. `diagnose_host`'s traffic ratio read like a verdict.** Its baseline is the
24 hours immediately *preceding* the recent window (ADR 078), so an off-peak
window is compared against a baseline containing the previous evening's peak. A
perfectly healthy host reads 50–70% "of baseline" on a weekend morning. In the
live run it reported 68% and 54% on hosts that `detect_traffic_drops` — which
compares against a seasonal, same-hour-of-day band across the whole scope — cleared
completely.

Two tools in the same server disagreed about the same host, and the one an
operator reaches for first during an incident was the one that was wrong.

**3. Sub-host figures were the box's, silently.** `diagnose_host` deliberately
reads across the whole canonical group, because on a multi-VIP machine the
traffic lives on sub-host interfaces (ADR 049). That is correct. But asking
about a sub-host with no traffic items of its own returned its parent's numbers
with nothing indicating the substitution — the bulk path annotates `(+N sub)`,
the single-host path did not.

## Decision

- `N/A` is no longer collapsed into a verdict. No measurements at all →
  **`NOT MEASURED — no check data`**. Some measured and all of those fine →
  **`OK where measured (N/3), M unmeasured`**. `ALL DEGRADED` now requires
  evidence of degradation: at least one protocol actually checked, and every
  checked one failing.
- The traffic ratio carries its own caveat whenever it falls below 85%: the
  baseline is the preceding 24h rather than the same hour of day, so the ratio
  is depressed outside peak hours, it is **not** an anomaly verdict, and
  `detect_traffic_drops` is the tool that gives one.
- When the figures span more than one Zabbix record, the report says so.

## Consequences

- The matrix stops crying wolf on a third of its rows, which is what makes the
  genuine `ALL DEGRADED` rows worth reading.
- `diagnose_host` still shows the ratio — it is useful — but can no longer be
  mistaken for the seasonal judgement that lives elsewhere.
- **Not fixed here**: `diagnose_host` could compute a true seasonal band, but
  that needs a 7-day trend fetch instead of 24h and would change the cost of
  every bulk diagnosis. Recorded as a follow-up rather than done blind; the
  disclosure removes the harm in the meantime.

## Verification

970 tests pass (+6). The matrix rule is pinned in all four states — no data,
genuine failure, partial measurement, fully fine — and the two disclosures are
pinned by source assertion so they cannot be quietly dropped.
