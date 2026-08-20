# ADR 131 — A widget reference you cannot decode

**Status**: Accepted (2026-08-20)
**Affected**: `tools/dashboards.py` (`collect_widget_refs`,
`hosts_behind_graphs`, `get_dashboard_detail`, `find_host_dashboard`),
`tests/test_dashboard_graph_refs.py`.
**Extends**: ADR 113, ADR 126, ADR 128, ADR 130.

## Context

Dashboard widgets name what they show through typed fields: `2` host group,
`3` host, `4` item, `5` item prototype, `6` graph, `7` graph prototype. Both
dashboard readers decoded **2, 3 and 4** and ignored the rest.

A classic **Graph** widget names a *graphid*, not a host. The graph belongs to
its items' hosts, so the link exists — but it needs one more hop, through
`graph.get`, to follow. Without that hop a dashboard built entirely from graph
widgets resolves to nothing.

Both callers then reported nothing as fact:

* `get_dashboard_detail` printed the widget count and no host section, so a
  dashboard reading "16 widgets" appeared to reference no hosts at all.
* `find_host_dashboard` answered **"not found on any dashboard"** for a host
  whose graphs sit on one. That is a confident negative produced by a decoder
  limitation, and it was used in this session to conclude a fleet had no
  dashboard coverage. The conclusion was unsound.

The blind spot is wide in practice: graph widgets are the ordinary way these
dashboards are built, and the largest here carry 225 and 108 widgets.

## Decision

**Decode every reference type, in one shared pure function.**
`collect_widget_refs` returns groups, hosts, items, graphs, graph prototypes
and item prototypes. Both tools use it, so they can no longer disagree about
what a dashboard mentions.

**Follow the graph hop.** `hosts_behind_graphs` resolves graphids through
`graph.get` with `selectHosts`, and those hosts join the referenced set.
`find_host_dashboard` resolves every graph across all dashboards once, then
matches the host directly *or* through a graph, and says which — "via graph
'<name>'" rather than an unexplained hit.

**Count what could not be decoded, and qualify the negative.** Some widgets
(SVG Graph and friends) name hosts by *pattern* rather than id; those cannot
be resolved to ids at all. `undecoded` counts them by widget type, the detail
view warns that their hosts are absent from its list, and a "not found"
answer from `find_host_dashboard` now carries the caveat that the host could
be on one of them. A widget with no fields is not counted — nothing to read is
not the same as failing to read something.

## Consequences

Dashboard coverage questions can be answered, and a negative answer states its
own limits instead of implying completeness.

Nine tests; four were confirmed to fail with graph decoding removed. One pins
that a host genuinely absent still reports absent, because a fix that makes
everything match is not a fix.

This is the fourth shape of one error found today, after ADR 126, 128 and 130.
Those three turned a missing measurement into a value: down, healthy, zero.
This one turns a missing *decoder* into a fact about the fleet — the reader
could not parse the reference, and reported that the reference was not there.

The rule generalises past defaults: **whenever a lookup can fail for a reason
that is about the reader rather than the data, the failure has to survive into
the output.** Otherwise the tool's own limits are indistinguishable from the
world's.
