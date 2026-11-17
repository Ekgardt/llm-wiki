"""Helpers shared by test_queue_v3_corruption.py and the test files importing them."""
from __future__ import annotations

from pathlib import Path

import markdown_transaction
import queue_core


def _queue(tmp_path: Path):
    coordinator = tmp_path / "run" / "markdown-transactions-v3.candidate.sqlite3"
    candidate = tmp_path / "run" / "queue-v3.candidate.sqlite3"
    markdown_transaction.initialize_coordinator_v3_candidate(
        coordinator, source_v2=None
    )
    queue_core.initialize_queue_v3_candidate(candidate, source_v2=None)
    return queue_core.MemoryQueue.from_v3_candidate(
        candidate, state_root=tmp_path
    )
