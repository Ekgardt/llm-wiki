"""Helpers shared by test_memory_queue.py and the test files importing them."""
from __future__ import annotations

from pathlib import Path

import queue_core
from queue_core import MemoryQueue


def _v3_queue(tmp_path: Path):
    candidate = tmp_path / "run" / "queue-v3.candidate.sqlite3"
    queue_core.initialize_queue_v3_candidate(candidate, source_v2=None)
    return MemoryQueue.from_v3_candidate(candidate, state_root=tmp_path)
