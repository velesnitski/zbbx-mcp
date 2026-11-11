# 163. Headless mode — run any tool once from the command line

## Status

Accepted (v1.18.0)

## Context

The tools answer only inside an MCP session. A scheduled job — a daily
report, a nightly drift check, another bot polling for problems — could not
use what they already do (seasonal drop detection, frozen-problem filtering,
traffic verdicts, the report generators) without an MCP client, and
re-implementing them in each consumer would fork the logic. The sibling
Slack server solved the same problem with a headless mode (its ADR 136);
this follows its contract so the two read the same to a script.

## Decision

Two subcommands on the existing entry point: `zbbx-mcp tools [--json]`
lists the tools, `zbbx-mcp run <tool> [key=value …] [--args JSON|-] [--json]
[--timeout SECONDS]` calls one. The server and transports are unchanged.

- **In-process, nothing per tool.** The command builds the server with
  `create_server()` and calls `FastMCP.list_tools` / `FastMCP.call_tool`, the
  functions the protocol handlers call. Argument validation, the response
  compression and budget, the analytics wrapper and the `ZABBIX_READ_ONLY`,
  `DISABLED_TOOLS` and `ZABBIX_TIER` gating are therefore the client's: a
  write tool under read-only is not listed and cannot be run. A tool is
  reachable the day it is registered.
- **Arguments typed by the schema.** Pairs become integers, numbers,
  booleans or JSON according to the tool's input schema, looking through
  `anyOf` for optional parameters. An unknown key — in a pair or in the
  `--args` object — a malformed value or a missing required argument is a
  usage error that names what the tool takes. Pairs override `--args`; flags
  may sit anywhere.
- **Output for machines.** Plain text by default; `--json` prints one object
  with stable fields `tool`, `args`, `ok`, `text` and, when the call wrote
  any, `files`. `ok` is false when the tool raised or answered with the
  `Error…` text the tools use for failures (the rule the analytics apply,
  ADR 160).
- **Files from the gate, not from the text.** Report tools announce their
  output in several phrasings. Every write already resolves its path through
  one of the two confinement gates (`safe_output_path`,
  `confined_output_path`, ADR 076); those now record each path into a
  context variable when a caller is tracking, and the command lists the
  recorded paths that exist and were modified during the call. A guard test
  fails if a tool module writes a file without a gate, so the list stays
  complete.
- **Exit codes:** 0 success, 1 the tool reported an error or exceeded
  `--timeout` (default 300 s), 2 a usage or configuration error. For the
  length of the command our stderr handler is raised to WARNING and our
  logger is kept off the root handler FastMCP installs (a propagated record
  skips the root's level check), so stdout holds only the result and stderr
  only warnings and errors; the log file is unchanged and analytics lines
  carry `"via": "cli"`. All of it is restored afterwards, so `main()` can be
  called in-process.

## Test approach

Pure tests for the command-line parser, schema typing (including `anyOf`),
argument merging and every usage error. End-to-end through `headless.main`
with the real `create_server()` and only the Zabbix client replaced: tool
listing in both forms, a call through the full pipeline, plain output, a
tool error, a timeout, read-only hiding a write tool, an unknown tool, a
misspelt argument, missing configuration, and the analytics line marked
`via: cli`. The output gate is tested for recording, for ignoring paths
never written, and for being a no-op when nothing tracks.

## Consequences

A scheduler gets every tool without an MCP client and without a second
implementation. Each run builds the server and exits; anything kept between
runs is the caller's. The response budget applies as for a client, so a job
that wants full output sets `ZABBIX_RESPONSE_BUDGET=0`. The in-process call
skips the transport, which the subprocess and integration tests still cover.
