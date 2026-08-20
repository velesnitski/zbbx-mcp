"""Shaping is measured in both directions (ADR 124).

Reading ingress alone cannot see a cap on egress, and for a relay fleet egress
is the likelier one to be limited — it is what costs the provider transit. So
an egress-only cap was not merely under-reported, it was invisible.

Measuring both also answers a question neither direction can. One ceiling in
both directions is a limit on the *link* — a port speed or a plan tier. A
ceiling in one direction only is a shaper aimed at that traffic. Those are
different tickets, and they look identical from one side.
"""

from __future__ import annotations

from zbbx_mcp.tools.traffic_shaping import ShapingVerdict, combine_directions


def V(verdict: str, ceiling: float) -> ShapingVerdict:
    return ShapingVerdict(verdict, ceiling, 0.9, 9, 10, None, 0.0, "")


class TestHeadline:
    def test_reports_the_worse_direction(self):
        # The action follows the constrained direction, not the healthy one.
        headline, _ = combine_directions(V("normal", 400.0), V("shaped", 100.0))
        assert headline == "shaped"

    def test_worse_direction_wins_either_way_round(self):
        a, _ = combine_directions(V("shaped", 100.0), V("normal", 400.0))
        b, _ = combine_directions(V("normal", 400.0), V("shaped", 100.0))
        assert a == b == "shaped"

    def test_a_missing_direction_never_improves_the_verdict(self):
        headline, _ = combine_directions(V("shaped", 100.0), None)
        assert headline == "shaped"

    def test_neither_direction_measured(self):
        headline, note = combine_directions(None, None)
        assert headline == "insufficient"
        assert "either direction" in note


class TestPairInference:
    def test_one_ceiling_in_both_reads_as_a_link_limit(self):
        _h, note = combine_directions(V("capped", 100.0), V("capped", 102.0))
        assert "one limit on the link" in note
        assert "port speed or plan" in note

    def test_different_ceilings_read_as_two_limits(self):
        _h, note = combine_directions(V("capped", 100.0), V("capped", 40.0))
        assert "two separate limits" in note
        assert "100" in note and "40" in note

    def test_egress_only_reads_as_a_shaper_on_that_direction(self):
        """The case that was previously invisible."""
        _h, note = combine_directions(V("normal", 400.0), V("shaped", 100.0))
        assert "outbound only" in note
        assert "shaper on that direction" in note

    def test_ingress_only_names_the_right_direction(self):
        _h, note = combine_directions(V("shaped", 100.0), V("normal", 400.0))
        assert "inbound only" in note

    def test_an_unmeasured_opposite_is_not_called_a_shaper(self):
        """The honest case, and the one easiest to get wrong.

        With no data for the other direction there is nothing to be asymmetric
        against — a one-sided shaper and a link-wide limit are indistinguishable.
        Calling it a shaper would be a confident wrong answer.
        """
        _h, note = combine_directions(V("capped", 100.0), None)
        assert "shaper" not in note.replace("per-direction shaper", "")
        assert "not measured" in note
        assert "cannot tell" in note

    def test_both_healthy_says_so_plainly(self):
        _h, note = combine_directions(V("normal", 400.0), V("normal", 390.0))
        assert "in normal" in note and "out normal" in note


class TestSymmetryTolerance:
    def test_close_ceilings_count_as_one_limit(self):
        # 5% apart: measurement noise on the same limit.
        _h, note = combine_directions(V("capped", 100.0), V("capped", 95.0))
        assert "one limit on the link" in note

    def test_far_apart_ceilings_do_not(self):
        _h, note = combine_directions(V("capped", 100.0), V("capped", 50.0))
        assert "two separate limits" in note

    def test_the_tolerance_actually_discriminates(self):
        # A rule that always fires, or never does, is not a rule.
        near = combine_directions(V("capped", 100.0), V("capped", 91.0))[1]
        far = combine_directions(V("capped", 100.0), V("capped", 89.0))[1]
        assert "one limit" in near
        assert "two separate" in far


class TestVerdictVocabularyIsCovered:
    """The ranking must cover every verdict the module can actually produce.

    The first version of `combine_directions` ranked verdicts with
    `_SEVERITY.index(...)` over a hand-typed tuple that listed six of the
    eight. `no_baseline` occurs on any fleet with short-history hosts, so the
    tool raised `tuple.index(x): x not in tuple` on effectively every real
    call — while every test in the classes above passed, because each one
    builds its verdicts from one of the six values that happened to be listed.

    A test that retyped the vocabulary would drift exactly as the tuple did.
    So these check the ranking against **ground truth**: what the source can
    construct, and what the function does when handed it.
    """

    def test_ranking_covers_every_constructible_verdict(self):
        import ast
        import inspect

        from zbbx_mcp.tools import traffic_shaping as ts

        tree = ast.parse(inspect.getsource(ts))
        produced = {}
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "ShapingVerdict"
                    and node.args):
                first = node.args[0]
                if isinstance(first, ast.Name):
                    produced[first.id] = getattr(ts, first.id)
                elif isinstance(first, ast.Constant):
                    produced[repr(first.value)] = first.value

        assert produced, (
            "found no ShapingVerdict(...) constructions to check against — "
            "the AST walk is broken, so a pass here would mean nothing")

        missing = {n: v for n, v in produced.items()
                   if v not in ts._SEVERITY_RANK}
        assert not missing, (
            f"{sorted(missing)} can be constructed but is absent from "
            "_SEVERITY_RANK. Under the old .index() ranking that raised "
            "ValueError on every call that met such a host")

    def test_no_pair_of_verdicts_can_raise(self):
        from zbbx_mcp.tools import traffic_shaping as ts

        vocab = list(ts._SEVERITY_RANK)
        assert len(vocab) >= 8, "vocabulary shrank — check this is intended"
        for a in vocab + [None]:
            for b in vocab + [None]:
                va = V(a, 100.0) if a else None
                vb = V(b, 100.0) if b else None
                headline, note = combine_directions(va, vb)
                assert isinstance(headline, str) and headline
                assert isinstance(note, str) and note

    def test_an_unknown_verdict_degrades_instead_of_raising(self):
        # A verdict added tomorrow and not yet ranked must sort last, not
        # take the tool down. The string still reaches the caller intact.
        headline, _ = combine_directions(V("some-new-verdict", 100.0),
                                         V("shaped", 50.0))
        assert headline == "shaped"
        headline, _ = combine_directions(V("some-new-verdict", 100.0), None)
        assert headline == "some-new-verdict"


class TestUncertaintyOutranksBenign:
    """ADR 107 at the pair level.

    A host that reads normal one way and unjudgeable the other has not been
    shown to be normal. Headlining it "normal" renders absent evidence as
    evidence of absence — which is what the no-baseline verdict exists to
    prevent in the first place.
    """

    def test_no_baseline_beats_normal(self):
        headline, note = combine_directions(V("normal", 400.0),
                                            V("no_baseline", 380.0))
        assert headline == "no_baseline"
        assert "no_baseline" in note

    def test_insufficient_beats_idle(self):
        headline, _ = combine_directions(V("idle", 1.0),
                                         V("insufficient", 0.0))
        assert headline == "insufficient"

    def test_but_a_real_finding_still_wins(self):
        # Uncertainty outranks benign, not evidence. A direction that is
        # provably shaped is the headline regardless of the other side.
        for other in ("no_baseline", "insufficient"):
            headline, _ = combine_directions(V("shaped", 100.0), V(other, 0.0))
            assert headline == "shaped", other
