# ADR 134 — Input that was not understood is not input that was not given

**Status**: Accepted (2026-08-24)
**Affected**: `tools/events.py` (`_parse_epoch`, `get_trends`),
`tests/test_trends_time_window.py`, `tests/test_api_contract_096.py`.
**Extends**: ADR 088, ADR 129.

## Context

`_parse_epoch` returned `0` for an empty argument *and* for one it could not
parse. The caller reads `0` as "not supplied" and substitutes its default
window. So an argument the parser did not understand silently became an
argument that was never given:

```python
get_trends(item_id, time_from="now-13d", time_till="now-8d")
```

returned the most recent `limit` hours. Not an error, not an empty result — a
full table of real, correctly formatted rows for a window nobody asked about.

`"now-13d"` is the shape that exposes it: it reads like a relative duration,
`parse_time` accepts only the bare `"13d"`, and nothing in the output
distinguishes the two. This was found while reconstructing an incident
timeline, where the "pre-event baseline" it returned was entirely post-event.
The error was caught only by reading the timestamps in the reply.

A second, quieter version of the same problem sits beside it: `limit` doubles
as the window **span**, so an explicit `time_from` further back than `limit`
hours is clipped. A caller asking for three days with the default `limit`
receives the last fifty hours of it, silently.

## Decision

**Distinguish absent from unintelligible.** `_parse_epoch` returns `0` for "not
supplied" and `None` for "not understood". `get_trends` refuses on `None`,
naming the offending argument and the formats that work — and refuses **before**
querying Zabbix, so a wrong window is never fetched.

**Disclose clipping.** When an explicit start is further back than `limit`
hours, the output says the window was clipped, by how much, and which knob
fixes it.

**Name the accepted formats once**, in `_TIME_FORMATS`, so the error text and
the docstring cannot drift from what `parse_time` implements.

## Consequences

An unparseable time argument now produces a refusal instead of a plausible
answer to a different question.

One existing test had to be rewritten: `test_empty_or_junk_is_zero` asserted
the conflation directly — `_parse_epoch("") == 0` and
`_parse_epoch("not-a-date") == 0` on consecutive lines. It passed for as long
as the defect existed, because it was a faithful description of it. A test can
pin a bug as firmly as it pins a feature, and this one did.

Ten new tests; five were confirmed to fail against the previous behaviour.

Worth noting for future tool arguments: the failure was silent specifically
because the rejected value looked *reasonable*. A caller who types nonsense
finds out immediately; a caller who types a near-miss of the accepted syntax
gets served. Where a parser has a narrow accepted grammar, the near-misses are
the dangerous input, not the obvious garbage.
