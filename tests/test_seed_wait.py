"""ADR 164: the integration seed keeps feeding the trigger until it fires.

Runs without Docker: a fake API stands in for the server's configuration-cache
race (values pushed before the trigger is cached are never evaluated).
"""

from __future__ import annotations

import pytest

from tests.integration.seed import wait_for_problem


class RacyServer:
    """Opens a problem only for a value pushed after the trigger is cached,
    which here happens on the ``cached_after``-th push."""

    def __init__(self, cached_after: int):
        self.cached_after = cached_after
        self.pushes = 0
        self.problem = False

    def push(self):
        self.pushes += 1
        if self.pushes >= self.cached_after:
            self.problem = True

    def call(self, method, params):
        assert method == "problem.get"
        return [{"eventid": "1"}] if self.problem else []


def test_a_value_the_trigger_missed_is_pushed_again_until_it_fires():
    srv = RacyServer(cached_after=3)
    wait_for_problem(srv, "9001", timeout_s=5, repush=srv.push, poll_s=0, repush_every_s=0)
    assert srv.problem and srv.pushes == 3


def test_a_problem_already_open_needs_no_push():
    srv = RacyServer(cached_after=0)
    srv.problem = True
    wait_for_problem(srv, "9001", timeout_s=5, repush=srv.push, poll_s=0, repush_every_s=0)
    assert srv.pushes == 0


def test_without_repush_the_race_is_lost_and_reported():
    srv = RacyServer(cached_after=1)
    with pytest.raises(SystemExit, match="never produced a problem"):
        wait_for_problem(srv, "9001", timeout_s=0.05, poll_s=0.01)
