# ADR 055: Zabbix 7.2+ API compatibility (auth header + group selector)

**Status:** Accepted
**Date:** 2026-06-12

## Problem

The monitored instance was upgraded from Zabbix 6.4 to **7.4.9**, two
major versions on. Two backward-incompatible JSON-RPC changes landed in
7.2 that break this server fleet-wide:

1. **The `auth` request-body property was removed.** The client put the
   API token in the request body (`payload["auth"] = token`). On 7.2+
   every authenticated call now fails with
   `Invalid parameter "/": unexpected parameter "auth"`. Only
   `apiinfo.version` (the one unauthenticated method) kept working, which
   is why `get_zabbix_version` still answered while everything else died.

2. **`host.get` / `trigger.get` dropped the deprecated `selectGroups`
   parameter** (and the returned `groups` property became `hostgroups`).
   The tool layer uses `selectGroups` in ~76 places and reads `groups` in
   ~82 places, so host-group classification — products, tiers,
   country-by-group — would break across nearly every tool the moment the
   auth blocker was lifted.

Confirmed against the official 7.2 API change notes. Other 7.0/7.2/7.4
removals were checked and do **not** apply here: `event.get`
`select_acknowledges`/`select_alerts`, `hostgroup.get`
`monitored_hosts`/`real_hosts`, the `proxy_hostid`→`proxyid` rename, and
the `item.get` header/query-field reshape are all unused; `template.get`
passes no group selector; the dashboard `plaintext`→`itemhistory` widget
rename was already in the label map.

## Decision

Fix both at the **client boundary** (`client.py`) rather than editing ~150
call sites — one tested place, no risk of missing a site or breaking a
`host.create`/`update` `groups` *input* (which is unchanged in 7.x):

1. **Auth → header.** Stop sending `auth` in the body; send
   `Authorization: Bearer <token>` per authenticated request. The Bearer
   header has been valid since 6.0, so this works on 6.x and 7.x alike.
   `apiinfo.version` / `user.login` still go out with no auth at all.

2. **Group selector translation.** For `host.get` / `trigger.get`, rewrite
   an outgoing `selectGroups` to `selectHostGroups` (copying params, never
   mutating the caller's dict), and on the response alias `hostgroups`
   back to `groups`. The tool layer keeps its 6.x spelling and runs
   unchanged; the translation is invisible above the client.

The net effect is a single client that spans Zabbix 6.2 through 7.x.

## Test approach

`tests/test_client.py` drives `ZabbixClient.call()` through an
`httpx.MockTransport` and asserts the exact wire format — the only way to
verify this without a live server:

- authenticated calls carry `Authorization: Bearer` and **no** body `auth`;
- `apiinfo.version` carries neither;
- `host.get`/`trigger.get` send `selectHostGroups` (not `selectGroups`)
  and the `hostgroups` response is aliased back to `groups`;
- the caller's params dict is not mutated.

Five new tests (561 → 566). Full end-to-end verification against the live
7.4.9 instance is pending a reconnect of the running MCP server, which
still holds the pre-fix module in memory.

## Consequences

- Tool count unchanged (161). Test count 561 → 566.
- Restores all authenticated tools on Zabbix 7.2+; host-group
  classification works again.
- No API surface or tool-signature change; the compatibility lives
  entirely in the client.
- Minor version bump (1.13.0) marks Zabbix 7.x support as a capability.

## Not included

- **Renaming the 76 `selectGroups` call sites.** Deliberately left in the
  6.x spelling; the client translates. Centralising the shim is lower-risk
  than a 150-site edit that cannot be live-tested, and keeps a single
  knob if the names move again.
- **`relay.get` / `get_proxies`.** That tool uses non-standard method and
  property names (a sensitive-data artifact, not Zabbix API), so the 7.0
  proxy-object renames do not apply to it as written; it is out of scope
  here and tracked separately.
- **6.0 support.** `selectHostGroups` requires 6.2; 6.0 is EOL. The client
  docstring now states 6.2–7.x.
