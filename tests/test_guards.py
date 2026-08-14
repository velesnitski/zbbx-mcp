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

- **Select-field guard** (ADR 077) — the deny-map above only checked param
  *names*, so a *valid* param carrying an *invalid field* slipped through:
  ``selectAcknowledges`` asked for ``alias`` (a pre-5.4 user field that has
  never existed on an acknowledge object), and Zabbix rejected the whole
  call with -32602 — get_problem_detail was dead on every problem. This
  guard checks the string literals *inside* known ``select*`` lists against
  the field sets Zabbix actually accepts.

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

# (method, select-param) -> the field names Zabbix actually accepts inside it.
# A *valid* param carrying an *invalid* field is still a -32602 (ADR 077:
# selectAcknowledges asked for "alias", which killed get_problem_detail).
# Sourced from the Zabbix 7.x error text, which enumerates the legal values.
ALLOWED_SELECT_FIELDS = {
    ("problem.get", "selectAcknowledges"): {
        "acknowledgeid", "userid", "clock", "message", "action",
        "old_severity", "new_severity", "suppress_until", "taskid",
    },
    ("problem.get", "selectTags"): {"tag", "value"},
    ("problem.get", "selectSuppressionData"): {
        "maintenanceid", "suppress_until", "userid",
    },
}

# method -> top-level `output` fields Zabbix rejects with -32602 because the
# field was removed/renamed. The prior guards only checked param *names* and
# *select-list* fields, never the top-level `output` list — which is how
# get_users shipped `user.get` output ["...","type","...","rows_per_page"]
# dead-on-every-call (ADR 085: `type` removed 5.2 -> role-based `roleid`).
DENIED_OUTPUT_FIELDS = {
    "user.get": {"type", "rows_per_page"},   # removed 5.2 -> roleid (ADR 085)
    "host.get": {"available"},                # removed 6.0 -> active_available (ADR 088)
    "discoveryrule.get": {"lastclock"},       # never a property of an LLD rule (ADR 088)
    "httptest.get": {"nextcheck"},            # not a web-scenario property (ADR 096)
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


def iter_call_select_fields():
    """Yield (path, lineno, method, param, fields) for every ``select*`` param
    whose value is a list literal of string constants."""
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
            if len(node.args) < 2 or not isinstance(node.args[1], ast.Dict):
                continue
            method = node.args[0].value
            for k, v in zip(node.args[1].keys, node.args[1].values, strict=False):
                if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
                    continue
                if not isinstance(v, ast.List):
                    continue
                fields = [
                    e.value for e in v.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)
                ]
                if fields:
                    yield path, node.lineno, method, k.value, fields


def _resolve_dict_arg(arg, call_line, assigns):
    """Resolve a call's second arg to a Dict node: inline literal, or the
    nearest-preceding same-name ``name = {...}`` assignment."""
    if isinstance(arg, ast.Dict):
        return arg
    if isinstance(arg, ast.Name):
        cands = [d for ln, n, d in assigns if n == arg.id and ln < call_line]
        return cands[-1] if cands else None
    return None


def iter_call_output_fields():
    """Yield (path, lineno, method, output_fields) for every ``client.call``.

    Unlike the other two scanners, this resolves a params dict passed **by
    variable** (``params = {...}; client.call("m", params)``), not only inline
    literals — get_users built its params in a variable, which is exactly why
    the earlier guards never saw its bad ``output`` (ADR 085). Resolution is by
    nearest preceding same-name assignment in the file, which matches this
    codebase's build-then-call idiom.
    """
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        assigns: list[tuple[int, str, ast.Dict]] = [
            (node.lineno, node.targets[0].id, node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Dict)
        ]

        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "call"
                and len(node.args) > 1
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                continue
            d = _resolve_dict_arg(node.args[1], node.lineno, assigns)
            if d is None:
                continue
            for k, v in zip(d.keys, d.values, strict=False):
                if (
                    isinstance(k, ast.Constant) and k.value == "output"
                    and isinstance(v, ast.List)
                ):
                    fields = [
                        e.value for e in v.elts
                        if isinstance(e, ast.Constant) and isinstance(e.value, str)
                    ]
                    if fields:
                        yield path, node.lineno, node.args[0].value, fields


