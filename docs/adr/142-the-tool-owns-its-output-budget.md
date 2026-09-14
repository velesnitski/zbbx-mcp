# 142. The tool owns its output budget

## Status

Accepted (v1.16.66)

## Context

Every response leaves the server through one wrapper that cuts it at
`ZABBIX_RESPONSE_BUDGET` characters (default 6,000) and appends
`[truncated N chars]`. The wrapper knows nothing about what it is cutting.
For a batch of trends over a cohort of a few dozen hosts with three metrics
each, the table is several times the budget; the wrapper cut it after roughly
two thirds of the rows, mid-host, and the marker said how many characters
were lost — not which hosts. The tool had already returned, so it could not
say either. The caller saw a table that stopped and had no way to ask for the
remainder except to re-run the same call with the same result.

A cut the tool makes is a fact it can state. A cut made after the fact is
invisible to both sides.

Two smaller gaps sat next to this one. The `hosts=` parameter (v1.16.64)
that lets a caller name an exact set — never trimmed by `max_results`,
unknown names reported rather than dropped — existed on `get_trends_batch`
and `compare_servers` only, so the natural follow-up ("now the load report
for those same hosts") had no way to be exact. And when a filter matched
nothing, all three tools said `No servers match the filters.` and nothing
else: a misspelled tier label and a genuinely empty set read identically.

## Decision

**The budget is read in one place.** `budget.response_budget()` reads
`ZABBIX_RESPONSE_BUDGET` with the server's default and the server's
semantics (`0` disables). The server's wrapper calls it too, so the tool's
idea of the budget and the server's cannot drift. `render_budget()` is that
figure less a fixed headroom; a tool that stops inside it is never touched by
the wrapper.

**`get_trends_batch` cuts itself, whole hosts at a time.** Rows are grouped
into one block per host, blocks are rendered in order until the next would
exceed the render budget, and the response ends with one explicit line:

    N of M hosts not shown: a, b, c[, +K more]. Request them with
    hosts=a,b,c or narrow the filter.

Up to twelve names are spelled out — enough to paste straight back into
`hosts=` — and a block is kept only if it fits *together with* the closing
line that names the hosts after it, so the closing line itself always fits.
A host is shown with every metric or not at all; a partial host is the one
thing worse than a missing one, because it reads as a host with fewer
metrics. Both aggregations (`summary`, `daily`) and both formats go through
the same fitter.

**A `format="compact"` rendering.** One `host|metric|avg|peak|min|now|trend`
line per host and metric, units stated once in the header rather than in
every cell, no markdown table scaffolding. The default (`table`) is
unchanged. Compact is roughly three fifths the characters of the table for
the same rows — and fewer tokens still, since the table's cell separators
and repeated units tokenize separately — so more hosts fit into one budget.

**`hosts=` on `get_server_load` and `get_traffic_report`**, with the same
semantics as the trend tool: exact names, never trimmed by `max_results`,
unknown names reported in a note ahead of the table, an all-unknown list
answered with a message about the names. The parsing and the note live in
one helper used by all three, so the wording cannot diverge. On these two
tools the set is applied client-side rather than in `host.get`: the load
report builds its parent map and the traffic report its canonical fold from
the whole fleet, and a server-side filter would drop the parent a named
sub-host inherits from.

**An empty match lists what exists.** All three tools now answer a
no-match with the distinct product labels and tier labels present among the
enabled hosts in scope — sorted, up to ten each, with a count of the rest —
and a reminder that filters match exactly (ADR 137/140). The traffic report
also separates "no host matched" from "hosts matched but carry no traffic
item", which one message used to cover.

## Consequences

The server's truncation still exists for every other tool. This ADR makes
one tool own its cut; the pattern — group output into units that must not
be split, fit them against `render_budget()`, name the remainder — is the
shape any other long-output tool should follow when it is next found cut
mid-row. Nothing here changes a tool that already fits.

The headroom is a constant, not a setting. It exists so that a wrapper
which later grows by a few bytes does not start cutting responses that a
tool believed fitted; making it configurable would let a deployment quietly
restore the defect.

The closing line names at most twelve hosts. Beyond that a caller who wants
the rest is better served by narrowing the filter than by pasting a long
list, and the line says so. The count is always exact.

A caller who reads the `[truncated N chars]` marker as "the tool cut this"
was already wrong; after this change a batch-trends response never carries
it, and the caller can trust that every host either appears in full or is
named at the bottom.
