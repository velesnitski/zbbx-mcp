#!/usr/bin/env bash
# Mutation testing of the pure core (reading, budget, data, formatters,
# app_map, classify). Configuration lives in pyproject.toml [tool.mutmut];
# this script only runs it the same way every time and prints the scores.
#
#   scripts/mutation.sh            # full run, then per-module score
#   scripts/mutation.sh --scores   # scores of the last run only
#
# Needs the test extra (uv sync --extra test). Slow: minutes, not seconds.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ "${1:-}" != "--scores" ]]; then
    rm -rf mutants
    start=$(date +%s)
    uv run --extra test mutmut run --max-children "${MUTMUT_CHILDREN:-4}" || true
    echo "mutation run took $(( $(date +%s) - start ))s"
fi

printf "%-12s %7s %7s %8s %8s %7s\n" module mutants killed survived no-tests score
# Per-module score = killed / all mutants (a mutant no test reaches counts
# against the score: nothing would notice it).
uv run --extra test mutmut results --all true 2>/dev/null | awk '
    /: / {
        mod = $1; sub(/^zbbx_mcp\./, "", mod); sub(/\..*/, "", mod)
        status = $0; sub(/^[^:]*: /, "", status); total[mod]++
        if (status == "killed" || status == "timeout") killed[mod]++
        else if (status == "survived") survived[mod]++
        else if (status == "no tests") untested[mod]++
    }
    END {
        for (m in total) printf "%-12s %7d %7d %8d %8d %6.1f%%\n", m, total[m], killed[m], survived[m], untested[m], 100 * killed[m] / total[m]
    }' | sort
