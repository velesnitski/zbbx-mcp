# 147. Trend traffic is found by prefix, like everything else

## Status

Accepted (v1.16.69)

## Context

ADR 105 established that a host's traffic items are found by key prefix
(`net.if.in[`) and filtered by interface name, because the stock agent
template writes the interface quoted (`net.if.in["enp3s0"]`) while the
in-house template writes it bare (`net.if.in[eth0]`), and an exact list of
bare keys sees only one fleet. ADR 109 routed every call site that finds
traffic items through one function so the fix could not be missed again.

One site was missed. The batch trend fetch — behind `get_trends_batch`,
`compare_servers`, `get_server_dashboard`, the executive, HTML and service
reports, and the regional trend tools — still resolved its metrics through
`METRIC_KEYS`, an exact list. A host on the stock template carried traffic
on its NIC and every one of those tools reported it as having none, which
reads as an idle machine, not as an unmeasured one.

## Decision

**The batch fetch discovers traffic through `physical_traffic_items`.**
The inbound and outbound traffic metrics are taken out of the exact-key
path and fetched by prefix search, unquoted before the interface test,
bridges and tunnels excluded, exactly as every other traffic reader does.
The other metrics keep their exact keys. Items are merged by id, and a
found item is assigned to a metric by the same predicate that selected it,
so the "busiest physical interface wins" rule now chooses among the right
candidates on both templates.

**The exact list stays out of the traffic path.** A test pins that the
exact filter carries no `net.if` key and that the one prefix search is the
literal pattern the guard of ADR 094 expects.

## Consequences

A fleet mixing both templates gets one consistent traffic column in every
trend-based tool. One more `item.get` per batch, in parallel with the
existing one. `METRIC_KEYS["traffic"]` remains for the callers that read
last values directly; it no longer decides what the trend tools see.
