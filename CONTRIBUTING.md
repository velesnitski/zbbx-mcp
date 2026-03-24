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
- No external linter enforced — just be consistent with existing code
- Use `asyncio.gather` for independent API calls
- Every tool gets an `instance: str = ""` parameter

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
