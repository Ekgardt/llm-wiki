"""Opening the legacy queue takes the write lock only when it has work to do.

docs/research/2026-09-25-opening-the-legacy-queue-takes-no-write-lock.md
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from memory_queue import MemoryQueue  # noqa: E402


def test_a_queue_opens_while_another_connection_holds_the_write_lock(tmp_path):
    MemoryQueue(tmp_path)
    holder = sqlite3.connect(tmp_path / "run" / "queue.sqlite3", isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        queue = MemoryQueue(tmp_path)
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    assert queue.db_path.is_file()
