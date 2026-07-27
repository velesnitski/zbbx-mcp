# ADR 096: sortfield, removed write params, unread output, and trend ordering

**Status:** Accepted
**Date:** 2026-07-27

## Problem

Four independent API-contract defects, each producing a dead tool or a
confident wrong answer:

1. **`mediatype.get` sorted by `name`.** Unlike most `*.get` methods, its
   only sort column is `mediatypeid`; Zabbix rejects anything else with
   -32500, so `get_media_types` errored on **every** call.
2. **`maintenance.create` passed `hostids`/`groupids`.** Deprecated in 6.0
   and removed in 7.2 in favour of `hosts`/`groups` object arrays. With
   neither reaching the server the call fails regardless, since a window
   requires at least one host or group — the tool was broken outright.
   (`host.create` already used the correct shape.)
3. **`get_alert_summary` never requested `clock`**, which is exactly the
   field its current/previous split reads. Every row fell back to `now` and
   landed in "current"; with `compare=True` (the default) the query window
   is twice the stated period, so the headline count silently covered 2N
   hours and the trend row never rendered.
4. **`get_trends` passed `sortfield`/`sortorder`**, which `trend.get` does
   not accept. They were silently ignored, so `limit` sliced the natural
   ascending order — the **oldest** retained hours — while the caller
   expected the newest.

Two smaller items rode along: `map.get`'s `select*` params do not support
`"count"`, so element/link counts always rendered `?`; and `httptest.get`
was asked for `nextcheck`, which is not a property of a web scenario.

Both `get_trends` and `create_maintenance` also rejected the natural
`YYYY-MM-DD` form of their time arguments — `int(value)` raised and killed
the call, so the caller had to know to pre-convert.

## Decision

Sort media types by `mediatypeid` and order by name client-side; send
`hosts`/`groups` object arrays; add `clock` to the alert output; drop the
unsupported trend sort params, bound the default window so the answer is
recent, and order newest-first in code; request id lists from `map.get` and
count them; drop `nextcheck`. Route both time arguments through the existing
shared `utils.parse_time` (epoch / ISO date / datetime / relative) instead
of `int()`.

Add a **sortfield allow-list guard** for methods whose sort columns are
narrower than the usual "any output field", including the two that accept
no sort parameters at all. This is a class no existing guard covered.

## Test approach

`tests/test_api_contract_096.py` — one class per defect, each asserting the
wire contract and the corrected behaviour (name ordering preserved despite
the sortfield change; object arrays sent and id lists absent; `clock`
requested and the window split honoured; counts rendered as numbers; no
sort params and a bounded, newest-first trend window). `RecordingClient`
gains rollback no-ops so write tools can be wire-tested at all.

## Consequences

- Two dead tools work; the alert summary stops double-counting; trends
  answer about the present.

## Not included

- **A full per-method sortfield table.** The guard lists only methods whose
  columns are narrower than the default, which is where the risk is.