# method -> sortfields Zabbix actually accepts. Only methods whose sort
# columns are NARROWER than the usual "any output field" are listed; an
# illegal value is a hard -32500 that kills the call (ADR 096: mediatype.get
# sorted by "name", which is not a sort column, so the tool errored on every
# invocation). trend.get/history.get take no sort parameters at all.
ALLOWED_SORTFIELDS = {
    "mediatype.get": {"mediatypeid"},
    "trend.get": set(),
    "history.get": set(),
    "alert.get": {"alertid", "clock", "eventid", "status"},
    "auditlog.get": {"auditid", "clock"},
}


def iter_call_sortfields():
    """Yield (path, lineno, method, sortfield) for every literal sortfield."""
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "call"
                and len(node.args) > 1
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[1], ast.Dict)
            ):
                continue
            d = node.args[1]
            for k, v in zip(d.keys, d.values, strict=False):
                if (
                    isinstance(k, ast.Constant) and k.value == "sortfield"
                    and isinstance(v, ast.Constant) and isinstance(v.value, str)
                ):
                    yield path, node.lineno, node.args[0].value, v.value


class TestSortfieldGuard:
    def test_sortfields_are_legal(self):
        violations = []
        for path, lineno, method, field in iter_call_sortfields():
            allowed = ALLOWED_SORTFIELDS.get(method)
            if allowed is None:
                continue
            if field not in allowed:
                detail = (
                    "accepts no sort parameters at all"
                    if not allowed else f"allows only {sorted(allowed)}"
                )
                violations.append(
                    f"{path.relative_to(ROOT)}:{lineno} — {method} sortfield="
                    f"{field!r}; {method} {detail}. Zabbix rejects with -32500 "
                    "(or silently ignores). Sort client-side. See ADR 096"
                )
        assert not violations, "\n".join(violations)

    def test_guard_is_not_vacuous(self):
        # Must actually reach the narrow-sort method it exists to protect.
        seen = {(m, f) for _, _, m, f in iter_call_sortfields()}
        assert ("mediatype.get", "mediatypeid") in seen
        assert len(seen) >= 8


def iter_call_wildcard_searches():
    """Yield (path, lineno, method, field, term) for every ``client.call``
    dict literal that enables ``searchWildcardsEnabled`` alongside a literal
    ``search`` term.

    Inline literals only — by design. The sites that build params in a
    variable are the ones taking a *caller's* query, and they wrap it
    (``q = s if "*" in s else f"*{s}*"``); it is the hardcoded literals that
    got this wrong.
    """
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "call"
                and len(node.args) > 1
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[1], ast.Dict)
            ):
                continue
            d = node.args[1]
            pairs = {
                k.value: v for k, v in zip(d.keys, d.values, strict=False)
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            }
            flag = pairs.get("searchWildcardsEnabled")
            if not (isinstance(flag, ast.Constant) and flag.value is True):
                continue
            search = pairs.get("search")
            if not isinstance(search, ast.Dict):
                continue
            for sk, sv in zip(search.keys, search.values, strict=False):
                if (
                    isinstance(sk, ast.Constant) and isinstance(sk.value, str)
                    and isinstance(sv, ast.Constant) and isinstance(sv.value, str)
                ):
                    yield path, node.lineno, node.args[0].value, sk.value, sv.value


