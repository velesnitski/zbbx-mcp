# ADR 004: Cost audit pipeline — sanity checks, provenance, anomaly detector, XLSX parser

**Status:** Accepted
**Date:** 2026-04-21

## Problem

A bulk cost import applied a provider aggregate as a per-server rate to
every pattern-matched host. Nothing in the MCP surface flagged the value
as an outlier against the existing provider median. After the fact there
was no standardized way to tell whether a host's cost came from a clean
billing match, a cluster extras top-up, a bulk pattern, or a median
estimate — every macro description read the same.

The XLSX used as source of truth has two distinct structures (a detailed
per-IP sheet with addon columns, and a simple name/IP/EUR-price sheet).
Each import session re-implemented the parsing in shell scripts, which
drifted and made mistakes easy.

## Decision

Four complementary changes, all in `tools/costs.py`:

1. **Provider-median helper (`_provider_medians`)** — one place that
   computes median `{$COST_MONTH}` per detected provider from all
   currently costed hosts. Reused by the other three features.

2. **Pre-flight sanity check in `import_costs_by_ip` dry-runs** — each
   proposed match is compared against its provider median. Any row at
   `≥2×` or `≤0.3×` the median is surfaced in a `⚠ Sanity check`
   section under the main table, so mis-allocations are visible before
   applying.

3. **Standardized cost-source tags** — a small set of string constants
   (`COST_SRC_BILLING_IP`, `COST_SRC_BILLING_NAME`,
   `COST_SRC_BILLING_TRANSLATED`, `COST_SRC_BILLING_COMPOUND`,
   `COST_SRC_CLUSTER_EXTRAS`, `COST_SRC_BULK_PATTERN`,
   `COST_SRC_PRODUCT_MEDIAN`, `COST_SRC_PROVIDER_MEDIAN`) written into
   the macro description at apply-time. Grep-able and machine-readable.

4. **Two new tools**:
   - `detect_cost_anomalies(high_factor, low_factor, max_results)` —
     reports every hosted cost outside the provider-median band. Uses
     the same helper and surfaces the source tag so the operator can
     tell *which* import created the outlier.
   - `import_from_xlsx(file_path, output_csv, eur_usd)` — reads the
     workbook, auto-detects the two known sheet shapes by header
     contents, and writes a flat `ip,billing_name,price_monthly` CSV
     ready for `reconcile_billing_audit` / `import_costs_by_ip`. Sibling
     IPs inside a multi-IP row are recorded with `price=0` so they
     remain matchable without double-counting.

## Consequences

- Future bulk imports get a warning on the dry-run before any mutation,
  matching the shape of the issue that motivated the ADR.
- Each macro description now answers "where did this number come from?"
  in one grep — pre-existing hosts retain their old descriptions until
  their macro is next written, which is the intended soft migration.
- `detect_cost_anomalies` doubles as a periodic health check: run it
  without arguments to see whether the fleet's cost distribution has
  drifted since the last import.
- `import_from_xlsx` removes hand-rolled parsing. It is intentionally
  opinionated about the two sheet shapes currently in use; new shapes
  will add new detection heuristics rather than a configuration surface.
- Tool count: 137 → 139.
