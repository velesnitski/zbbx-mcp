# ADR 130 — An unreported count is not a count of zero

**Status**: Accepted (2026-08-20)
**Affected**: `tools/traffic.py` (`get_traffic_report`),
`tests/test_traffic_report_connections.py`.
**Extends**: ADR 107, ADR 113, ADR 126, ADR 128.

## Context

`get_traffic_report` renders a session count and a bandwidth-per-client figure
beside each server's throughput. It read the count as:

```python
conns = host_conns.get(hid, 0)
```

A host carrying no item for the configured connections key is absent from that
map, so it took the default and rendered `0`. A host genuinely reporting no
sessions also rendered `0`. The two are not the same claim, and nothing in the
output distinguished them.

On a fleet where the configured key is not deployed, every row therefore read
`0` connections with `–` for bandwidth-per-client. That is a plausible,
alarming, entirely fabricated finding: it says the servers are carrying traffic
with nobody connected. The truth is that nobody asked those servers, because
they carry no such item.

This is the third instance of one class found in a single day, alongside ADR
126 (a missing check scored as a failing one) and ADR 128 (a key nobody carries
reported as healthy). The three differ only in which direction the absence
fell — down, up, and zero — which is why none of them looked like the same bug
from the output.

The environment variable was set, so the usual "is it configured" check passed.
Configuration being present says nothing about whether the thing configured
exists on the hosts.

## Decision

**`None` for unmeasured, and render it as such.** `host_conns.get(hid)` returns
`None` when the host carries no item; the column shows `–`, and a footnote
states the count is unknown rather than zero, with how many rows it affects.

**Unmeasured propagates correctly through the fold.** A canonical box stays
unmeasured only while none of its sub-hosts reported; a measured sibling makes
the box measured for what it could see.

**Unmeasured sorts last, not lowest.** Sorting by connections previously placed
an unmeasured host among the genuinely idle ones, since `None` had already been
flattened to `0`. It now sorts after every measured value rather than competing
with them.

**A genuine zero still reads zero.** The fix must not launder a real finding
into a shrug, and a test pins that.

## Consequences

The report can no longer state a session count that was never taken. Where the
key is undeployed the column is empty and says why, which is a question about
provisioning rather than a fleet-wide client collapse.

Six tests; two were confirmed to fail against the previous default-to-zero
read.

The rule this repo has now written three times in one day: **a default value
is a claim.** `.get(key, 0)` does not mean "zero if missing", it means "assert
zero if missing", and every such default is a place where the code answers a
question nobody measured. Where the answer matters, the absence has to survive
as far as the reader.
