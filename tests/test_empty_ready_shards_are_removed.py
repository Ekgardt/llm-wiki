"""Empty ready shards go (C-13). The quarantine settle of the same note was removed:
docs/research/2026-09-26-a-spent-attempt-keeps-its-state.md.
"""

from __future__ import annotations

from pathlib import Path

import reclaim_runtime_state


def test_empty_shards_under_ready_are_removed_too(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(reclaim_runtime_state, "STATE_ROOT", tmp_path)
    for stage in ("pending", "ready"):
        (tmp_path / "run/capture-intents" / stage / "0a").mkdir(parents=True)

    assert reclaim_runtime_state.remove_empty_intent_shards() == 2
