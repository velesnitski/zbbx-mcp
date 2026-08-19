"""Structured logging and analytics.

Logging (errors/warnings):
- Always to stderr in JSON
- Always to ~/.zbbx-mcp/zbbx-mcp.log (override with ZABBIX_LOG_FILE)

Analytics (every tool call):
- Always to ~/.zbbx-mcp/analytics.log (override with ZABBIX_ANALYTICS_FILE)
- Sentry breadcrumbs (if SENTRY_DSN is set)

Each installation gets a persistent instance_id (UUID).
"""

import functools
import json
import logging
import os
import re
import sys
import time
import uuid
from pathlib import Path

from zbbx_mcp import __version__

_INSTANCE_DIR = Path.home() / ".zbbx-mcp"
_INSTANCE_ID_FILE = _INSTANCE_DIR / "instance_id"
_ANALYTICS_FILE = _INSTANCE_DIR / "analytics.log"

_sentry_enabled = False  # set True by setup_sentry()

# Keys to extract from tool params for analytics (safe, non-sensitive)
_ANALYTICS_KEYS = frozenset({
    "query", "host_id", "group", "instance", "search",
    "max_results", "limit", "severity_min", "product",
    "tier", "country", "sort_by", "threshold", "hours",
})


def _get_instance_id() -> str:
    """Get or create a persistent instance UUID."""
    try:
        if _INSTANCE_ID_FILE.exists():
            return _INSTANCE_ID_FILE.read_text().strip()
        _INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
        instance_id = str(uuid.uuid4())[:8]
        _INSTANCE_ID_FILE.write_text(instance_id)
        return instance_id
    except OSError:
        return "unknown"


INSTANCE_ID = _get_instance_id()

# Analytics logger (separate from error logger)
_analytics_logger: logging.Logger | None = None


class JSONFormatter(logging.Formatter):
    """Structured JSON log formatter."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%SZ"),
            "level": record.levelname.lower(),
            "msg": record.getMessage(),
            "instance": INSTANCE_ID,
        }
        for key in ("tool", "duration_ms", "error_type", "status"):
            val = getattr(record, key, None)
            if val is not None:
                entry[key] = val
        if record.exc_info and record.exc_info[1]:
            entry["exception"] = str(record.exc_info[1])
        return json.dumps(entry, ensure_ascii=False)


class AnalyticsFormatter(logging.Formatter):
    """Compact JSON formatter for analytics events."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%SZ"),
            "tool": getattr(record, "tool", "?"),
            "duration_ms": getattr(record, "duration_ms", 0),
            "status": getattr(record, "status", "ok"),
            "instance": INSTANCE_ID,
        }
        params = getattr(record, "params", None)
        if params:
            entry["params"] = params
        response_size = getattr(record, "response_size", None)
        if response_size is not None:
            entry["response_size"] = response_size
        error_detail = getattr(record, "error_detail", None)
        if error_detail:
            entry["error"] = error_detail
        return json.dumps(entry, ensure_ascii=False)


def setup_logging() -> logging.Logger:
    """Configure error logging. Call once at startup."""
    logger = logging.getLogger("zbbx_mcp")
    logger.setLevel(logging.INFO)

    # Stderr handler (always on)
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(JSONFormatter())
    logger.addHandler(stderr_handler)

    # File handler (default: ~/.zbbx-mcp/zbbx-mcp.log)
    log_file = os.environ.get("ZABBIX_LOG_FILE", str(_INSTANCE_DIR / "zbbx-mcp.log"))
    try:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(JSONFormatter())
        logger.addHandler(file_handler)
    except OSError:
        pass

    # Analytics logger (separate file, separate logger)
    global _analytics_logger
    _analytics_logger = logging.getLogger("zbbx_mcp.analytics")
    _analytics_logger.setLevel(logging.INFO)
    _analytics_logger.propagate = False
    try:
        analytics_file = os.environ.get("ZABBIX_ANALYTICS_FILE", str(_ANALYTICS_FILE))
        Path(analytics_file).parent.mkdir(parents=True, exist_ok=True)
        ah = logging.FileHandler(analytics_file)
        ah.setFormatter(AnalyticsFormatter())
        _analytics_logger.addHandler(ah)
    except OSError:
        pass

    return logger


