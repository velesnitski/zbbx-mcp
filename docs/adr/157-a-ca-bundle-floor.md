# 157. A CA bundle floor

## Status

Accepted (v1.16.77)

## Context

A certificate authority's intermediate keys were generated weak and its
intermediates revoked; browsers blocked them. `certifi` before 2026.10.19
still trusts the root they chain to (GHSA-6q48-6c7w-9gw2, critical). This
server reaches its monitoring API over HTTPS through httpx, which trusts the
certifi bundle, so a party on the network path could present a certificate
chained to that root and read or alter the API traffic, the token included.

## Decision

`certifi>=2026.10.19` is a declared floor and the lock is re-resolved, the
routine of ADR 149. No code change; the suite is the evidence the upgrade is
inert.

## Consequences

The trust store this server uses no longer contains the affected root. The
same floor applies to every Python service of the fleet that makes HTTPS
calls, checked repo by repo.
