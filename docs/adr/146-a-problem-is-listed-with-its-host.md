# 146. A problem is listed with its host

## Status

Accepted (v1.16.69)

## Context

`get_problems(group=…)` listed twelve open problems and named no host on
any of them. `problem.get` returns the trigger's own name and no host, and
the tool printed exactly that. A reader could not tell which machine each
line belonged to, and a per-host query could not be checked against the
group view by hand; a report that agent-unavailable triggers were open on
three named hosts could be neither confirmed nor refuted from the listing.

A second, quieter gap sat in the host filter. A VIP sub-host is registered
as `<parent> <label>` (ADR 127); its per-address service checks are its own
triggers, while the agent, CPU and memory triggers live on the parent,
which is the physical machine. Asking for the sub-host's problems answered
about its addresses only and said nothing about the machine, which is what
the caller was asking about.

## Decision

**Every listed problem carries its host.** The tool requests `objectid`,
resolves the distinct trigger ids in one `trigger.get` with `selectHosts`,
and the formatter prints the host before the trigger name. The resolved
view (`include_resolved=True`) reads `event.get`, which can select hosts
directly, and prints them the same way. A trigger that resolves to no host
is printed without one rather than with a guess.

**A sub-host query includes its parent and says so.** When the host
argument has the compound form and the parent exists, the parent's id is
added to the filter and the header states it. The reader sees the machine's
problems and the address's problems in one list, each named. A plain host
name is unaffected.

## Consequences

One extra API call per listing, over the distinct trigger ids, not per
problem. The formatter's signature is unchanged; the host is an optional
field on the row, so every other caller renders as before.
