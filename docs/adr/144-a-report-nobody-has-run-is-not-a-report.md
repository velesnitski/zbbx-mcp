# 144. A report nobody has run is not a report

## Status

Accepted (v1.16.67)

## Context

The suite had 1,399 tests and 42 % line coverage. The two numbers describe
different trees. The core modules — the fetch layer every tool reads
through, the value maps, the classifier, the reading boundary — sat between
78 % and 100 %. The report generators sat between 4 % and 9 %: the executive
report, the full report, the HTML and infrastructure reports, the dashboard
export, the per-server report. Together they are more than two thousand
statements that assemble a fleet from a dozen API calls, fold it, and write a
workbook, and no test had ever driven one of them end to end. The shared
fetch layer itself was at 55 %, and the server wrapper — the one place every
response passes through — at 25 %.

Untested code in a report generator is not a hypothetical. ADR 140 made a
trend row's current value optional so that a dead agent's last reading is no
longer printed as "now". Every site that read it was updated except one, in
the executive report, which no test reached: a traffic item with history but
no live reading raised `TypeError` and the whole report failed. A second
site in the same file divided by the number of hosts with an address, which
is zero for an empty fleet. Both surfaced on the first day the module had a
wire test.

## Decision

**Every report generator has a wire test that runs it end to end.** The
test drives the real tool function through the recording client (ADR 072),
asserts the API parameters it sends, the text it returns, and the file it
writes — sheet names, rows, fills for a workbook; exact fragments for HTML —
across a small fleet, an empty fleet, a silent agent, and a host without an
address. Output paths sit under a temporary root supplied through
`ZBBX_FILE_ROOTS`, so the confinement of ADR 076 is exercised rather than
bypassed.

**The fetch layer, the server wrapper, the client and the Excel helpers are
covered in-process.** `fetch_all_data`, the trend batch, the traffic map,
the service status and the host cache are driven through canned responses;
the server is built with `create_server()` and its wrapping, compression,
read-only and skip lists are asserted on the registry it produces; the
client is exercised through a mock transport, never the network.

**Fixtures follow ADR 127 and ADR 141.** Uninhabited-territory codes,
example numbering in the reserved band, documentation address ranges,
generic labels and round costs; a last value carries a clock relative to the
run or the never-collected sentinel. The fixture guards and the private
sweep are the check that the rule held.

## Consequences

Line coverage moved from 42 % to 54 % overall, the core modules from 78 % to
94 %, the tool modules from 35 % to 46 %; 187 tests were added. The executive
report no longer fails on a stale traffic reading or an empty fleet.

What remains uncovered is named rather than assumed. The service-check
branches of three reports depend on environment variables read when the
data module is imported and cannot be enabled per test without reloading
it; the regional roll-ups have nothing to roll up because the fixture codes
belong to no region by design; two lines in the fetch layer guard an
exception that the surrounding `gather(return_exceptions=True)` already
absorbs. Each is a candidate for a later change to the code rather than for
a test that works around it.