class TestSearchWildcardGuard:
    """ADR 094 — `searchWildcardsEnabled` turns a bare term into an EXACT match.

    With the flag on, Zabbix stops wrapping the term in ``%…%`` and only
    translates ``*`` → ``%``. A literal with no ``*`` therefore matches the
    key *exactly* — and since no real item key equals a prefix like
    ``vfs.fs.size``, the query silently returns nothing. Five call sites
    shipped this way; each rendered a confident, fully-populated-looking
    answer built from zero rows.
    """

    def test_literal_search_terms_carry_a_wildcard(self):
        violations = [
            f"{path.relative_to(ROOT)}:{lineno} — {method} search[{field!r}]="
            f"{term!r} has no '*' while searchWildcardsEnabled is True, so it "
            "is an EXACT match and returns nothing. Wrap it: "
            f"'*{term}*'. See ADR 094"
            for path, lineno, method, field, term in iter_call_wildcard_searches()
            if "*" not in term
        ]
        assert not violations, "\n".join(violations)

    def test_guard_is_not_vacuous(self):
        # Worthless unless it actually sees the call sites it guards.
        seen = list(iter_call_wildcard_searches())
        # Was >= 4. Eight traffic-discovery sites collapsed into one helper
        # (ADR 109), so this scanner legitimately sees one fewer inline
        # literal — consolidation, not erosion. The wildcard coverage it lost
        # is asserted directly in TestTrafficDiscoveryGuard below, so the
        # total is unchanged.
        assert len(seen) >= 3, f"scanner found only {len(seen)} wildcard searches"
        assert all("*" in term for *_, term in seen)

    def test_module_scoped_search_terms_carry_a_wildcard(self):
        """Catch search terms defined AWAY from the call site.

        The scanner above only reads `client.call` dict literals, so a term
        held in a config dict and assigned via
        `params["search"] = cfg["search"]` slipped through — that is exactly
        how the disk forecast shipped querying an exact key and silently
        fetching zero items. Heuristic but precise: within a module that
        enables wildcards anywhere, every literal `"search"` mapping must
        carry a `*`. Modules that never set the flag are untouched, because
        a bare term there is a correct substring search.
        """
        violations = []
        for path in sorted(SRC.rglob("*.py")):
            text = path.read_text()
            # Only the *variable-built* form is invisible to the scanner
            # above. A module that sets the flag inline is already covered,
            # and including it here would false-positive on its unrelated
            # non-wildcard searches (a bare term without the flag is a
            # correct substring search).
            if '["searchWildcardsEnabled"] = True' not in text:
                continue
            tree = ast.parse(text, filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Dict):
                    continue
                for k, v in zip(node.keys, node.values, strict=False):
                    if not (isinstance(k, ast.Constant) and k.value == "search"):
                        continue
                    if not isinstance(v, ast.Dict):
                        continue
                    for _sk, sv in zip(v.keys, v.values, strict=False):
                        if (
                            isinstance(sv, ast.Constant)
                            and isinstance(sv.value, str)
                            and "*" not in sv.value
                        ):
                            violations.append(
                                f"{path.relative_to(ROOT)}:{node.lineno} — search "
                                f"term {sv.value!r} has no '*' in a module that "
                                "enables searchWildcardsEnabled; it will be an "
                                "EXACT match. See ADR 094"
                            )
        assert not violations, "\n".join(violations)


class TestOutputFieldGuard:
    def test_no_denied_output_fields(self):
        violations = []
        for path, lineno, method, fields in iter_call_output_fields():
            denied = DENIED_OUTPUT_FIELDS.get(method)
            if not denied:
                continue
            bad = sorted(set(fields) & denied)
            if bad:
                violations.append(
                    f"{path.relative_to(ROOT)}:{lineno} — {method} output carries "
                    f"{bad} (Zabbix rejects the whole call with -32602; the field "
                    "was removed/renamed). See ADR 085"
                )
        assert not violations, "\n".join(violations)

    def test_output_scanner_sees_variable_built_params(self):
        # It must resolve params passed by variable, not just inline dicts —
        # that was the blind spot. user.get is built as `params = {...}`.
        methods = {m for _, _, m, _ in iter_call_output_fields()}
        assert "user.get" in methods
        assert "host.get" in methods


