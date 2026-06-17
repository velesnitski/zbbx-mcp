"""Tests for scripts/sync-mcp-label.py pure logic (ADR 061).

The script lives in scripts/ with a hyphenated name, so it's loaded from
its file path. The version lookup is dependency-injected, so these tests
never spawn a subprocess or touch ~/.claude.json.
"""

import importlib.util
import pathlib

import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "sync-mcp-label.py"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("sync_mcp_label", _SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class TestParseSemver:
    def test_bare_version(self, mod):
        assert mod.parse_semver("1.14.0") == "1.14.0"

    def test_extracts_from_noisy_output(self, mod):
        assert mod.parse_semver("Resolved 56 packages\n1.15.0\n") == "1.15.0"

    def test_keeps_prerelease_suffix(self, mod):
        assert mod.parse_semver("0.0.0+unknown") == "0.0.0+unknown"

    def test_no_version(self, mod):
        assert mod.parse_semver("no numbers here") == ""


class TestEntryMatches:
    def test_fragment_in_args(self, mod):
        entry = {"command": "uv", "args": ["run", "--directory", "/x/zbbx-mcp", "zbbx-mcp"]}
        assert mod.entry_matches(entry) is True

    def test_fragment_in_command(self, mod):
        assert mod.entry_matches({"command": "/opt/zbbx-mcp/bin/zbbx-mcp", "args": []}) is True

    def test_unrelated_entry(self, mod):
        assert mod.entry_matches({"command": "uv", "args": ["run", "slk-mcp"]}) is False

    def test_non_dict(self, mod):
        assert mod.entry_matches("nope") is False


class TestWiredDirectory:
    def test_extracts_directory(self, mod):
        assert mod._wired_directory(["run", "--directory", "/x/zbbx-mcp", "zbbx-mcp"]) == "/x/zbbx-mcp"

    def test_absent(self, mod):
        assert mod._wired_directory(["run", "zbbx-mcp"]) == ""

    def test_dangling_flag(self, mod):
        assert mod._wired_directory(["run", "--directory"]) == ""


class TestVersionFromPyproject:
    def test_reads_project_version(self, mod, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "zbbx-mcp"\nversion = "1.15.0"\n'
        )
        assert mod.version_from_pyproject(str(tmp_path)) == "1.15.0"

    def test_missing_file(self, mod, tmp_path):
        assert mod.version_from_pyproject(str(tmp_path)) == ""


class TestMcpContainers:
    def test_root_and_projects(self, mod):
        cfg = {
            "mcpServers": {"a": {}},
            "projects": {"/p1": {"mcpServers": {"b": {}}}, "/p2": {"other": 1}},
        }
        conts = mod.mcp_containers(cfg)
        assert len(conts) == 2  # root + p1; p2 has no mcpServers


class TestRenameIn:
    def _container(self):
        return {
            "zabbix": {"command": "uv", "args": ["run", "--directory", "/x/zbbx-mcp", "zbbx-mcp"]},
            "youtrack": {"command": "uv", "args": ["run", "yt-mcp"]},
        }

    def test_renames_to_versioned_key(self, mod):
        c = self._container()
        changed = mod.rename_in(c, get_version=lambda cmd, args: "1.15.0")
        assert changed is True
        assert "zabbix v1.15.0" in c
        assert "zabbix" not in c
        assert "youtrack" in c  # untouched

    def test_preserves_insertion_order(self, mod):
        c = self._container()
        mod.rename_in(c, get_version=lambda cmd, args: "1.15.0")
        assert list(c.keys()) == ["zabbix v1.15.0", "youtrack"]

    def test_idempotent_when_already_current(self, mod):
        c = {"zabbix v1.15.0": {"command": "uv", "args": ["run", "zbbx-mcp"]}}
        assert mod.rename_in(c, get_version=lambda cmd, args: "1.15.0") is False

    def test_skips_when_no_version(self, mod):
        c = self._container()
        assert mod.rename_in(c, get_version=lambda cmd, args: "") is False
        assert "zabbix" in c  # unchanged
