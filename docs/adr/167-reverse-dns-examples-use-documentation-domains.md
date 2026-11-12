# 167. Reverse-DNS examples use documentation domains

## Status

Accepted

## Context

ADR 127 names examples with `example.*` domains. The reverse-DNS helpers
(`rdns_label` in the bootstrap tool and script) were documented and tested
with provider-style names written out by hand, so the convention did not yet
cover them.

## Decision

Their docstrings and the `rdns_label` test use `example.net` and
`example.org` names. Behaviour is unchanged: the helper still keeps the
last three labels of a PTR name, and the test asserts exactly that.

## Consequences

Every hand-written example in the repository now follows ADR 127. Real
provider names appear only where they are generated from the public routing
dataset (ADR 120) or listed as common datacenter codes.