def setup_sentry() -> None:
    """Initialize Sentry if SENTRY_DSN is set."""
    dsn = os.environ.get("SENTRY_DSN")
    if not dsn:
        return

    import sentry_sdk
    from sentry_sdk.integrations.logging import LoggingIntegration

    sentry_sdk.init(
        dsn=dsn,
        release=f"zbbx-mcp@{__version__}",
        environment=os.environ.get("SENTRY_ENVIRONMENT", "production"),
        traces_sample_rate=0,
        send_default_pii=False,
        # Sentry's typed signature differs by version (Event/dict, hint type);
        # _scrub_event accepts/returns dict which is compatible at runtime.
        before_send=_scrub_event,  # type: ignore[arg-type]
        integrations=[
            LoggingIntegration(level=logging.WARNING, event_level=logging.ERROR),
        ],
    )
    sentry_sdk.set_tag("instance_id", INSTANCE_ID)
    global _sentry_enabled
    _sentry_enabled = True


_SENSITIVE_PATTERNS = ("token", "secret", "password", "dsn", "key", "auth", "credential")


# Anything address- or machine-shaped, redacted before an event leaves the
# process. Sentry is a third party: an error string that merely *mentions* a
# host is infrastructure disclosure, and error strings quote host names and
# addresses constantly ("connect to X failed", "no items on Y").
#
# Deliberately over-broad. Redacting a value that turned out to be harmless
# costs a little debugging detail; missing one ships infrastructure to a
# service outside this system. The trade is not symmetric.
_IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b|\b(?:[0-9a-fA-F]{1,4}:){2,}[0-9a-fA-F]{0,4}\b")
_HOSTISH_RE = re.compile(r"\b[a-z][a-z0-9]*(?:-[a-z0-9]+)+\b", re.IGNORECASE)
_CC_NUM_RE = re.compile(r"\b[a-z]{2}\d{2,6}\b", re.IGNORECASE)


def _deny_terms() -> tuple[str, ...]:
    """Deployment-specific terms from ZBBX_SENSITIVE_STRINGS, if configured.

    The same list the fixture guard uses (ADR 119). One configured list, two
    enforcement points: it keeps terms out of the repo, and out of Sentry.
    """
    raw = os.environ.get("ZBBX_SENSITIVE_STRINGS", "").strip()
    if not raw:
        return ()
    try:
        if os.path.isfile(raw):
            with open(raw) as fh:
                lines = fh.read().splitlines()
        else:
            lines = raw.split(",")
    except OSError:
        return ()
    return tuple(t.strip() for t in lines if t.strip() and not t.strip().startswith("#"))


def _scrub_value(val: str) -> str:
    """Redact sensitive content from a string bound for Sentry."""
    lower = val.lower()
    for pat in _SENSITIVE_PATTERNS:
        if pat in lower:
            return "[REDACTED]"          # a credential: drop the whole string
    for term in _deny_terms():
        if term.lower() in lower:
            return "[REDACTED]"
    # Addresses and machine-shaped names are replaced in place, so the shape of
    # the error survives for debugging while the identifiers do not.
    val = _IP_RE.sub("[IP]", val)
    val = _HOSTISH_RE.sub(
        lambda m: m.group(0) if m.group(0).lower() in _HOSTISH_ALLOW else "[HOST]",
        val)
    # Compound host names put the sibling in a bare trailing token with no
    # hyphen ("<parent> xx0000"), so the rule above misses exactly the half
    # that identifies the machine. Two letters then digits is narrow enough to
    # leave sha256 / http2 / utf8 alone.
    return _CC_NUM_RE.sub("[HOST]", val)


# Ordinary hyphenated words that appear in error text and name nothing.
_HOSTISH_ALLOW = frozenset({
    "read-only", "not-found", "rate-limit", "time-out", "timed-out",
    "json-rpc", "content-type", "user-agent", "max-results", "e-mail",
    "well-known", "up-to-date", "self-signed", "multi-instance",
})