class TestFleetDataGuard:
    """ADR 079 — public docs must not carry deployment magnitudes.

    Documentation ages with the system it describes. Prose can
    drift into quoting one run's numbers (host counts, subnet spreads,
    regional footprints). The pre-push sensitive scan is a *string* deny-list,
    so numbers and ISO country codes are invisible to it by construction; this
    guard covers the class the scan cannot see.

    Deliberately scoped to *observed* magnitudes. Configured thresholds and
    caps ("capped at 50 hosts per call", "fires when >=5 hosts on >=3 distinct
    /24s") are design facts about this codebase, not descriptions of anyone's
    estate, and must keep passing.
    """

    DOCS = [ROOT / "CHANGELOG.md", ROOT / "README.md", ROOT / "CLAUDE.md"]

    def _docs(self):
        return self.DOCS + sorted((ROOT / "docs" / "adr").glob("*.md"))

    def _iso2(self):
        from zbbx_mcp.country import CAPITAL_COORDS
        return {c for c in CAPITAL_COORDS if len(c) == 2}

    PATTERNS = [
        (re.compile(r"fleet of \d+", re.I), "a real fleet size"),
        (re.compile(
            r"\b(returned|ranked|showed|found|reported|analys(?:ed|es)|analyz(?:ed|es))"
            r"\s+(?:all\s+|one\s+)?\d+\s+(hosts?|servers?|nodes?|clusters?)\b", re.I),
         "an observed host/server/cluster count"),
        (re.compile(r"\b\d{3,}\s+(hosts?|servers?|nodes?)\b", re.I),
         "a fleet-scale host count"),
        (re.compile(r"\b\d+\s*/24s\b"), "a real subnet-spread count"),
    ]

    def test_no_real_fleet_magnitudes(self):
        violations = []
        for path in self._docs():
            if not path.exists():
                continue
            for n, line in enumerate(path.read_text().splitlines(), 1):
                for rx, why in self.PATTERNS:
                    m = rx.search(line)
                    if m:
                        violations.append(
                            f"{path.relative_to(ROOT)}:{n} — {m.group(0)!r} looks like "
                            f"{why}; keep real fleet magnitudes out of the public repo"
                        )
        assert not violations, "\n".join(violations)

    def test_no_country_footprint_lists(self):
        # Two or more ISO-2 codes in a row ("XX / YY") describes where an estate
        # actually runs. A lone code is fine — it appears as a parameter.
        iso2 = self._iso2()
        seq = re.compile(r"\b([A-Z]{2})\b\s*[/,]\s*\b([A-Z]{2})\b")
        violations = []
        for path in self._docs():
            if not path.exists():
                continue
            for n, line in enumerate(path.read_text().splitlines(), 1):
                for m in seq.finditer(line):
                    if m.group(1) in iso2 and m.group(2) in iso2:
                        violations.append(
                            f"{path.relative_to(ROOT)}:{n} — {m.group(0)!r} discloses "
                            "the fleet's country footprint"
                        )
        assert not violations, "\n".join(violations)

    def test_guard_is_not_vacuous(self):
        # The guard must actually fire on each banned *shape* (ADR 079). Fixtures
        # are synthetic by construction: a test that hardcoded real magnitudes
        # would put into the repo exactly what this guard exists to keep out.
        shapes = [
            "`get_at_risk_hosts` ranked all 123 hosts at the same score",
            "A fleet of 456 servers might quietly",
            'returned one "wave" of 789 hosts',
            "12 hosts across 7 /24s, 62% average drop",
        ]
        for line in shapes:
            assert any(rx.search(line) for rx, _ in self.PATTERNS), line

        # Two real ISO-2 codes, taken from the dataset rather than hardcoded —
        # so the fixture cannot itself disclose a footprint.
        iso2 = self._iso2()
        a, b = sorted(iso2)[:2]
        seq = re.compile(r"\b([A-Z]{2})\b\s*[/,]\s*\b([A-Z]{2})\b")
        m = seq.search(f"hosts spanning {a} / {b}")
        assert m and m.group(1) in iso2 and m.group(2) in iso2


class TestSelectFieldGuard:
    def test_select_fields_are_legal(self):
        violations = []
        for path, lineno, method, param, fields in iter_call_select_fields():
            allowed = ALLOWED_SELECT_FIELDS.get((method, param))
            if not allowed:
                continue
            bad = sorted(set(fields) - allowed)
            if bad:
                violations.append(
                    f"{path.relative_to(ROOT)}:{lineno} — {method} {param} carries "
                    f"{bad} (Zabbix rejects the whole call with -32602; allowed: "
                    f"{sorted(allowed)}). See ADR 077"
                )
        assert not violations, "\n".join(violations)

    def test_select_scanner_not_vacuous(self):
        # Worthless unless it actually sees the call sites it is meant to guard.
        seen = {(m, p) for _, _, m, p, _ in iter_call_select_fields()}
        assert ("problem.get", "selectAcknowledges") in seen
        assert ("problem.get", "selectSuppressionData") in seen


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


