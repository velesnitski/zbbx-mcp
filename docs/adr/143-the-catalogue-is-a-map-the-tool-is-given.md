# 143. The catalogue is a map the tool is given

## Status

Accepted (v1.16.66)

## Context

Every "what serves X" question this server is asked is really "what does
the client app offer under X". The server has three ways to answer, and
none of them is the product's.

- The **hostname code** (`country=` everywhere) is a naming convention.
  ADR 138 showed it drifting from where the box is.
- The **resolved datacenter** (`get_geo_inventory`, ADR 122/138) is where
  the box is. It says nothing about whether the app offers it, or under
  which heading.
- The **host group** (`product` / `tier`) is how operators file a host in
  Zabbix. It is close to the product's view and still not it: a host can
  sit in the right group and be absent from the catalogue because the
  product's own selection predicate — active, heartbeat within a window —
  excludes it at the moment of asking.

The product's catalogue — what a user sees as audiences, sections and
entries, and which member servers each entry hands out — lives in the
product's database. Nothing in Zabbix can reconstruct it, and every attempt
to infer it from names and groups produced a count about a different set
than the one the user is served from.

A private exporter can write that catalogue to a local JSON file. The
question is what the public server does with it.

## Decision

**The catalogue is an input, not an inference.** `get_app_view` reads the
map from `ZABBIX_APP_MAP` (a file path or inline JSON) and takes it as
given. The public code knows only the SHAPE — `audiences → sections →
entries → members[].ip`, with optional per-member `load` / `clients` and
per-entry `display_load` — in generic vocabulary; the contents live in a
gitignored `*.local.json`, the same arrangement ADR 120/122 use for
provider and datacenter ranges.

**The loader fails closed and does not cache.** Unusable input — a missing
file, malformed JSON, an entry without a key, a member whose address is not
a string, a map without `generated_at` — means no map, never a partial one.
A half-read catalogue would report some entries against the product's data
and silently omit the rest, which is the exact defect this tool exists to
remove. The exporter rewrites the file on its own schedule, so the map is
read on every call; a copy held in memory would defeat the age check.

**A member listed with no address is a count, not a defect.** The exporter
keeps a member whose address column is empty — the product still offers it —
and writes it with a null `ip`. Rejecting the whole map for it would hide the
catalogue behind one unfilled row; dropping the member would understate what
the product offers. The entry carries such members as `without_address`; the
tool counts them in `members`, prints them under their own heading, and never
lists them as matched or unmatched, because there was nothing to join on.

**The join is by address, over every interface.** `host_ip` takes a host's
first non-loopback interface, which is right for "where is this box" and
wrong for a join: the product lists a server by whichever address it hands
to clients. A member address that matches no enabled host is **named** in
the output — the product offers a server Zabbix does not monitor, and that
is a finding, not a zero-traffic server.

**The map's own provenance is printed and judged.** The header states when
the map was generated, its age, and the predicate under which its rows were
selected. A map older than the window the predicate names (a seconds or
minutes figure parsed from the text; fifteen minutes when it names none) is
flagged stale: the catalogue has moved on and the figures describe an
earlier one.

**Metrics reuse the shared definitions.** Traffic is the carrier NIC per
host (`physical_traffic_items` + `carrier_traffic`, ADR 109/137); CPU is the
live reading or nothing (`fetch_cpu_map`, ADR 140); "agent not reporting"
is a matched host with no live CPU reading, counted and named. Labels for
audience, section and entry match exactly (ADR 137).

## Consequences

The tool answers the user's question with the user's own vocabulary and is
honest about the seam: what the product offers comes from the map, how
those servers are doing comes from Zabbix, and every place the two fail to
meet — an unmatched member, a silent agent, an aged map — is stated rather
than absorbed into a total.

A deployment without an exporter gets a clear message naming the variable
and the reason, not an empty table. A deployment whose exporter breaks gets
the same message the moment the file is unusable, instead of a report that
quietly shrank.

The map's freshness is now the exporter's responsibility, and the tool
holds it to the window the exporter itself declares. That is the intended
coupling: the predicate is the only statement of how current the rows are,
so it is also the standard the map is judged by.
