# 166. Configuration cache and lean trend reads

## Status

Accepted (v1.19.0)

## Context

With per-method API accounting (ADR 160/161) in place, five of the heaviest
tools were run once each against a large fleet. Wall times were
several seconds to tens of seconds; in every one the time was server-side, in two calls:

- `trend.get` — the hourly rows themselves, and
- `item.get` — the fleet-wide pattern search for network-interface items,
  seconds per tool call. `fetch_traffic_map` ran its own tag-filtered version
  of it beside the shared one, so a report could scan twice, and every tool
  repeated it for data that changes only when a template is edited.

Locally, building trend rows cost a `datetime` + `strftime` and a second
`float()` per row — millions of rows for a fleet over a month — and the
stdlib JSON parser was about twice as slow as orjson on responses that size.

A rewrite in another language was considered and rejected: the measured time
is the server's, which no client language changes, and the local share is
recoverable in place.

## Decision

- **Configuration cache** (`config_cache.py`): configuration reads are cached
  for `ZABBIX_CONFIG_CACHE_TTL` seconds (default 300, `0` off) and concurrent
  identical reads share one request. Values — `lastvalue`, trends, history —
  are never cached. Every method that is not a read clears the cache in
  `ZabbixClient.call`, so a write is visible to the next read.
- **One NIC discovery.** `physical_traffic_items` discovers fleet-wide
  (`templated: false`, structure fields only) when asked for 200 or more
  hosts, or for 20 or more while that discovery is cached; it keeps the
  requested hosts' physical NICs and reads their values by item id. Smaller
  requests keep the direct per-host search. `fetch_traffic_map` uses it
  instead of its tag search and exact-key fallback, which also missed
  untagged interfaces and the stock template's quoted keys.
- **Lean trend reads.** `fetch_trends_batch(extremes=False)` requests only
  `value_avg` (about 40 % less data); `peak` and `min_val` are then NaN so a
  reader that should not be there fails visibly. The eight call sites whose
  modules never read them pass it.
- **Local parsing.** Day keys are computed once per UTC day, each value is
  parsed once, and responses are parsed with orjson (stdlib fallback).
- `ZABBIX_TREND_CONCURRENCY` (1–16, default 4) lets an operator who owns the
  server's capacity raise trend parallelism.

## Test approach

Cache: TTL parsing, repeat reads served once, five concurrent reads sharing
one request, disabled cache always asking, a write clearing it on the real
client. Discovery: a fleet request scans once across two calls and reads only
physical NICs by id; a small request uses a warm cache and makes no call for
structure-only output. Trends: lean and full reads agree on averages, daily
values, trend direction and current value; lean extremes are NaN. Transport:
orjson parsing; concurrency clamping. Integration: the fleet path, forced on,
against real Zabbix 7.0, 7.4 and 8.0. A local profile of `fetch_trends_batch`
on a fleet-month payload halved its own time.

## Consequences

A second heavy tool in a session skips the interface scan, the first skips
its duplicate, and month-scale reads move less data. A new interface appears
in fleet-sized results within the cache TTL rather than at once.
