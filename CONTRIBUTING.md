# Contributing to zbbx-mcp

Thanks for your interest in contributing!

## Quick start

```bash
git clone https://github.com/velesnitski/zbbx-mcp.git
cd zbbx-mcp
python -m venv .venv && source .venv/bin/activate
pip install -e ".[test]"
pytest
```

## Development workflow

1. Fork the repo and create a branch from `dev`
2. Make your changes
3. Add tests if adding new tools
4. Run `pytest` — all tests must pass
5. Open a PR against `dev`

## Code style

- Python 3.10+
- `ruff check src tests` must pass (run in CI)
- `mypy src/zbbx_mcp` must pass on core modules (run in CI; `tools/` is
  currently excluded — see `pyproject.toml [tool.mypy]`)
- `pytest --cov=zbbx_mcp --cov-fail-under=15` must pass (run in CI)
- Use `asyncio.gather` for independent API calls
- Every tool gets an `instance: str = ""` parameter

## Sensitive content (public-repo hygiene)

This is a public repository. Every commit, ADR, and CHANGELOG entry is
indexed and searchable. **Do not commit any of the following**:

- **Real product or company names** — use `<product>` or generic
  placeholders in examples
- **Real hostnames** that follow your fleet's naming convention — use
  `host-a`, `edge-de01`, `parent child` as placeholders
- **Protocol names** specific to your service stack
- **ISP / carrier names** specific to your infrastructure
- **Non-ASCII characters** — keep all source ASCII-only. Localised data
  (XLSX headers, error strings) belongs in env vars or runtime input,
  not source
- **Internal jargon, ticket IDs, or codenames** that reveal what the
  fleet actually does

### Reproducible scan

Before committing, run:

```bash
# Cyrillic / non-ASCII letters
grep -rnE '[^\x00-\x7F]' src/ tests/ docs/ README.md CHANGELOG.md \
    --include='*.py' --include='*.md' --include='*.toml'

# Generic placeholder for project-specific banned strings — adapt the
# regex to your operator's actual sensitive terms before relying on it
grep -rniE 'your-product-here|your-hostname-pattern|...' src/ tests/ docs/
```

Both commands should print nothing and exit with code 1. The full
banned-string list for this repo lives in operator memory (not the
public source) — ask before adding new sensitive terms or templates.

## Adding a new tool

1. Add the function in the appropriate module under `src/zbbx_mcp/tools/`
2. Wrap with `@mcp.tool()` decorator
3. Update `WRITE_TOOLS` in `__init__.py` if the tool modifies data
4. Update `tests/test_registration.py` with the new tool name and count
5. Update `README.md` tool table

## Reporting bugs

Open an issue with:
- What you did
- What you expected
- What happened (include error message if available)
- Your Zabbix version

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
