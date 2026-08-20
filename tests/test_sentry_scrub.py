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

from zbbx_mcp.logging import _scrub_event, _scrub_nested, _scrub_value


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
        "host srv-tf9001 unreachable",
        "edge-bv9001 returned no items",
    ])
    def test_hyphenated_names_go(self, text):
        assert "[HOST]" in _scrub_value(text)

    def test_the_compound_sibling_goes_too(self):
        """The half that actually identifies the machine.

        A compound name puts the sibling in a bare trailing token with no
        hyphen, so a hyphen-based rule removes the parent and leaves the
        sibling — the more specific of the two.
        """
        out = _scrub_value("srv-aq9001 aq9002 has no items")
        assert "aq9002" not in out
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
    assert _scrub_value("srv-tf9001") != "srv-tf9001"
    assert _scrub_value("192.0.2.1") != "192.0.2.1"


class TestExtraFields:
    """`extra` was scrubbed by key name only.

    That assumed a sensitive value always sits under a revealing name. A host
    address under `target` or `arg` is exactly as identifying as one under
    `host`, and nothing was looking at values.
    """

    def test_a_value_under_an_innocuous_key_is_scrubbed(self):
        ev = {"extra": {"target": "192.0.2.10", "note": "srv-tf9001 down"}}
        out = _scrub_event(ev, {})
        assert out["extra"]["target"] == "[IP]"
        assert "[HOST]" in out["extra"]["note"]

    def test_a_revealing_key_still_drops_the_whole_value(self):
        ev = {"extra": {"api_token": "abc123"}}
        assert _scrub_event(ev, {})["extra"]["api_token"] == "[REDACTED]"

    def test_nested_structures_are_reached(self):
        ev = {"extra": {"args": {"inner": ["192.0.2.1", {"deep": "edge-bv9001"}]}}}
        out = _scrub_event(ev, {})["extra"]["args"]["inner"]
        assert out[0] == "[IP]"
        assert out[1]["deep"] == "[HOST]"

    def test_non_strings_survive(self):
        # Counts and flags are what make a report useful; only identifiers go.
        out = _scrub_nested({"n": 5, "ok": True, "none": None})
        assert out == {"n": 5, "ok": True, "none": None}

    def test_the_depth_guard_fails_closed(self):
        """A guard that fails open is not a guard.

        Strings are scrubbed at any depth; only an over-deep container is
        dropped, so nothing identifying rides out past the cap.
        """
        deep = {"a": {"b": {"c": {"d": {"e": {"f": {"g": "192.0.2.1"}}}}}}}
        assert "192.0.2.1" not in repr(_scrub_nested(deep))
        beyond = {"a": {"b": {"c": {"d": {"e": {"f": {"g": {"h": {"i": 1}}}}}}}}}
        assert "[TRUNCATED]" in repr(_scrub_nested(beyond))
