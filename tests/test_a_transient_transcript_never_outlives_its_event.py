"""A transcript copy made for one event is removed even when its intent is not published.

See docs/research/2026-09-25-a-transient-transcript-never-outlives-its-event.md.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import integration_adapter
import pytest


def test_a_failed_publication_leaves_no_copy_behind(tmp_path: Path, monkeypatch) -> None:
    vault, state_root, project = tmp_path / "vault", tmp_path / "state", tmp_path / "project"
    (vault / "knowledge" / "projects").mkdir(parents=True)
    project.mkdir()
    monkeypatch.setattr(integration_adapter, "ROOT", vault)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setattr(integration_adapter, "STATE_ROOT", state_root)
    monkeypatch.setattr(
        integration_adapter, "_run_delegate", lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr="")
    )

    def refuse(*_args):
        raise OSError("queue unavailable")

    monkeypatch.setattr(integration_adapter, "_publish_durable_capture_intent", refuse)
    envelope = integration_adapter.normalize_event(
        "opencode", "session_end", {"directory": str(project), "sessionId": "s1", "transcript_text": "private words"}
    )

    with pytest.raises(OSError, match="queue unavailable"):
        integration_adapter.ingest_event(envelope, trigger="opencode-idle")

    assert list((state_root / "cache" / "transient-transcripts").glob("*")) == []
