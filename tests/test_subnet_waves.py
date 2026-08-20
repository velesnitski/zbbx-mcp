"""Correlated decline inside one network (ADR 132).

The cohort test is what makes erosion trustworthy — a host is "eroding" only
when it falls faster than its scope's median, so a market-wide dip reads as
demand rather than N host failures. It also guarantees a blind spot: when the
decliners DOMINATE their cohort they drag the median down with themselves,
every one lands at `slope ≈ cohort_slope`, and a correlated infrastructure
event is labelled *demand*. The more hosts an event takes out, the more
certainly it hides.

`detect_disruption_wave` does not cover it either — it requires the blast
radius to span many /24s by design — so a subnet-confined wave falls between
the two tools.

Addresses here are RFC 5737 documentation ranges, per the repo-wide guard.
"""

from __future__ import annotations

from zbbx_mcp.tools.traffic_erosion import subnet_waves


def R(host: str, ip: str, decline: float) -> dict:
    return {"host": host, "ip": ip, "decline_pct": decline}


class TestTheShapeItExistsToFind:
    def test_hosts_in_one_network_falling_together_are_a_wave(self):
        """Several hosts, one network, similar decline."""
        rows = [R(f"edge-aq900{i}", f"192.0.2.{10 + i}", d)
                for i, d in enumerate((38.0, 39.0, 40.0, 38.5))]
        waves = subnet_waves(rows)
        assert len(waves) == 1
        assert waves[0]["prefix"] == 24
        assert len(waves[0]["hosts"]) == 4
        assert waves[0]["spread"] == 2.0

    def test_a_wider_range_is_used_when_the_subnet_is_not_shared(self):
        # RFC 5737 documentation ranges are all /24s, so a "same /16, different
        # /24" case cannot be built inside them. RFC 1918 space can, and is
        # equally unroutable — which is the property the guard is protecting.
        rows = [R("edge-aq9001", "10.1.1.10", 40.0),
                R("edge-aq9002", "10.1.2.20", 41.0),
                R("edge-aq9003", "10.1.3.30", 39.0)]
        waves = subnet_waves(rows)
        assert len(waves) == 1
        assert waves[0]["prefix"] == 16


class TestItDoesNotFireOnDemand:
    def test_two_hosts_are_not_a_wave(self):
        rows = [R("edge-aq9001", "192.0.2.10", 40.0),
                R("edge-aq9002", "192.0.2.11", 41.0)]
        assert subnet_waves(rows) == []

    def test_wide_spread_is_three_stories_not_one_event(self):
        """22 / 40 / 95 in one subnet is not a correlated event."""
        rows = [R("edge-aq9001", "192.0.2.10", 22.0),
                R("edge-aq9002", "192.0.2.11", 40.0),
                R("edge-aq9003", "192.0.2.12", 95.0)]
        assert subnet_waves(rows) == []

    def test_shallow_decliners_are_not_members(self):
        rows = [R("edge-aq9001", "192.0.2.10", 5.0),
                R("edge-aq9002", "192.0.2.11", 6.0),
                R("edge-aq9003", "192.0.2.12", 4.0)]
        assert subnet_waves(rows) == []

    def test_hosts_without_an_address_cannot_join(self):
        rows = [R("edge-aq9001", "", 40.0),
                R("edge-aq9002", "", 41.0),
                R("edge-aq9003", "", 39.0)]
        assert subnet_waves(rows) == []


class TestOneEventIsReportedOnce:
    def test_a_slash24_claims_its_hosts_before_the_slash16(self):
        """Otherwise the same hosts surface twice — as a rack and as a range.

        A reader cannot tell whether that is one event or two.
        """
        rows = [R("edge-aq9001", "10.2.1.10", 40.0),
                R("edge-aq9002", "10.2.1.11", 41.0),
                R("edge-aq9003", "10.2.1.12", 39.0),
                # Same /16, different /24 — not enough alone to be a wave.
                R("edge-bv9001", "10.2.5.10", 40.5)]
        waves = subnet_waves(rows)
        assert len(waves) == 1
        assert waves[0]["prefix"] == 24
        every = [h for w in waves for h in w["hosts"]]
        assert len(every) == len(set(every)), "a host appears in two waves"

    def test_two_genuinely_separate_subnets_are_two_waves(self):
        rows = ([R(f"edge-aq900{i}", f"192.0.2.{10 + i}", 40.0) for i in range(3)]
                + [R(f"edge-bv900{i}", f"198.51.100.{10 + i}", 30.0) for i in range(3)])
        waves = subnet_waves(rows)
        assert len(waves) == 2
        assert {w["prefix"] for w in waves} == {24}


class TestTheCohortMaskingCase:
    def test_a_wave_that_dominates_its_cohort_is_still_found(self):
        """The case the cohort test cannot see.

        Four of five hosts fall together, so the cohort median falls with them
        and every member reads "tracks cohort" — demand. The spatial rule does
        not consult the cohort at all, so it still fires.
        """
        rows = [R(f"edge-aq900{i}", f"192.0.2.{10 + i}", d)
                for i, d in enumerate((38.0, 39.0, 40.0, 38.5))]
        rows.append(R("edge-bv9001", "198.51.100.7", 1.0))   # the lone steady peer
        waves = subnet_waves(rows)
        assert len(waves) == 1
        assert len(waves[0]["hosts"]) == 4