def _scrub_nested(value: object, _depth: int = 0) -> object:
    """Apply `_scrub_value` to every string inside a nested structure.

    Sentry `extra` payloads are arbitrary JSON, so an identifier can sit at any
    depth. Bounded recursion: a hostile or cyclic structure must not turn error
    reporting into a hang.
    """
    if isinstance(value, str):
        return _scrub_value(value)          # scrubbed at any depth
    if _depth > 6:
        # Past the cap, drop the branch rather than pass it through: a guard
        # that fails open is not a guard.
        return "[TRUNCATED]"
    if isinstance(value, dict):
        return {k: _scrub_nested(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub_nested(v, _depth + 1) for v in value]
    return value


def _scrub_event(event: dict, hint: dict) -> dict:
    """Remove sensitive data before sending to Sentry."""
    # Scrub extra fields: the key name AND the value. Keying alone assumed a
    # sensitive value always sits under a revealing name, but a host address
    # under `target` or `arg` is just as identifying as one under `host`.
    if "extra" in event:
        for key in list(event["extra"].keys()):
            if any(s in key.lower() for s in _SENSITIVE_PATTERNS):
                event["extra"][key] = "[REDACTED]"
            else:
                event["extra"][key] = _scrub_nested(event["extra"][key])
    # Scrub exception messages
    if "exception" in event:
        for exc in event.get("exception", {}).get("values", []):
            val = exc.get("value", "")
            if isinstance(val, str):
                exc["value"] = _scrub_value(val)
    # Scrub breadcrumbs
    for bc in event.get("breadcrumbs", {}).get("values", []):
        data = bc.get("data", {})
        if "error" in data and isinstance(data["error"], str):
            data["error"] = _scrub_value(data["error"])
    return event


def _extract_params(kwargs: dict) -> dict:
    """Extract safe params for analytics logging."""
    return {k: v for k, v in kwargs.items() if k in _ANALYTICS_KEYS and v}


def _add_sentry_breadcrumb(
    tool: str, params: dict, duration_ms: int, status: str,
    response_size: int = 0, error_detail: str = "",
) -> None:
    """Add tool call as Sentry breadcrumb (visible in error context)."""
    if not _sentry_enabled:
        return
    import sentry_sdk
    data: dict = {"params": params, "duration_ms": duration_ms, "status": status}
    if response_size:
        data["response_size"] = response_size
    if error_detail:
        data["error"] = error_detail
    sentry_sdk.add_breadcrumb(
        category="tool",
        message=tool,
        data=data,
        level="info" if status == "ok" else "error",
    )


def logged(func):
    """Decorator that logs every tool call to analytics + Sentry breadcrumbs.

    Captures:
    - Tool name and safe params (for usage stats)
    - Duration in ms
    - Response size in chars (for context overflow detection)
    - Error details for failed calls
    """

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        tool_name = func.__name__
        params = _extract_params(kwargs)
        start = time.monotonic()
        status = "ok"
        response_size = 0
        error_detail = ""

        try:
            result = await func(*args, **kwargs)
            if isinstance(result, str):
                response_size = len(result)
            return result
        except Exception as exc:
            status = "error"
            error_detail = str(exc)[:200]
            if _sentry_enabled:
                import sentry_sdk
                with sentry_sdk.new_scope() as scope:
                    scope.set_tag("tool", tool_name)
                    scope.set_context("tool_call", {"params": params})
                    sentry_sdk.capture_exception(exc)
            raise
        finally:
            duration_ms = int((time.monotonic() - start) * 1000)

            if _analytics_logger:
                _analytics_logger.info(
                    tool_name,
                    extra={
                        "tool": tool_name,
                        "params": params,
                        "duration_ms": duration_ms,
                        "status": status,
                        "response_size": response_size,
                        "error_detail": error_detail,
                    },
                )

            _add_sentry_breadcrumb(tool_name, params, duration_ms, status, response_size, error_detail)

    return wrapper
