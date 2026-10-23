# 159. MCP SDK session floor

## Status

Accepted (v1.16.79)

## Context

The MCP Python SDK before 1.30.0 keeps every StreamableHTTP session it
creates and never evicts them, so a client that opens sessions in a loop
exhausts the server's memory. This server offers `--transport
streamable-http`, so the defect is reachable whenever that transport is used;
stdio is unaffected.

## Decision

`mcp>=1.30.0` is the declared floor and the lock is re-resolved, the routine
of ADR 149. No code change; the suite is the evidence the upgrade is inert.

## Consequences

The HTTP transport no longer accumulates sessions without bound.
