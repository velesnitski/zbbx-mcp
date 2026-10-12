# 151. A search term is a substring

## Status

Accepted (v1.16.73)

## Context

The first run of the end-to-end suite against a real server (ADR 150) failed
two of thirty-one tests, both on `get_hostgroups`. A query of `app_` returned
no groups, and `app_free` returned only the group of that exact name. With
`searchWildcardsEnabled` set, Zabbix treats a bare term as an exact,
case-insensitive match — the trap ADR 094 recorded for item keys. The
recording client returns whatever it is given, so no unit test could see it;
the tool had been wrong since it was written.

The same run showed that `include_hosts=True` printed a count per group but
not the hosts the caller asked for.

## Decision

**A caller's search term is sent as a substring pattern.**
`utils.contains_pattern` wraps a bare term in `*…*` and leaves a term that
already carries `*` alone; the host group search and the item search use it.
Tests pin the pattern that is sent, not the rows returned.

**Host fallbacks stay exact on purpose.** Several tools look a host up by
its technical name, and on a miss retry by technical or visible name with
wildcards enabled and a limit of one. That is a case-insensitive exact match,
and it is the right one: a substring with a limit of one would silently pick
a different host whose name contains the one asked for.

**`include_hosts` lists the names**, sorted and capped at twenty-five with a
count of the rest.

## Consequences

Partial group names work as the docstring always promised. The integration
layer justified itself on its first run, catching a defect no amount of fake
data could reach.
