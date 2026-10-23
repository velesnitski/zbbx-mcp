# 158. Four small read corrections

## Status

Accepted (v1.16.78)

## Context

Four defects, each small, each producing a wrong answer rather than an error:

1. The guard test forbade `sortfield` on `history.get`. The API accepts
   `itemid` and `clock` there (checked against a live 7.x server); the guard
   was stricter than the API and steered code away from a valid sort.
2. `get_item_history` asked the caller for `value_type` and defaulted it to
   float. A caller that did not know the item's type read the wrong history
   table and got an empty answer for an item that has data.
3. `rollback_by_index` could only undo. A journal entry whose object was
   since changed by hand could be neither undone safely nor removed, so it
   stayed at the head of the journal and blocked `rollback_last`.
4. `detect_service_port_split` and `detect_disruption_wave` found management
   NICs with an exact key list, missing the quoted keys of the stock
   template (the ADR 147 defect, two more call sites). The split detector
   also summed every NIC, so a bond and its member ports counted the same
   traffic twice.

## Decision

1. The guard allows `itemid` and `clock` for `history.get`; `trend.get`
   still takes none.
2. `value_type` is read from the item. The parameter remains as an override;
   values outside 0–4 fall back to the item's own type.
3. `rollback_by_index(discard=True)` removes the entry from the journal
   without calling the API, and says so.
4. Both detectors use `physical_traffic_items`; the service item is still
   fetched by its exact key. The split detector aggregates through
   `aggregate_host_windows`, the bond-aware rule the wave detector already
   uses (ADR 100).

## Test approach

Wire tests: an override and an out-of-range override of `value_type`;
discard leaves Zabbix untouched and the journal one shorter; a bond with
uneven member readings yields the bond's drop, not the sum's; a quoted key
is found. The bond and quoted-key tests fail on the previous code.

## Consequences

Item history works without knowing the item's type. The service split no
longer understates a management drop on bonded hosts, and hosts on the stock
template are no longer reported as having no management traffic.
