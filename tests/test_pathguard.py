"""Filesystem-confinement guard tests (ADR 076).

Regression coverage for the CWE-22/CWE-73 path-traversal class described in the
yt-mcp advisory GHSA-99mq-fjjc-6v9j: caller-controlled file_path / output_dir
arguments must not read or write outside an allowlist of roots, symlinks must be
resolved before the check, and the check must be sibling-prefix-safe.
"""

import os

import pytest

from zbbx_mcp import utils
from zbbx_mcp.utils import (
    _within_roots,
    confined_input_path,
    confined_output_path,
    safe_output_path,
)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Pin the allowlist to a single known root so tests are isolated from the
    default roots (which include the system temp dir that holds tmp_path)."""
    root = tmp_path / "allowed"
    root.mkdir()
    real = os.path.realpath(root)
    monkeypatch.setattr(utils, "_allowed_roots", lambda: [real])
    return root


class TestWithinRoots:
    def test_child_allowed(self, sandbox):
        assert _within_roots(os.path.realpath(sandbox / "a.csv")) is True

    def test_root_itself_allowed(self, sandbox):
        assert _within_roots(os.path.realpath(sandbox)) is True

    def test_sibling_prefix_rejected(self, sandbox, tmp_path):
        # The old startswith() bug let '<root>-evil' pass a prefix match.
        sib = tmp_path / "allowed-evil"
        sib.mkdir()
        assert _within_roots(os.path.realpath(sib / "x")) is False

    def test_parent_rejected(self, sandbox, tmp_path):
        assert _within_roots(os.path.realpath(tmp_path)) is False


class TestConfinedInputPath:
    def test_reads_allowed_file(self, sandbox):
        p = sandbox / "in.csv"
        p.write_text("hi")
        assert confined_input_path(str(p)) == os.path.realpath(p)

    def test_rejects_outside_root(self, sandbox, tmp_path):
        outside = tmp_path / "secret.txt"
        outside.write_text("s")
        with pytest.raises(ValueError, match="outside the allowed roots"):
            confined_input_path(str(outside))

    def test_rejects_traversal_escape(self, sandbox):
        with pytest.raises(ValueError):
            confined_input_path(str(sandbox / ".." / "escape.txt"))

    def test_rejects_symlink_escape(self, sandbox, tmp_path):
        # A symlink planted inside an allowed root must not redirect the read out.
        secret = tmp_path / "secret.txt"
        secret.write_text("s")
        link = sandbox / "link.csv"
        os.symlink(secret, link)
        with pytest.raises(ValueError, match="outside"):
            confined_input_path(str(link))

    def test_rejects_missing_file(self, sandbox):
        with pytest.raises(ValueError, match="not found"):
            confined_input_path(str(sandbox / "nope.csv"))

    def test_rejects_empty(self, sandbox):
        with pytest.raises(ValueError, match="empty"):
            confined_input_path("")

    def test_size_cap(self, sandbox):
        p = sandbox / "big.csv"
        p.write_text("x" * 100)
        with pytest.raises(ValueError, match="too large"):
            confined_input_path(str(p), max_bytes=10)


class TestSafeOutputPath:
    def test_allows_bare_basename(self, sandbox):
        out = safe_output_path(str(sandbox), "report.xlsx")
        assert out == os.path.join(os.path.realpath(sandbox), "report.xlsx")

    def test_rejects_filename_traversal(self, sandbox):
        with pytest.raises(ValueError, match="filename"):
            safe_output_path(str(sandbox), "../evil.xlsx")

    def test_rejects_nested_filename(self, sandbox):
        with pytest.raises(ValueError, match="filename"):
            safe_output_path(str(sandbox), "sub/evil.xlsx")

    def test_rejects_dir_outside_root(self, sandbox, tmp_path):
        with pytest.raises(ValueError, match="allowed roots"):
            safe_output_path(str(tmp_path / "elsewhere"), "r.xlsx")


class TestConfinedOutputPath:
    def test_allows_and_creates_parent(self, sandbox):
        target = sandbox / "sub" / "out.csv"
        resolved = confined_output_path(str(target))
        assert resolved == os.path.realpath(target)
        assert os.path.isdir(os.path.dirname(resolved))

    def test_rejects_outside_root(self, sandbox, tmp_path):
        with pytest.raises(ValueError, match="allowed roots"):
            confined_output_path(str(tmp_path / "x" / "out.csv"))


class TestFileRootsEnv:
    def test_env_extends_allowed_roots(self, tmp_path, monkeypatch):
        extra = os.path.realpath(tmp_path / "custom")
        monkeypatch.setenv("ZBBX_FILE_ROOTS", str(tmp_path / "custom"))
        assert extra in utils._allowed_roots()

    def test_defaults_present_without_env(self, monkeypatch):
        monkeypatch.delenv("ZBBX_FILE_ROOTS", raising=False)
        roots = utils._allowed_roots()
        assert os.path.realpath(os.path.expanduser("~/Downloads")) in roots
