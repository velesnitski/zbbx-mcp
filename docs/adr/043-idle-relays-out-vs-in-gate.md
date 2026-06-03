# ADR 043: `get_idle_relays` — gate on physical out-vs-in, not inbound only

**Status:** Accepted
**Date:** 2026-06-03

## Problem

`get_idle_relays` flagged a host when its physical NIC carried inbound
traffic (≥ `min_mgmt_kbps`) while every tunnel-class interface read 0 bps,
calling it a forwarding failure. The detection looked at `net.if.in` only.

That premise breaks for NAT-mode relays. Such a relay forwards through the
physical NIC (NAT / MASQUERADE); its named tunnel interfaces are idle **by
design**. So "physical busy + tunnels at 0" is the *normal* signature of a
healthy NAT relay, not a failure — and because results were sorted by
physical throughput, the busiest (healthiest) relays surfaced first. The
docstring already carried a "may be false positives, cross-check
architecture" hedge, but nothing gated on it, so the tool returned a wall of
healthy relays. The same flaw existed in the downstream report consumer.

## Decision

Fetch `net.if.out` and gate on the **physical out-vs-in ratio**. A relay
forwards what it receives, so flag a forwarding failure only when ALL hold:

- at least one tunnel-class interface exists,
- every tunnel interface reads 0 bps in,
- physical inbound ≥ `min_mgmt_kbps`, **and**
- physical outbound < `_OUT_IN_RATIO` (0.1) × physical inbound — i.e. traffic
  arrives but is not relayed.

A healthy forwarder has out ≈ in and is excluded. A genuine failure receives
but barely sends (out ≪ in). `_split_iface_metrics` now buckets both
directions (physical inbound, physical outbound, tunnel inbound); the output
shows In and Out kbps as the evidence, and an empty result returns a "no
forwarding failures" note rather than a list of healthy relays.

## Consequences

- The tool now returns only relays with the receive-but-don't-forward
  signature; healthy NAT-mode relays are excluded by construction.
- `_split_iface_metrics` signature changed (now takes in-items + out-items);
  `_find_idle_relays` returns `(hostid, in_kbps, out_kbps, tunnel_count,
  sample)`. Helper tests updated; a new test asserts a balanced-throughput
  relay is not flagged.
- `min_mgmt_kbps` is retained as the inbound noise floor.
- Tests: 524 passed.

## Lesson

"Interface X is idle" is not "the host is broken" until you know X is the
path that *should* carry the traffic. When a single in-counter can't
distinguish a working data path from a dead one, the discriminator is the
companion out-counter (does what comes in also go out), not a tighter
threshold on the in-counter alone.
