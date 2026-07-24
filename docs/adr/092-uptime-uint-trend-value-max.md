# ADR 092: uptime tools must read `value_max`, not `value_avg`, for 0/1 check trends

**Status:** Accepted
**Date:** 2026-07-24

## Problem

`get_service_uptime_report` (`geo_health.py`) computes hourly uptime from
service-check trends via the pure `compute_host_uptime` (ADR 075), which marks
an hour up when the trend value clears `up_threshold` (0.5). It requested
`value_avg`.

A service check is a **0/1 unsigned-int** item, so its trends live in Zabbix's
`trends_uint` table, whose `value_min` / `value_avg` / `value_max` columns are
**integer** (bigint). Zabbix computes the hourly average as `sum/count` and
stores it truncated toward zero. So an hour that was up 59 of 60 minutes —
`avg = 0.983` — is stored as **`value_avg = 0`**. Reading `value_avg >= 0.5`
then scores that near-perfect hour as a full outage: a **60× over-penalty**.
Uptime was systematically understated for every host with even a single sub-
minute dip in an hour, and a chronically flappy-but-reachable host could read
near 0%.

(Live proof from an SLA cross-validation: a trend row with `min=0 max=1
avg=0.0000 num=60` — mathematically impossible for a real mean, and the
signature of the integer-truncated column.)

## Decision

Request **`value_max`** for the 0/1 service-check trends and feed it to
`compute_host_uptime`:

- **up hour** = `value_max >= 0.5` — the protocol responded **at least once**
  that hour (reachable);
- **down hour** = `value_max = 0` — fully dark for the whole hour;
- a partial hour (`min=0, max=1`) is a flap, counted as up here (reachability),
  not a full outage — the flap dimension is `detect_check_flaps`' job (ADR 090),
  not the uptime number's.

The traffic gate keeps `value_avg` (accurate for the large-uint NIC counter,
which does not hit the truncation floor). `compute_host_uptime` is unchanged —
its per-hour input is now documented as an **up indicator** (value_max), with
the truncation reason inline so the contract can't silently regress.

### Scope check

- `get_sla_dashboard` (`executive.py`) is **not affected** — it reads point-in-
  time `lastvalue` (0/1 now), never a trend average.
- `get_trends` (`events.py`) was named as a suspect ("returns nothing for uint
  items"). Not reproduced: `trend.get` is a single API method with no table
  selector and does return `trends_uint` rows (that is how uptime is computed at
  all). The earlier empty result was a date-string time argument raising in
  `int(...)`, unrelated to table selection. Left unchanged.

## Test approach

`tests/test_uptime.py` (+2): a wire test drives `get_service_uptime_report`
through a recording client whose 24 up-hours carry `value_avg="0"` **and**
`value_max="1"` (the exact truncation shape). The host must read `100.0% /
HEALTHY`, where the old `value_avg` path read `0.0% / DOWN`; plus a contract
assertion that the check trend fetch requests `value_max`. 816 → 818.

## Consequences

- Uptime figures reflect reachability instead of a per-hour truncation
  artefact; the worst-case 60× understatement is gone. Behavioural fix, no
  API-surface or tool-count change.
- The uptime semantic is now explicitly "reachable at least once per hour",
  which composes cleanly with `detect_check_flaps` for the within-hour dip
  detail.

## Not included

- **A `value_min`-based partial-hour column.** The report answers reachability;
  intra-hour flap counts are `detect_check_flaps`' domain, not a second column
  here.
