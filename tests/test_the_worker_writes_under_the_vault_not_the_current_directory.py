"""Without `LLM_WIKI_ROOT` the worker's daily log is the vault's, wherever it was started.

`daily_log_path` and `manual_compile` defaulted the vault to the current
directory while the rest of the module uses the scripts' parent, so
`memory_queue.py work` started elsewhere appended flushed memory under that
directory and marked the task succeeded.
"""

from __future__ import annotations

from pathlib import Path

import memory_queue
import pytest
import queue_core


def test_the_daily_log_is_resolved_under_the_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LLM_WIKI_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    vault = Path(queue_core.__file__).resolve().parent.parent

    path = memory_queue.daily_log_path("2026-08-25")

    assert path == vault / "knowledge" / "daily" / "2026-08-25.md"
