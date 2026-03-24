from zbbx_mcp.logging import (
    _scrub_event, _scrub_value, _extract_params,
    JSONFormatter, AnalyticsFormatter, _ANALYTICS_KEYS,
)


class TestScrubValue:
    def test_redacts_token(self):
        assert _scrub_value("invalid token abc123") == "[REDACTED]"

    def test_redacts_password(self):
        assert _scrub_value("wrong password for user") == "[REDACTED]"

    def test_redacts_auth(self):
        assert _scrub_value("auth failed: bad credential") == "[REDACTED]"

    def test_passes_safe_value(self):
        assert _scrub_value("Host not found") == "Host not found"

    def test_case_insensitive(self):
        assert _scrub_value("Invalid TOKEN") == "[REDACTED]"


class TestScrubEvent:
    def test_scrubs_extra_keys(self):
        event = {"extra": {"api_token": "secret123", "tool": "search_hosts"}}
        result = _scrub_event(event, {})
        assert result["extra"]["api_token"] == "[REDACTED]"
        assert result["extra"]["tool"] == "search_hosts"

    def test_scrubs_exception_values(self):
        event = {"exception": {"values": [{"value": "auth token expired"}]}}
        result = _scrub_event(event, {})
        assert result["exception"]["values"][0]["value"] == "[REDACTED]"

    def test_scrubs_breadcrumb_errors(self):
        event = {"breadcrumbs": {"values": [{"data": {"error": "bad token in request"}}]}}
        result = _scrub_event(event, {})
        assert result["breadcrumbs"]["values"][0]["data"]["error"] == "[REDACTED]"

    def test_passes_safe_breadcrumb(self):
        event = {"breadcrumbs": {"values": [{"data": {"error": "Host not found"}}]}}
        result = _scrub_event(event, {})
        assert result["breadcrumbs"]["values"][0]["data"]["error"] == "Host not found"

    def test_handles_missing_fields(self):
        event = {}
        result = _scrub_event(event, {})
        assert result == {}


class TestExtractParams:
    def test_extracts_safe_keys(self):
        kwargs = {"query": "web*", "host_id": "123", "webhook_url": "https://evil.com"}
        result = _extract_params(kwargs)
        assert "query" in result
        assert "host_id" in result
        assert "webhook_url" not in result

    def test_skips_empty(self):
        result = _extract_params({"query": "", "limit": 50})
        assert "query" not in result
        assert "limit" in result

    def test_analytics_keys_defined(self):
        assert "query" in _ANALYTICS_KEYS
        assert "instance" in _ANALYTICS_KEYS
        assert "webhook_url" not in _ANALYTICS_KEYS
