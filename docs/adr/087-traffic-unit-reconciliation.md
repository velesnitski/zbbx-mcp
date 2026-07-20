# ADR 087: One traffic-unit conversion (`get_peak_analysis` 8×, bytes divisor 64×)

**Status:** Accepted
**Date:** 2026-07-17

## Problem

The codebase carried two conflicting notions of "convert a raw trend value to
Mbps", and neither agreed with the other:

1. **`get_peak_analysis` hardcoded `value * 8 / 1_000_000`** (`executive.py`,
   two blocks). The repo default is **bits/sec**, where every other tool
   divides by `_TRAFFIC_DIVISOR` (`1_000_000`). So on a standard deployment,
   `get_peak_analysis` reported **8× the true Mbps** — a 100 Mbps interface
   showed 800 — and disagreed with `get_traffic_report`, `detect_traffic_*`,
   the dashboards, everything, for the same item.

2. **The bytes-mode divisor was `8_000_000`** (`fetch.py`). But bytes/s → Mbps
   is `×8 / 1e6 = / 125_000`, so `/ 8_000_000` is **64× too low** (1 MB/s = 8
   Mbps read as 0.125). Config-gated (`ZABBIX_TRAFFIC_UNIT=bytes`), so latent,
   but wrong.

The two are mirror images: `get_peak_analysis`'s `×8/1e6` is *coincidentally
correct for bytes* and wrong for bits; the bytes divisor is wrong for bytes.
Nothing was correct in both modes.

## Decision

Route `get_peak_analysis` through the shared `TRAFFIC_DIVISOR` like every other
tool, and fix the bytes divisor to **`125_000`**. After this:

| mode | divisor | 100 Mbps interface reads |
|------|---------|--------------------------|
| bits/s (default) | `1_000_000` | 100 ✓ (was 800 in peak) |
| bytes/s | `125_000` | 100 ✓ (was 12.5k via /8e6) |

One conversion, correct in both modes, consistent across all tools.

## Test approach

`tests/test_traffic_units.py` (+4): the default divisor is `1_000_000` and
`100e6 bits → 100 Mbps`; the bytes divisor `125_000` is arithmetically the
bytes/s→Mbps factor (`1e6/8`); and a regression lock asserts `get_peak_analysis`
no longer contains `* 8 / 1_000_000` and routes through `TRAFFIC_DIVISOR`.
Avoids a module reload (fetch is re-exported widely, so reloading it mid-suite
would desync references). 770 → 774.

## Consequences

- `get_peak_analysis` Mbps figures match the rest of the fleet tooling on the
  default config; bytes-mode deployments read correctly for the first time.
- The peak/trough *ratio* was always right (both endpoints scaled together);
  only the absolute Mbps labels were wrong — so this changes the numbers shown,
  not the shape of the analysis. Tool count unchanged (163).

## Not included

- **A guard forbidding raw traffic arithmetic.** A lint rule against
  `* 8 / 1e6`-style literals could generalise the regression lock, but the
  single source-scan test covers the one file that had it; deferred.
