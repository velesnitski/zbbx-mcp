# ADR 038: Server name carries the package version

**Status:** Accepted
**Date:** 2026-05-28

## Problem

Claude Code's `/mcp` panel renders each connected server's
`serverInfo.name` (returned by the MCP `initialize` response) and
nothing else — no version, no build, no commit. With a bare
`"zabbix"` name, the operator can't tell at a glance which
version is loaded:

- After a release, is the running server already on the new
  build, or still on the old one?
- During a rollout, which session is on which version?
- In a multi-instance setup, are all clients on the same release?

The Go MCP SDK has the same shape (`server.NewMCPServer(name,
version, ...)` — only `name` reaches the UI). Communities running
those servers concatenate the version into the name string so
the panel reads e.g. `slack v0.4.14  ✓ connected`. Same trick
fits cleanly here.

## Decision

Two small changes.

### 1. Resolve `__version__` from package metadata

`zbbx_mcp/__init__.py` previously hard-coded `__version__ = "1.6.0"`
— stale, and a maintenance hazard (we bump `pyproject.toml` every
release but the runtime string drifted). Switch to
`importlib.metadata.version("zbbx-mcp")` so the import-time value
always matches the installed distribution.

Fallback to `"0.0.0+unknown"` when the package isn't installed
(editable / `python -m` from a source checkout where the dist
metadata is missing) so imports keep working in dev sessions.

### 2. Embed version in `FastMCP` server name

`server.py` `create_server()` now constructs:

```python
from zbbx_mcp import __version__ as _zbbx_version
mcp = FastMCP(f"zabbix v{_zbbx_version}")
```

`FastMCP` propagates the constructor name to
`initialize.result.serverInfo.name`. After a server restart the
`/mcp` panel reads `zabbix v1.9.5  ✓ connected`.

### Compat note

Clients that compared `serverInfo.name` to a literal `"zabbix"`
must switch to `startswith("zabbix")` or parse out the version
suffix. The MCP smoke test
(`tests/test_server.py::TestServerStartup::test_initialize`)
was updated to use `startswith` — that's the only known consumer
in this repo.

## Test approach

The existing `test_initialize` JSON-RPC smoke test now asserts
`startswith("zabbix v")` instead of `== "zabbix"`. That single
change covers both the FastMCP-name plumbing and the
metadata-derived version (a failed `__version__` lookup would
make the string `"zabbix v0.0.0+unknown"`, which still passes
`startswith` but would be obvious in any actual `/mcp` panel).

No new tests added — the change is one display string plus a
metadata import; the existing smoke validates both ends.

## Consequences

- Tool count unchanged (161).
- Test count unchanged (488); one assertion relaxed
  (`==` → `startswith`).
- `WRITE_TOOLS` unchanged.
- No new env vars.
- **Visible change**: `/mcp` panel now shows the version inline.
- **Wire-compat change**: any external consumer comparing
  `serverInfo.name == "zabbix"` will break. The internal smoke
  was the only such consumer in this tree.

## Not included

- **Stripping the version from logged events.** The structured
  log line still says `"Starting zbbx-mcp"` (not the FastMCP
  name); no log changes needed.
- **Cli output / `--version` flag.** Out of scope — the
  question this ADR answers is "what does the running MCP
  server identify itself as to clients?"
- **Branding the name beyond version** (build hash, env tag).
  Premature; the version alone covers the question operators
  asked. Revisit if a multi-deployment-tag scenario surfaces.
- **Backporting the version-derivation pattern to other MCPs**
  (yt-mcp, zbbx-mcp's siblings). Each is a separate repo; copy
  the two-line change when the same UX gap surfaces there.
