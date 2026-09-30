# 148. Three small honesty fixes found in use

## Status

Accepted (v1.16.70)

## Context

Three defects surfaced in ordinary use within one week, none of them large,
all of the same class: a tool returned a short or empty answer that read as
a finding when it was an artefact of its own input handling or output
ordering.

- `classify_external_ips` split a pasted list on commas and newlines only.
  A space-separated list of thirty addresses was one token, failed to
  parse, and the tool reported "No valid IPs". `audit_external_ips` took a
  first line with a comma and a dot count other than three for a CSV header,
  so two addresses on one line were a header and the list was empty.
- `get_dashboard_detail` printed the referenced hosts before the pages. On
  a dashboard with a few hundred hosts the response budget cut the output
  inside the host list; the pages — what a dashboard call is for — were
  never reached, and the marker said how many characters were lost.
- `get_problems(include_resolved=True)` over a window older than the
  housekeeper's trigger-event retention returned only what the housekeeper
  had kept and presented it as the whole answer. A host with hourly flaps
  showed a handful of events for five months, which read as five quiet
  months.

## Decision

**Input is split on any separator a person would paste**, and a CSV header
is recognised by content — a first line with commas and no parsable
address — not by counting dots.

**Pages come first, hosts are trimmed to the budget with a count.** The
host list is fitted to the render budget the same way the trend batch fits
its blocks (ADR 142), and closes with one line naming how many hosts were
left out and the tool that writes the full list.

**A window that reaches past retention says so.** One `housekeeping.get`
per resolved-events call; when the window starts before the retention
horizon, the listing carries a line stating the retention period and that
anything earlier has been deleted, not resolved. Housekeeping off, or a
period that cannot be read, produces no note rather than a wrong one.

## Consequences

One extra call on the resolved-events path only. No change to the live
problem listing, the export tools, or any caller of the parsers, whose
previous inputs still parse the same way.
