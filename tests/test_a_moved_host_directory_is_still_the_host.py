"""A host whose directory was moved with its own variable is still captured.

See `docs/research/2026-09-17-a-moved-host-directory-is-still-the-host.md`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

HOSTS = [("CLAUDE_CONFIG_DIR", "projects"), ("CODEX_HOME", "sessions")]


def _moved_transcript(tmp_path, monkeypatch, variable: str, inner: str) -> Path:
    moved = tmp_path / "moved-host"
    transcript = moved / inner / "project" / "session.jsonl"
    transcript.parent.mkdir(parents=True)
    # Bytes, not text: the product keeps a transcript's bytes as they are, and
    # `write_text` would make this line end in `\r\n` on Windows.
    transcript.write_bytes(b"a decision worth keeping\n")
    monkeypatch.setenv(variable, str(moved))
    return transcript


@pytest.mark.parametrize(("variable", "inner"), HOSTS)
def test_the_adapter_reads_a_transcript_from_the_moved_directory(
    tmp_path, monkeypatch, variable, inner
):
    import integration_adapter

    transcript = _moved_transcript(tmp_path, monkeypatch, variable, inner)

    assert integration_adapter._capture_path_evidence(str(transcript)) == (
        "a decision worth keeping\n"
    )


def test_a_directory_nobody_configured_is_still_refused(tmp_path):
    import integration_adapter

    stray = tmp_path / "elsewhere" / "projects" / "session.jsonl"
    stray.parent.mkdir(parents=True)
    stray.write_text("text\n", encoding="utf-8")

    with pytest.raises(PermissionError):
        integration_adapter._capture_path_evidence(str(stray))
