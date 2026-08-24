"""Absent traffic data is not evidence of flowing traffic (ADR 133).

`_classify_verdict` derived `traffic_collapsed` from a comparison that is only
possible when both figures exist:

    traffic_collapsed = (baseline is not None and recent is not None and ...)

so a host with **no traffic data at all** produced `False` — indistinguishable
from a host measured and healthy. Execution then fell to the final
agent-unreachable branch, which told the reader:

    "Agent unreachable but traffic still flowing — agent-side issue
     (restart agent, check connectivity to Zabbix server)."

That sentence asserts traffic is flowing on the strength of never having
measured any. It is the most consequential form of this defect found so far,
because it does not merely mislabel — it issues a wrong instruction. Live: four
hosts in one city, agent dead and no traffic for 26 days, across two separate
/24s, each recommending an agent restart. The correct action was to call the
provider.

Traffic therefore has three states — collapsed, flowing, and **unmeasured** —
and only the first two are evidence of anything.
"""

from __future__ import annotations

from zbbx_mcp.tools.diagnose import _classify_verdict, _verdict_primary_signal


def V(**kw):
    base = dict(
        mode="server",
        agent_ping_val=1,
        agent_ping_age_min=0.5,
        traffic_baseline_mbps=100.0,
        traffic_recent_mbps=90.0,
        open_problems=0,
        https_down=False,
        https_age_h=None,
    )
    base.update(kw)
    return _classify_verdict(**base)


class TestAgentDownWithNoTrafficData:
    def test_is_down_not_degraded(self):
        """The live case: silent on every channel available."""
        verdict, _ = V(agent_ping_val=0, agent_ping_age_min=60.0,
                       traffic_baseline_mbps=None, traffic_recent_mbps=None,
                       open_problems=5)
        assert verdict == "down"

    def test_never_claims_traffic_is_flowing(self):
        """The defect itself, stated directly."""
        _, action = V(agent_ping_val=0, agent_ping_age_min=60.0,
                      traffic_baseline_mbps=None, traffic_recent_mbps=None,
                      open_problems=5)
        assert "still flowing" not in action
        assert "no traffic data" in action.lower()

    def test_does_not_send_the_reader_to_the_agent(self):
        _, action = V(agent_ping_val=0, agent_ping_age_min=60.0,
                      traffic_baseline_mbps=None, traffic_recent_mbps=None)
        assert "restart agent" not in action.lower()
        assert "provider" in action.lower()

    def test_the_primary_signal_distinguishes_the_two_down_cases(self):
        assert _verdict_primary_signal(
            {"verdict": "down", "traffic_baseline_mbps": None}
        ) == "agent down + no traffic data"
        assert _verdict_primary_signal(
            {"verdict": "down", "traffic_baseline_mbps": 200.0}
        ) == "agent down + traffic collapsed"


class TestTheOtherBranchesStillWork:
    def test_agent_down_with_traffic_genuinely_flowing_is_still_degraded(self):
        """The fix must not turn every agent fault into an outage.

        Here traffic IS measured and IS healthy — the one case where
        'agent-side issue' is the right call.
        """
        verdict, action = V(agent_ping_val=0, agent_ping_age_min=60.0,
                            traffic_baseline_mbps=100.0,
                            traffic_recent_mbps=95.0)
        assert verdict == "degraded"
        assert "still flowing" in action

    def test_agent_down_with_collapsed_traffic_is_still_down(self):
        verdict, action = V(agent_ping_val=0, agent_ping_age_min=60.0,
                            traffic_baseline_mbps=200.0,
                            traffic_recent_mbps=2.0)
        assert verdict == "down"
        assert "hosting provider" in action

    def test_agent_up_with_collapsed_traffic_is_still_traffic_lost(self):
        verdict, _ = V(traffic_baseline_mbps=200.0, traffic_recent_mbps=2.0)
        assert verdict == "traffic_lost"

    def test_agent_up_with_no_traffic_data_is_not_down(self):
        """Unmeasured alone is not an outage — only unmeasured AND agent down."""
        verdict, _ = V(traffic_baseline_mbps=None, traffic_recent_mbps=None)
        assert verdict == "healthy"

    def test_agent_up_no_traffic_data_but_problems_open_is_degraded(self):
        verdict, _ = V(traffic_baseline_mbps=None, traffic_recent_mbps=None,
                       open_problems=3)
        assert verdict == "degraded"

    def test_a_low_baseline_is_not_a_collapse(self):
        # The >= 5 Mbps floor: a quiet host dropping from 2 to 0.1 is not an
        # outage claim, and must not become one via the unmeasured path either.
        verdict, _ = V(agent_ping_val=0, agent_ping_age_min=60.0,
                       traffic_baseline_mbps=2.0, traffic_recent_mbps=0.1)
        assert verdict == "degraded"


class TestBulkSummaryCountsIt:
    def test_a_silent_host_reaches_the_flagged_count(self):
        """`bulk_diagnose` summarises only down/traffic_lost/https_down.

        While a silent host classified as `degraded`, a whole city offline for
        26 days rendered as '0 flagged as down' above a table listing it.
        """
        from zbbx_mcp.tools.diagnose import _render_bulk_table

        rows = [{
            "host": "edge-aq9001", "verdict": V(
                agent_ping_val=0, agent_ping_age_min=60.0,
                traffic_baseline_mbps=None, traffic_recent_mbps=None,
                open_problems=5)[0],
            "mode": "server", "action": "x",
            "traffic_baseline_mbps": None, "problems": [1, 2, 3, 4, 5],
        }]
        out = _render_bulk_table(rows, 1)
        assert "(1 flagged as down" in out
