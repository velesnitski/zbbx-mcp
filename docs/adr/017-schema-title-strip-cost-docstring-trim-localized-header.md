# ADR 017: Schema-title strip, cost-tool docstring trim, env-driven localised XLSX header

**Status:** Accepted
**Date:** 2026-05-04

## Problem

A per-tool token-footprint audit on the 154-tool catalog turned up
three issues left over after ADRs 015 and 016:

1. **`title` field on every JSON-schema parameter is pure overhead.**
   FastMCP auto-generates titles like `"Max Results"` for the
   ``max_results`` parameter for UI rendering. The property *key*
   already conveys the parameter name to the LLM, and Anthropic's
   tool-use API does not consume the title. Audit measured **638
   title fields = ~12.5k chars (~3k tokens) = 22% of total schema
   overhead** at full tier.
2. **Four cost-tool docstrings predate the ADR-013 trim discipline.**
   `export_cost_audit` (746 chars), `import_from_xlsx` (643),
   `analyze_cost_import` (382), `import_cluster_ip_fees` (362). Plus
   two mid-tier offenders, `get_stale_items` (264) and
   `get_trigger_timeline` (190). All carry multi-paragraph mode
   explainers / format examples / decision rationale that belongs in
   ADRs.
3. **A leftover non-ASCII literal in `costs.py`.** A full-tree sweep
   found a single localised substring used to match a non-English
   column header in `import_from_xlsx`. The literal had survived
   earlier audits and was still present on the public branch.

## Decision

### Schema-title strip in `_compact_descriptions`

The function (which already strips ``Args:`` from each tool
description) gains a second pass:

```python
for spec in props.values():
    if isinstance(spec, dict) and "title" in spec:
        del spec["title"]
```

Runs on the same `ZABBIX_COMPACT_TOOLS=true` gate (the default).
When the operator opts out (`ZABBIX_COMPACT_TOOLS=false`) the titles
come back — preserves the escape hatch for any client that depends
on them for UI rendering.

### Cost-tool and trigger-timeline docstring trim

Six docstrings collapsed from multi-paragraph prose to one summary
line plus the `Args:` block, with an ADR-number cross-reference for
readers who want the long form. Same shape as the ADR-015 trim. The
four cost tools cite ADRs 002, 004, 005, 009 where their decision
context already lives.

### Env-driven localised XLSX header

`import_from_xlsx`'s sheet detector matched a hardcoded "ip server"
English header *or* a hardcoded Cyrillic equivalent. The Cyrillic
literal is removed; a new env var `ZABBIX_BILLING_IP_HEADER` carries
any localised substring the operator wants to match, with a default
of empty (English-only matching). Localised workbooks keep working
by setting the env var; the public source carries no foreign
characters.

## Test approach

No new unit tests in this commit — all three changes are
configuration-level:

- The schema-title strip removes a redundant field; existing
  `test_server.py` `test_tool_has_description` still asserts that
  every tool has a non-empty description (unchanged), and
  `test_tools_list` still asserts the 154 tool count (unchanged).
- The docstring trims do not touch any pure-helper logic; the 332
  pre-existing tests all pass.
- The XLSX-header env var is matched at runtime via
  ``os.environ.get``; the change preserves the English default and
  does not rely on a new code path under test.

## Consequences

- 332 tests pass (unchanged).
- Tool count stays at 154; `WRITE_TOOLS` unchanged.
- Compact-mode handshake at full tier drops from ~104k chars to
  ~89k chars (**~30k → ~25k tokens, –17%**). Every other tier sees
  the same proportional drop:
    - core: ~5k tokens (unchanged in headline; -2k chars internally)
    - ops: ~11k → ~9k tokens
    - finance: ~10k → ~8k tokens
    - reports: ~13k → ~11k tokens
- New env var `ZABBIX_BILLING_IP_HEADER` is documented in README;
  unset behaviour matches what users who only have English headers
  see today.
- The public `src/` tree now contains zero Cyrillic characters.

## Not included

- Schema `$ref` deduplication for repeated parameters
  (`instance: str = ""` × 154, `max_results × 80`, etc.). MCP
  requires each tool's `inputSchema` to be self-contained; a
  shared `$defs` block would have to repeat per-tool, costing more
  than it saves.
- A meta-tool dispatcher. Same trade-off as in ADR 016: kills LLM
  autonomy.
- Stripping `title` even when `ZABBIX_COMPACT_TOOLS=false`. The
  expanded view is opt-in; respecting it for clients that may use
  `title` for form rendering is the safer default.
- The Cyrillic word in commit message `bdffaf25` ("Release v1.0.0")
  is still on the public history. A `git filter-repo` rewrite was
  proposed and is awaiting authorisation.