class TestFileLengthGuard:
    """ADR 074 — prevent unbounded files (the test_analytics.py sink pattern).

    Length itself is cheap for both humans and AI agents when navigation is
    targeted; what hurts is the *accumulation sink* — one file every change
    appends to (edit-anchor collisions, misleading names, "where does this
    test go" defaulting to the biggest file). test_analytics.py reached
    4,104 lines / 67 classes across ~10 domains before being split.

    Budgets are deliberately generous — well-structured modules like
    executive.py (~1,050, one gated block per tool) fit comfortably. The
    guard exists to force "start a new module" over "append to the
    biggest", not to trigger refactor churn. No grandfathered exceptions:
    if this fails, split by domain (see ADR 074 for the method).
    """

    SRC_BUDGET = 1_100
    TEST_BUDGET = 1_000

    @staticmethod
    def _oversized(root, budget):
        out = []
        for path in sorted(root.rglob("*.py")):
            n = sum(1 for _ in path.open())
            if n > budget:
                out.append(f"{path.relative_to(ROOT)}: {n} lines (budget {budget})")
        return out

    def test_src_files_within_budget(self):
        over = self._oversized(SRC, self.SRC_BUDGET)
        assert not over, "Split by domain instead of growing:\n" + "\n".join(over)

    def test_test_files_within_budget(self):
        over = self._oversized(ROOT / "tests", self.TEST_BUDGET)
        assert not over, "Start a new test_<domain>.py instead:\n" + "\n".join(over)


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


class TestTrafficDiscoveryGuard:
    """ADR 109 — traffic items are found by KEY, never by item NAME.

    Item names are template cosmetics: Zabbix's stock *Linux by Zabbix agent*
    template calls the metric "Interface enp3s0: Bits received" while the
    in-house template calls it "Incoming network traffic on enp3s0". A name
    search therefore examines one fleet and reports the other as carrying no
    traffic at all — three hosts with an obvious anomaly returned a clean
    result (ADR 105).

    Five call sites had each grown their own copy of that search, so the ADR
    105 fix reached exactly one of them. They now share
    ``physical_traffic_items``; this guard is what stops a sixth copy from
    appearing, since the failure is silent and looks like a clean report.
    """

    def test_no_item_get_searches_traffic_by_name(self):
        violations = []
        for path in sorted(SRC.rglob("*.py")):
            for lineno, line in enumerate(
                path.read_text().splitlines(), start=1
            ):
                if "Incoming network traffic" in line and '"name"' in line:
                    violations.append(
                        f"{path.relative_to(ROOT)}:{lineno} — traffic items "
                        "searched by item NAME, which misses every host on the "
                        "stock Linux template. Use "
                        "fetch.physical_traffic_items(). See ADR 105/109"
                    )
        assert not violations, "\n".join(violations)

    def test_discovery_helper_is_the_only_net_if_in_search(self):
        # A second key-based search would drift from the physical-interface
        # filter the same way the name searches drifted. One definition.
        offenders = []
        for path in sorted(SRC.rglob("*.py")):
            if path.name == "fetch.py":
                continue  # the definition itself lives here
            text = path.read_text()
            if '"*net.if.in[*"' in text or "'*net.if.in[*'" in text:
                offenders.append(str(path.relative_to(ROOT)))
        assert not offenders, (
            "raw net.if.in discovery outside fetch.physical_traffic_items: "
            + ", ".join(offenders)
        )

    def test_the_helper_search_terms_are_wildcard_wrapped(self):
        # The coverage TestSearchWildcardGuard's scanner cannot give this path
        # any more: its terms are module constants, not inline literals. Under
        # searchWildcardsEnabled a term without '*' is an EXACT match and finds
        # nothing, which would blank every traffic tool at once now that they
        # all come through here (ADR 094 + 109).
        from zbbx_mcp.fetch import TRAFFIC_IN_KEY_SEARCH, TRAFFIC_OUT_KEY_SEARCH
        for term in (TRAFFIC_IN_KEY_SEARCH, TRAFFIC_OUT_KEY_SEARCH):
            assert term.startswith("*") and term.endswith("*"), term
            assert "net.if." in term
