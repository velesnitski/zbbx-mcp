"""Mutation guard — does the suite actually pin the decisions? (ADR 119)

A green suite proves the tests run, not that they hold anything down. Three
defects in one day were all of this shape: a threshold that let a real case
through, a rule that scored a dead protocol as perfect, and a name that silently
shadowed a dict. Two were caught by a synthetic control; one by luck.

So this mutates the decision-carrying code and checks that something notices.

**Why not a mutation-testing tool.** `mutmut` and `cosmic-ray` re-run the whole
suite per mutant — hours for 1000+ tests, which cannot live in the same CI job.
This narrows the question instead: mutate only the pure functions where the
decisions live, and check each mutant against a handful of ORACLES stated right
here. No subprocess, no suite re-run, milliseconds per mutant.

An oracle is the behaviour that must survive refactoring. If a mutant passes
every oracle, the mutant *survived* — that behaviour is not pinned, and the
guard fails naming the exact line and mutation.

The targets are pure and self-contained: no network, no credentials, no
fixtures carrying real data.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "zbbx_mcp"


# --------------------------------------------------------------------------
# Mutation operators. Each returns a *semantic* change — the kind a tired
# author actually makes — not noise like renaming a local.
# --------------------------------------------------------------------------

_CMP_FLIP = {
    ast.Lt: ast.LtE, ast.LtE: ast.Lt,
    ast.Gt: ast.GtE, ast.GtE: ast.Gt,
    ast.Eq: ast.NotEq, ast.NotEq: ast.Eq,
}


class _Mutator(ast.NodeTransformer):
    """Applies exactly ONE mutation, identified by its index."""

    def __init__(self, target_index: int, scope: set[str]):
        self.target_index = target_index
        self.scope = scope
        self.seen = -1
        self.applied: str | None = None
        self._fn_stack: list[str] = []

    def _in_scope(self) -> bool:
        return bool(self._fn_stack) and self._fn_stack[-1] in self.scope

    def visit_FunctionDef(self, node):  # noqa: N802
        self._fn_stack.append(node.name)
        node = self.generic_visit(node)
        self._fn_stack.pop()
        return node

    def visit_Compare(self, node):  # noqa: N802
        self.generic_visit(node)
        if not self._in_scope() or len(node.ops) != 1:
            return node
        flip = _CMP_FLIP.get(type(node.ops[0]))
        if flip is None:
            return node
        self.seen += 1
        if self.seen == self.target_index:
            self.applied = (
                f"{self._fn_stack[-1]}:{node.lineno} "
                f"{type(node.ops[0]).__name__} -> {flip.__name__}"
            )
            node.ops = [flip()]
        return node

    def visit_BoolOp(self, node):  # noqa: N802
        self.generic_visit(node)
        if not self._in_scope():
            return node
        self.seen += 1
        if self.seen == self.target_index:
            new = ast.Or() if isinstance(node.op, ast.And) else ast.And()
            self.applied = (
                f"{self._fn_stack[-1]}:{node.lineno} "
                f"{type(node.op).__name__} -> {type(new).__name__}"
            )
            node.op = new
        return node

    def visit_Constant(self, node):  # noqa: N802
        if not self._in_scope():
            return node
        if isinstance(node.value, bool):
            self.seen += 1
            if self.seen == self.target_index:
                self.applied = f"{self._fn_stack[-1]}:{node.lineno} {node.value} -> {not node.value}"
                return ast.copy_location(ast.Constant(value=not node.value), node)
            return node
        if isinstance(node.value, (int, float)) and node.value not in (0, 1):
            self.seen += 1
            if self.seen == self.target_index:
                bumped = node.value * 2
                self.applied = f"{self._fn_stack[-1]}:{node.lineno} {node.value} -> {bumped}"
                return ast.copy_location(ast.Constant(value=bumped), node)
        return node


def _count_sites(source: str, scope: set[str]) -> int:
    n = 0
    while True:
        m = _Mutator(n, scope)
        m.visit(ast.parse(source))
        if m.applied is None:
            return n
        n += 1


def _mutate(source: str, index: int, scope: set[str]) -> tuple[str, str]:
    m = _Mutator(index, scope)
    tree = m.visit(ast.parse(source))
    ast.fix_missing_locations(tree)
    return ast.unparse(tree), (m.applied or "")


def _load(source: str, name: str):
    ns: dict = {"__name__": name}
    exec(compile(source, f"<mutant:{name}>", "exec"), ns)  # noqa: S102
    return ns


# --------------------------------------------------------------------------
# Oracles. Each asserts a behaviour that must survive any refactor. They are
# deliberately few: one per decision that a defect actually turned on.
# --------------------------------------------------------------------------

def _oracle_uptime(ns) -> None:
    protocol_score = ns["protocol_score"]
    # A dead protocol must cost the host. Scoring it 100% is the exact bug the
    # ported specification would have shipped.
    score, n = protocol_score({"a": {"up": 100, "total": 100},
                               "b": {"up": 0, "total": 100},
                               "c": {"up": 100, "total": 100}})
    assert n == 3 and 66.0 < score < 67.0
    # A protocol the host does not run must not be counted against it.
    assert protocol_score({"a": {"up": 100, "total": 100},
                           "b": {"up": 0, "total": 0}}) == (100.0, 1)
    # Nothing measured is no verdict, never a zero.
    assert protocol_score({}) == (None, 0)
    # A bad up/total pair must not produce 300%. Added because the guard
    # caught its own oracle missing this: the clamp constant could be doubled
    # and every assertion above still passed.
    assert protocol_score({"a": {"up": 300, "total": 100}})[0] == 100.0
    # A PARTIAL uptime, which is the only input that can tell the percentage
    # conversion from the clamp: with full-uptime protocols a doubled scale
    # saturates back to 100 and every assertion above still holds.
    assert protocol_score({"a": {"up": 50, "total": 100},
                           "b": {"up": 100, "total": 100}}) == (75.0, 2)
    low = ns["low_coverage_hosts"]
    assert low([{"host": "a", "hours": 19}, {"host": "b", "hours": 400}]) == ["a"]
    assert low([{"host": "a", "hours": 0}]) == []


def _oracle_shaping(ns) -> None:
    classify = ns["classify_shaping"]
    hit = ns["ceiling_hit_rate"]
    swing = ns["swing_ratio"]
    peer = ns["classify_peer_cap"]
    import math
    flat = [100.0, 100.1, 99.9, 100.0, 100.2, 99.8] * 4
    vary = [400.0, 380.0, 420.0, 350.0, 410.0, 390.0] * 8
    sine = [50 + 300 * (0.5 * (1 - math.cos(2 * math.pi * i / 24))) for i in range(48)]
    cap = [min(120.0, v) for v in sine]
    burst = [v * 1.08 if i % 11 == 0 else v for i, v in enumerate(cap)]

    # A clipped series is walled; a healthy one is not. Both directions.
    assert hit(flat)[0] > 0.9
    assert hit(vary)[0] < 0.5
    # A burst-tolerant cap must still read shaped (ADR 108).
    assert classify(burst, sine).verdict == "shaped"
    # An uncapped diurnal curve must not.
    assert classify(sine, sine).verdict == "normal"
    # No baseline is not "normal" (ADR 107).
    assert classify(vary, []).verdict == "no_baseline"
    # Swing separates a held host from a busy one (ADR 118).
    assert swing(cap) < 0.4 < swing(sine)
    # Flat AND below peers is a cap; flat AT peer level is saturation.
    assert peer(0.13, 37.0, [0.46] * 3, [400.0] * 3)[0] is True
    assert peer(0.05, 400.0, [0.46] * 3, [400.0] * 3)[0] is False
    assert peer(0.05, 30.0, [0.06] * 3, [400.0] * 3)[0] is False


def _oracle_dead_protocols(ns) -> None:
    classify = ns["classify_protocol"]
    agg = ns["aggregate_by_check"]
    nb = 10_000
    recent = set(range(nb - 24, nb))
    old = set(range(nb - 40, nb - 30))
    # One answer anywhere in the window is alive — the tool must not
    # contradict the availability rule, only see behind it.
    assert classify({nb - 20}, recent, recent, nb).state == "alive"
    assert classify(set(), recent, recent, nb).state == "never up"
    assert classify(old, recent | old, recent, nb).state == "died"
    # Too few samples is no verdict; a dark host belongs to the SLA.
    assert classify(set(), set(range(nb - 2, nb)), recent, nb).state == "too young"
    assert classify(set(), recent, set(), nb).state == "host dark"
    # The denominator comes from judged hosts, not from the failures.
    rows = [{"hostname": f"n{i}", "key": "k", "kind": "never up", "dead_h": 24}
            for i in range(5)]
    assert agg(rows, {"k": 5})[0]["fleet_wide"] is True
    assert agg(rows, {"k": 40})[0]["fleet_wide"] is False
    assert agg(rows[:1], {"k": 1})[0]["fleet_wide"] is False


TARGETS = [
    pytest.param(
        SRC / "uptime.py",
        {"protocol_score", "low_coverage_hosts"},
        _oracle_uptime,
        7,
        id="uptime",
    ),
    pytest.param(
        SRC / "tools" / "traffic_shaping.py",
        {"classify_shaping", "ceiling_hit_rate", "swing_ratio",
         "classify_peer_cap", "percentile"},
        _oracle_shaping,
        40,
        id="traffic_shaping",
    ),
    pytest.param(
        SRC / "tools" / "dead_protocols.py",
        {"classify_protocol", "aggregate_by_check"},
        _oracle_dead_protocols,
        4,
        id="dead_protocols",
    ),
]


@pytest.mark.parametrize(("path", "scope", "oracle", "expected_sites"), TARGETS)
def test_every_mutant_is_killed(path, scope, oracle, expected_sites):
    source = path.read_text()
    total = _count_sites(source, scope)
    # Pinned rather than floored. A DROP means a branch or threshold was
    # deleted and the guard silently got easier; a RISE means new decisions
    # arrived that nobody wrote an oracle for. Either way the number should be
    # looked at, not drift.
    assert total == expected_sites, (
        f"{path.name}: {total} mutation sites, expected {expected_sites}. "
        "New decisions need an oracle; removed ones need this number updated."
    )

    survivors = []
    for i in range(total):
        mutated, what = _mutate(source, i, scope)
        try:
            ns = _load(mutated, f"mut_{path.stem}_{i}")
        except Exception:
            continue          # mutant does not even import — killed
        try:
            oracle(ns)
        except AssertionError:
            continue          # killed, as intended
        except Exception:
            continue          # blew up — also killed
        survivors.append(what)

    assert not survivors, (
        f"{len(survivors)}/{total} mutants SURVIVED in {path.name} — the suite "
        "does not pin these decisions:\n  " + "\n  ".join(survivors)
    )


def test_the_guard_can_actually_fail():
    """The guard is worthless if its oracles pass on anything.

    Feeds a deliberately broken implementation through the same machinery and
    requires the oracle to reject it — otherwise a green result above would
    mean nothing.
    """
    broken = (
        "def protocol_score(per_proto):\n"
        "    return 100.0, len(per_proto or {})\n"
        "def low_coverage_hosts(rows, floor=48):\n"
        "    return []\n"
    )
    with pytest.raises(AssertionError):
        _oracle_uptime(_load(broken, "broken"))
