# 162. One tool layer across Zabbix 6.2 to 8.0

## Status

Accepted (v1.17.0)

## Context

Zabbix 8.0 LTS removed the `massupdate` methods of host, host group,
template and template group and the whole host-interface write API
(ZBXNEXT-10387, ZBXNEXT-10814). It added a JSON item value type (6), a
Telemetry query item type (24), a Push media type (5), and acknowledge
entries the server writes itself when a maintenance suppresses or releases a
problem (actions 512 and 1024, no user, no message). New installs and newly
created roles have API access disabled; the Super admin role keeps it.

The client already spans 6.2–7.x by keeping the tool layer free of version
branches: requests use only what every supported version accepts, responses
are read tolerantly. The question was whether 8.0 fits that rule or needs a
second code path, and how to keep 7.x working while adding it.

## Decision

- **One module knows the differences.** `zbbx_mcp.api_compat` holds the
  removed-methods table, the value/item/media type maps, acknowledge-action
  decoding and the single-line rendering of text and JSON values. Tools
  import from it; `items` and `media` re-export their old names so nothing
  downstream changes.
- **Never call a removed method, never ask for a newer-only field.** A guard
  test parses every API call in the source and the integration seed and
  fails on any method a supported version removed. The acknowledge labels are
  decoded from `action`, which every version returns, rather than by asking
  for 8.0's `maintenanceid`, which a 7.x server would reject along with the
  whole call; a test pins that.
- **Recognise what only 8.0 returns.** JSON and binary values render as one
  table-safe line; the history override accepts types 0–6; maintenance
  entries are attributed to the system and excluded from the user lookup;
  Push is named; the version report lists the 8.0 features.
- **Prove it on real servers.** The integration workflow becomes a matrix
  over 7.0.31 (oldest supported LTS), 7.4.9 (the current 7.x minor) and
  8.0.0, the image version a compose variable. The seed signs in as Admin,
  whose Super admin role keeps API access on a fresh 8.0 install (verified in
  the 8.0 seed data), so no role change is needed.
- The permission error names the 8.0 role API-access default.

## Test approach

Pure tests for the action labels, the system-entry rule and the text
compaction. Wire tests for JSON history, the type override, units on numeric
history, system acknowledgements and the Push type. The guard over every
literal API method in the source. End-to-end: the matrix above, one job per
release, `fail-fast` off so one release's failure does not hide another's.

## Consequences

The same build serves 7.x and 8.0, and a change that breaks either fails CI
before it can reach main. Supporting a future release means adding a row to
`api_compat` and an entry to the matrix.
