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
