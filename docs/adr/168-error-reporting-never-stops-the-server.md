# 168. Error reporting never stops the server

## Status

Accepted

## Context

The server is often installed with `uvx`, which resolves dependencies afresh
from `pyproject.toml` rather than `uv.lock`. The range `sentry-sdk>=2.0`
therefore admits a new major release on any reinstall. sentry-sdk 3.x
rejects options that `setup_sentry` passed to `sentry_sdk.init`, and the
call was not guarded: with `SENTRY_DSN` set, the server failed at startup.

Error reporting is optional; a failure in it must not take the server down.

## Decision

- **Cap the range** at `sentry-sdk>=2.0,<3`. A new major SDK is adopted on
  purpose, not by a reinstall.
- **Never fail on telemetry.**
  - When `sentry_sdk.init` rejects an option, setup retries without it. The
    event scrubber stays, so nothing personal is sent.
  - The logging integration is optional; if it cannot be imported, Sentry
    starts without it.
  - Any other setup failure leaves the server running without Sentry and
    says so on stderr. `_sentry_enabled` stays false, so nothing tries to
    report through an SDK that never started.

## Consequences

- The server starts with any installed sentry-sdk, with or without
  `SENTRY_DSN`; checked against 2.x and 3.x.
- A broken Sentry setup costs error reports, not the server.
- `tests/test_sentry_init.py` covers no DSN, a rejected option, any init
  failure, a missing integration, and the version cap.
