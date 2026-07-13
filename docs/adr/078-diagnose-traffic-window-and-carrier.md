# ADR 078: diagnose traffic — collapsed baseline window + carrier dilution

**Status:** Accepted
**Date:** 2026-07-13

## Problem

While cross-checking a support report against Zabbix, `diagnose_host`
reported **"No traffic items / trend data available"** for hosts that were
visibly moving tens of Mbps, and `bulk_diagnose` flagged nothing. The same
hosts were analysed fine by `detect_traffic_drops`. Three separate defects,
found by bisecting the divergence:

1. **The baseline window collapsed (severe).** The code set
   `baseline_from = now - 24h` (hardcoded) while
   `baseline_till = now - traffic_hours`. For any **`traffic_hours >= 24`**
   the range was empty (at 24) or *inverted* (at 168), so `trend.get`
   returned nothing, `traffic_baseline` stayed `None`, the tool printed "No
   traffic items", and — worse — the `traffic_lost` verdict became
   **unreachable**, silently degrading to `healthy`. Simply *widening the
   window*, the natural thing to do for a longer view, disabled the check.
   The default (`traffic_hours=6`) happened to work, which is why this
   survived: it only breaks when an operator asks for a wider view.

2. **Carrier dilution (severe).** Traffic was a flat mean over *every* trend
   row across *every* matched NIC. A box with a busy carrier beside idle NICs
   read low by exactly the idle count: live, a host with `bond0` at ~60 Mbps
   and an idle `eno4` at 0 reported **30.1 Mbps** — half the truth. That
   distorts the baseline-to-recent ratio the verdict is computed from, and an
   idle peer can mask a collapse on the real carrier.

3. **Two disagreeing definitions of "traffic item".** `diagnose` matched an
   *exact* hardcoded key list (`TRAFFIC_IN_KEYS`), while `fetch_traffic_map`
   (used by `detect_traffic_drops`) globbed `net.if.in[` and filtered by
   physical-NIC prefix. So the two paths disagreed about which NICs counted —
   the direct cause of one tool seeing traffic the other could not.

## Decision

- **`_traffic_windows(now, traffic_hours)`** (pure) — the baseline is the 24h
  immediately *preceding* the recent window, so it abuts it exactly and can
  never collapse or invert at any width. `traffic_hours` is clamped to ≥ 1.
- **`_carrier_traffic_mbps(base, recent)`** (pure) — mean per interface, then
  take the busiest interface *in the baseline* as the carrier and measure
  **both** windows on that same item. Like-for-like comparison; an idle peer
  can neither dilute the figure nor mask a carrier collapse.
- **`is_physical_traffic_in_key(key)`** (pure, in `fetch.py`) — the single
  definition of "this item carries the host's inbound traffic", now used by
  **both** `fetch_traffic_map` and `diagnose`. The two can no longer drift.

## Test approach

`tests/test_diagnose.py` (+11): the window never inverts at 1/6/12/23/24/25/
168/720h (the 24h and 168h regressions are pinned explicitly), the baseline
abuts the recent window and is 24h wide, and non-positive hours are clamped;
an idle NIC no longer dilutes the carrier (60 + idle-0 → 60, not 30), a
carrier collapse stays visible next to a busy peer, no trends → `(None, None)`,
bad values skipped; the shared predicate accepts bond/eth/ens/eno/enp/ppp and
rejects virtual/tunnel NICs (docker, tun, veth, per-daemon interfaces) and
non-`net.if.in` keys. 710 → 721.

## Consequences

- `diagnose_host` / `bulk_diagnose` report real traffic at any
  `traffic_hours`, and `traffic_lost` is reachable again — a host that lost
  its traffic is no longer silently `healthy`.
- Traffic figures are no longer halved by idle NICs.
- Tool count unchanged (163).

## Not included

- **`ext*` interfaces.** `PHYSICAL_IFACE_PREFIXES` still excludes them, so a
  host whose *only* NICs are `ext1`/`ext2` reads as no-traffic. This is a
  pre-existing limitation shared by both paths (not a regression), and on the
  hosts observed the `bond0` aggregate over those NICs is matched anyway.
  Widening the prefix set deserves its own change with fleet-wide validation.
- **`TRAFFIC_IN_KEYS`** stays as `fetch_traffic_map`'s legacy fallback for
  pre-6.4 Zabbix; it is no longer a *selection* rule anywhere.
