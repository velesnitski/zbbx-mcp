"""ADR 150: the domain CSV export is confined like every other writer.

It used to open a fixed path directly, skipping the root check and raising a
traceback when the folder was missing. Fixtures are the neutral ones of the
domains wire suite.
"""

from __future__ import annotations

from tests.test_domains_wire import _client, probes  # noqa: F401  (fixture re-export)
from tests.wiretest import run_tool
from zbbx_mcp.tools import domains


def test_the_write_goes_through_the_root_guard(probes, monkeypatch, tmp_path):  # noqa: F811
    # The temp dir is always an allowed root, so a refusal cannot be staged
    # with real paths here; assert that the guard is consulted and that its
    # refusal becomes a message, not a traceback or a file.
    seen = []

    def refuse(output_dir, filename):
        seen.append((output_dir, filename))
        raise ValueError(f"Output directory '{output_dir}' is not in the allowed roots.")

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(domains, "safe_output_path", refuse)
    out = run_tool(domains, "get_domain_status", _client(), format="csv")
    assert seen and seen[0][0] == "~/Downloads" and seen[0][1].startswith("domains-")
    assert out.startswith("Cannot write the CSV:") and "not in the allowed roots" in out
    assert not (tmp_path / "Downloads").exists()


def test_a_missing_folder_inside_the_roots_is_created(probes, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ZBBX_FILE_ROOTS", str(tmp_path))
    out = run_tool(domains, "get_domain_status", _client(), format="csv")
    files = list((tmp_path / "Downloads").glob("domains-*.csv"))
    assert len(files) == 1 and f"to `{files[0]}`" in out
