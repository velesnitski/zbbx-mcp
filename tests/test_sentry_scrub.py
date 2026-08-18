"""Nothing identifying leaves the process in an error report (ADR 123).

Sentry is outside this system. Error strings quote host names and addresses
constantly — "connect to X failed", "no items on Y" — so an exception message
is an egress channel, and it was scrubbing only credential words.

The rule here is deliberately over-broad. Redacting something harmless costs a
little debugging detail; missing something ships infrastructure to a third
party. The trade is not symmetric, so the tests below assert over-redaction is
acceptable and under-redaction is not.
"""

from __future__ import annotations

import pytest

from zbbx_mcp.logging import _scrub_value


class TestIdentifiersAreRemoved:
    @pytest.mark.parametrize(("text", "addr"), [
        ("connect to 192.0.2.10:10050 failed", "192.0.2.10"),
        ("timeout reaching 198.51.100.7", "198.51.100.7"),
        ("peer 2001:0db8:85a3:0000 unreachable", "2001:0db8:85a3:0000"),
    ])
    def test_addresses_go(self, text, addr):
        out = _scrub_value(text)
        assert "[IP]" in out
        assert addr not in out, "the address itself must not survive"

    @pytest.mark.parametrize("text", [
        "host srv-nl01 unreachable",
        "edge-de01 returned no items",
    ])
    def test_hyphenated_names_go(self, text):
        assert "[HOST]" in _scrub_value(text)

    def test_the_compound_sibling_goes_too(self):
        """The half that actually identifies the machine.

        A compound name puts the sibling in a bare trailing token with no
        hyphen, so a hyphen-based rule removes the parent and leaves the
        sibling — the more specific of the two.
        """
        out = _scrub_value("srv-us01 us02 has no items")
        assert "us02" not in out
        assert out.count("[HOST]") == 2

    def test_credentials_drop_the_whole_string(self):
        # A token can be anywhere in the message; partial redaction is not
        # safe when the secret's shape is unknown.
        assert _scrub_value("auth failed for token=abc123") == "[REDACTED]"


class TestOrdinaryTextSurvives:
    @pytest.mark.parametrize("text", [
        "read-only mode: write tools disabled",
        "request timed-out after 30s",
        "sha256 mismatch, http2 disabled, utf8 decode error",
        "content-type must be json-rpc",
    ])
    def test_kept_verbatim(self, text):
        # Over-redaction is tolerable, but not to the point of destroying every
        # message — an error nobody can read is its own failure.
        assert _scrub_value(text) == text


class TestDenyList:
    def test_configured_terms_drop_the_string(self, monkeypatch):
        monkeypatch.setenv("ZBBX_SENSITIVE_STRINGS", "widgetcorp,acme-internal")
        assert _scrub_value("failed talking to WidgetCorp api") == "[REDACTED]"

    def test_unset_changes_nothing(self, monkeypatch):
        monkeypatch.delenv("ZBBX_SENSITIVE_STRINGS", raising=False)
        assert _scrub_value("plain message") == "plain message"

    def test_a_file_path_is_accepted(self, monkeypatch, tmp_path):
        f = tmp_path / "terms.txt"
        f.write_text("# comment\nwidgetcorp\n\n")
        monkeypatch.setenv("ZBBX_SENSITIVE_STRINGS", str(f))
        assert _scrub_value("widgetcorp down") == "[REDACTED]"


def test_the_scrubber_is_not_vacuous():
    """A scrubber that redacts nothing would pass every test above by accident."""
    assert _scrub_value("srv-nl01") != "srv-nl01"
    assert _scrub_value("192.0.2.1") != "192.0.2.1"
