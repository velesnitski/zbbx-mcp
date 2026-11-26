"""Error reporting never stops the server (ADR 168)."""

import pathlib
import sys
import types

from zbbx_mcp import logging as zlog


def _fake_sdk(init):
    sdk = types.ModuleType("sentry_sdk")
    sdk.init = init
    sdk.tags = {}
    sdk.set_tag = lambda k, v: sdk.tags.__setitem__(k, v)
    return sdk


def _use(monkeypatch, sdk):
    monkeypatch.setitem(sys.modules, "sentry_sdk", sdk)
    monkeypatch.setenv("SENTRY_DSN", "https://key@example.invalid/1")
    monkeypatch.setattr(zlog, "_sentry_enabled", False)


def test_no_dsn_does_nothing(monkeypatch):
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    monkeypatch.setattr(zlog, "_sentry_enabled", False)
    zlog.setup_sentry()
    assert zlog._sentry_enabled is False


def test_sdk_rejecting_an_option_retries_without_it(monkeypatch):
    calls = []

    def init(**kw):
        calls.append(kw)
        if "send_default_pii" in kw:
            raise TypeError("Unknown option 'send_default_pii'")

    sdk = _fake_sdk(init)
    _use(monkeypatch, sdk)
    zlog.setup_sentry()
    assert len(calls) == 2
    assert "send_default_pii" not in calls[1]
    assert calls[1]["before_send"] is zlog._scrub_event
    assert "instance_id" in sdk.tags
    assert zlog._sentry_enabled is True


def test_any_init_failure_leaves_the_server_running(monkeypatch, capsys):
    def init(**kw):
        raise RuntimeError("boom")

    _use(monkeypatch, _fake_sdk(init))
    zlog.setup_sentry()
    assert "Sentry disabled" in capsys.readouterr().err
    assert zlog._sentry_enabled is False


def test_missing_logging_integration_is_tolerated(monkeypatch):
    calls = []
    sdk = _fake_sdk(lambda **kw: calls.append(kw))
    _use(monkeypatch, sdk)
    monkeypatch.setitem(sys.modules, "sentry_sdk.integrations", None)
    zlog.setup_sentry()
    assert len(calls) == 1
    assert "integrations" not in calls[0]
    assert zlog._sentry_enabled is True


def test_sentry_sdk_is_capped_below_3():
    text = (pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    assert '"sentry-sdk>=2.0,<3"' in text
