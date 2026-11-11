# 165. The agent guide follows the example convention

## Status

Accepted

## Context

ADR 127 settled how examples in this repository are named: country codes of
uninhabited territories, numbering in the 9000 band, documentation IPs and
`example.*` domains. `AGENTS.md`, which coding agents read before they write
a test or a docstring, still recommended an older pattern — a common country
code with a low number — so every agent-written fixture started from the
convention ADR 127 replaced. Two fixtures had kept that older shape.

## Decision

`AGENTS.md` states the ADR 127 convention itself, with an example, and the
remaining fixtures use it. No behaviour changes; the tests assert the same
things with the new names.

## Consequences

The written rule and the guide agents actually follow now agree, so new
fixtures start in the convention instead of being corrected afterwards.
