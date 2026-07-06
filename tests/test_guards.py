"""Architecture guard tests (ADR 072).

Two failure classes shipped real bugs this quarter; these tests make
recurrence a CI failure instead of a live incident:

- **API-contract guard** — an unsupported ``select*`` parameter on
  ``problem.get`` reached the wire twice (ADR 068 in triage, ADR 070 in
  get_recent_changes), each crashing the tool with -32602 on first live
  call. The guard AST-scans every ``client.call("<method>", {...})`` dict
  literal in ``src/`` against a deny-map of parameters Zabbix rejects.
  Best-effort by design: params dicts built dynamically are invisible to
  it, but every call site in this codebase (and both shipped bugs) used
  inline dict literals.

- **Doc-count guard** — hand-maintained tool counts drifted three ways
  (ADR 063: badge 161 / tier table 156 / prose 154 against a real 162).
  The guard pins every documented count to the computed registry, so a
  new tool that misses a doc site fails the suite instead of silently
  aging the README.
"""

import ast
import pathlib
import re

from zbbx_mcp.tools import ALL_TOOLS
from zbbx_mcp.tools.tiers import resolve_tier_disabled

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "zbbx_mcp"

# API method -> params Zabbix rejects with -32602. Extend as new classes
# are discovered; problem.get has NO host/group selects (only
# selectAcknowledges / selectTags / selectSuppressionData).
DENIED_PARAMS = {
    "problem.get": {"selectHosts", "selectGroups", "selectHostGroups"},
}


def iter_call_dict_keys():
    """Yield (path, lineno, api_method, param_keys) for every
    ``*.call("<method>", {...literal...})`` in src/."""
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "call"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                continue
            if len(node.args) > 1 and isinstance(node.args[1], ast.Dict):
                keys = {
                    k.value for k in node.args[1].keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)
                }
                yield path, node.lineno, node.args[0].value, keys


class TestApiContractGuard:
    def test_no_denied_params(self):
        violations = [
            f"{path.relative_to(ROOT)}:{lineno} — {method} carries "
            f"{sorted(keys & DENIED_PARAMS[method])} (Zabbix rejects with -32602; "
            "map via trigger.get instead, see ADR 068/070)"
            for path, lineno, method, keys in iter_call_dict_keys()
            if method in DENIED_PARAMS and keys & DENIED_PARAMS[method]
        ]
        assert not violations, "\n".join(violations)

    def test_scanner_not_vacuous(self):
        # The guard is only worth anything if it actually sees the call sites.
        methods = [m for _, _, m, _ in iter_call_dict_keys()]
        assert methods.count("problem.get") >= 5
        assert methods.count("trigger.get") >= 3


class TestDocCountGuard:
    N = len(ALL_TOOLS)

    def test_readme_counts_match_registry(self):
        readme = (ROOT / "README.md").read_text()
        assert f"tools-{self.N}-" in readme, (
            f"README badge != {self.N} (computed from ALL_TOOLS)")
        assert f"**{self.N} tools**" in readme, (
            f"README headline != {self.N}")

    def test_readme_tier_table_matches_computed(self):
        readme = (ROOT / "README.md").read_text()
        for tier in ("core", "ops", "finance", "reports", "full"):
            size = self.N - len(resolve_tier_disabled(tier, ALL_TOOLS))
            assert re.search(rf"^\|\s*`{tier}`\s*\|\s*{size}\s*\|", readme, re.M), (
                f"README tier row `{tier}` != computed {size}")

    def test_claude_md_count_matches(self):
        claude = (ROOT / "CLAUDE.md").read_text()
        assert f"{self.N} tools" in claude, f"CLAUDE.md header != {self.N}"
