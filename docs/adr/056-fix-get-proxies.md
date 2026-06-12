# ADR 056: Fix `get_proxies` — it never called a real API method

**Status:** Accepted
**Date:** 2026-06-12

## Problem

`get_proxies` called `relay.get` with a `relayid` output field. Neither
exists in any Zabbix version — the tool errored on every invocation since
it was written (the names look like an over-eager find/replace from a
data-scrub pass). The ADR 055 work surfaced it while auditing the 7.x
breaking-change surface: the 7.0 proxy-object renames could not even
apply, because the method being called was never real.

## Decision

Rewrite against the actual `proxy.get`, targeting the Zabbix 7.0+ proxy
object (the connected server is 7.4; the tool never worked on 6.x either,
so there is no 6.x behaviour to preserve):

- `relay.get` → `proxy.get`; `relayid` → `proxyid`.
- Pre-7.0 `host`/`status` → 7.0 `name`/`operating_mode`
  (0 = Active, 1 = Passive — the old code keyed Active/Passive off the
  removed `status` values 5/6).
- **New:** surface `version` + `compatibility` (available since 6.4) — a
  proxy running an outdated (⚠) or unsupported (✗) version relative to
  the server is exactly what an operator wants flagged right after a
  server upgrade like this week's.

A small pure helper `format_proxy_compat(compatibility, version)` renders
the annotation and is unit-tested; the tool body stays config-level.

## Test approach

Four pure-helper tests (`TestFormatProxyCompat`): current version renders
bare `vX.Y.Z`, outdated/unsupported annotated, unknown version + undefined
compatibility renders empty. The `proxy.get` call itself is exercised by
the registration/smoke tests like every other tool.

## Consequences

- Tool count unchanged (161). Tests +4 (566 → 570).
- `get_proxies` works for the first time; on a fleet with proxies it now
  also acts as an upgrade-companion check (version skew flagged).
- No API surface change; no new env vars.

## Not included

- **Proxy groups (`proxygroup.get`, 7.0+).** No proxy groups configured
  on the monitored instance; a `get_proxy_groups` tool is trivial to add
  when one appears.
- **6.x fallback for this tool.** The pre-7.0 property names differ, but
  the tool never worked there anyway; supporting both would double the
  surface for zero users.
