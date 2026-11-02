# 160. Trend reads sized to the server

## Status

Accepted (v1.16.80)

## Context

Profiling the local analytics showed the slowest tools all share one read:
hourly trends for every host's items in a single `trend.get`. Three defects
sat in that one shape.

1. **Too big for one request.** A month of hourly rows for a fleet is
   hundreds of thousands of rows; the frontend builds the whole answer in PHP
   memory and fails with HTTP 500. The executive dashboard and the
   month-over-month comparison failed this way at their default thirty days.
   Three report tools had worked around it by chunking hosts — one chunk
   after another, never in parallel.
2. **Limits that cut silently.** Each call site computed its own row limit as
   `items × days × 24`, one row short per item (a window of N hours touches
   N + 1 hourly buckets). Two sites used a constant that ignored the window,
   so month-over-month (sixty days against a thirty-day limit) and a
   longer-than-a-month uptime report lost part of their data with no error.
3. **No way to see any of it.** Tools report failure as text starting
   `Error`, and the analytics recorded every such call as `ok`; the history
   showed almost no failures. Wall time alone could not say whether a slow
   tool asked for too much, asked too often or computed too long. And the test
   suite wrote its synthetic calls into the same analytics file.

## Decision

- `zbbx_mcp.trends.fetch_trends(client, itemids, time_from, output,
  time_till=None)` is the one way to read trends at fleet scale. It splits
  items so no request carries more than 75,000 rows (half of what the report
  tools' chunking already proved safe), runs at most four requests at once,
  and sets each request's limit from the window it actually asks for. An
  error in any chunk is an error, never a partial answer.
- Sixteen call sites move to it; the three sequential host-chunk loops and
  the predictive tool's own loop are removed. A guard test forbids a direct
  `trend.get` outside the helper and the single-host tools.
- Analytics: a tool whose answer starts with `Error` is logged as `error`
  with the first 200 characters; every tool call also records `api_calls`
  and `api_ms`, counted per call through a context variable the client
  increments, so concurrent tool calls never mix.
- The test suite logs to a temporary directory.

## Test approach

A fake frontend that answers HTTP 500 above the row budget: a four-thousand
item, thirty-day read comes back whole across several requests; a sixty-day
read keeps both ends of the window; peak concurrency stays within the bound;
an error in one chunk raises. Wire tests that pinned the old limits now
assert the property instead — the limit covers every hourly bucket of the
window for every item. Analytics: error text is a failure, "Error" mid-text
is not; calls made under `gather` are all counted; two concurrent tool calls
keep separate counts.

## Consequences

The month-scale tools answer instead of failing, and long windows are whole.
Requests run four at a time, so the heavy tools are bounded by the slowest
quarter of their chunks rather than by one oversized request. The next
performance question is answered from `api_calls` / `api_ms` rather than by
guessing.

## Not included

Sequential per-item loops elsewhere (predictive item discovery,
external-IP history, the drop detector's two correlation reads) are small and
left for measurement to rank; batching cost-macro writes is a write-path
change and needs its own rollback design.
