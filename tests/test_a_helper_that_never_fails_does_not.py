"""The plugin helpers exit 0 whatever the writer raises, and keep the reason.

See `docs/research/2026-09-17-a-helper-that-says-it-never-fails-does-not.md`.
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

HELPERS = [
    (
        "daily_log_append",
        "opencode_daily_append",
        {"slug": "app", "sessionId": "opencode-1", "block": "## [10:00:00] entry\nbody\n"},
    ),
    (
        "tool_breadcrumb_append",
        "opencode_tool_breadcrumb",
        {"slug": "app", "sessionId": "opencode-1", "tool": "edit", "target": "src/a.py"},
    ),
]


@pytest.fixture
def unwritable_vault(tmp_path, monkeypatch):
    """A vault whose daily directory is a file, so the writer refuses with a non-OS error."""
    import capture_diagnostics

    (tmp_path / "knowledge").mkdir()
    (tmp_path / "knowledge" / "daily").write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))
    trail = tmp_path / "capture-failures.jsonl"
    monkeypatch.setattr(capture_diagnostics, "FAILURE_LOG", trail)
    return trail


@pytest.mark.parametrize(("helper", "kind", "payload"), HELPERS)
def test_the_helper_exits_zero_and_writes_the_reason_down(
    unwritable_vault, monkeypatch, capsys, helper, kind, payload
):
    module = __import__(helper)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    status = module.main()

    records = [json.loads(line) for line in unwritable_vault.read_text("utf-8").splitlines()]
    assert (status, [record["kind"] for record in records]) == (0, [kind])
    assert f"{helper}: write failed:" in capsys.readouterr().err
