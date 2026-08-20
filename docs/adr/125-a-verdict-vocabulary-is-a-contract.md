# ADR 125 — A verdict vocabulary is a contract, not a convention

**Status**: Accepted (2026-08-20)
**Affected**: `src/zbbx_mcp/tools/traffic_shaping.py` (`_SEVERITY_RANK`,
`combine_directions`), `tests/test_shaping_directions.py`.
**Amends**: ADR 124. **Extends**: ADR 107, ADR 119.

## Context

ADR 124 added `combine_directions`, which reports a host at its worse
direction. It ranked verdicts by position in a hand-typed tuple:

```python
_SEVERITY = ("shaped", "capped", "dropped", "normal", "idle", "insufficient")
...
return _SEVERITY.index(v.verdict) if v else len(_SEVERITY)
```

The module declares **eight** verdicts. That tuple listed six. Any host whose
verdict was one of the two omitted raised `ValueError: tuple.index(x): x not
in tuple`, and one of them — `no_baseline` — is produced for any host without
trend history before the comparison window. A fleet of real size always
contains such a host, so the tool failed on **every** call from the moment it
shipped until it was reported a day later.

Three things about the failure are worth recording, because each is a pattern
rather than an incident.

**The tests were green throughout.** Thirteen of them, covering the headline
rule, the pair inference, and the symmetry tolerance. Every one built its
fixtures from the six values the tuple happened to list, so the suite pinned
the behaviour of the covered subset and said nothing about the rest. This is
the question ADR 119 exists to ask — *does this test pin anything?* — and the
answer here was: it pins the half that works.

**The same commit contained the correct pattern.** Forty lines away, a
lookup was changed from `_order[...]` to `_order.get(..., 2)` precisely
because a missing key would raise. The reasoning was available and was not
carried across to the ranking. Knowing a rule is not applying it; only a test
applies it everywhere.

**The retyped strings were the vector.** The verdicts exist as module
constants. The tuple restated their values as literals, so it could not
drift *detectably* — nothing connected the two. `pinned` had the same defect
(`v.verdict in ("shaped", "capped")`), harmlessly so far.

## Decision

**Rank with a dict keyed by the constants, looked up with a default.** An
unranked verdict sorts last and the verdict string still reaches the caller
intact. Ordering is a presentation question, and no presentation question
justifies failing a report.

**Uncertainty outranks benign.** `no_baseline` and `insufficient` now rank
above `normal` and `idle`, below every real finding. A host that reads normal
one way and unjudgeable the other has not been shown to be normal; headlining
it `normal` is the pair-level form of rendering absent evidence as evidence
of absence — the thing ADR 107 forbids, cited by name in this same file where
the no-baseline verdict is constructed. A direction that is provably shaped
still wins: uncertainty outranks benign, not evidence.

**Coverage is asserted against ground truth, not a list.** A test that
retyped the vocabulary would drift exactly as the tuple did. Instead the
guard AST-walks the module for every `ShapingVerdict(...)` construction,
resolves each first argument, and asserts it is ranked — checking the
ranking against *what the code can actually produce*. A second test drives
every verdict against every other, plus `None`, and asserts no pair raises.

## Consequences

The failure mode is now structural rather than vigilant: a verdict added
later is ranked or the AST guard fails, and an unranked one degrades instead
of raising. The AST walk asserts it found constructions at all, so a broken
walk fails loudly rather than passing on an empty set (ADR 119).

Both guards were confirmed to fail against the shipped ranking before being
accepted. `peer capped` is ranked but currently unreachable — declared
vocabulary with no construction site, which the AST guard tolerates in this
direction by design: it checks that everything constructible is ranked, not
that everything ranked is constructible.

The general rule this repo keeps relearning: **an enumeration written by hand
beside the thing it enumerates is a defect with a delay on it.** Derive it,
or test it against the source.
