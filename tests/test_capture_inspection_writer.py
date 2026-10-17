"""Reading retained source files must not hold a SQLite cursor over their I/O."""
from __future__ import annotations

import sqlite3
import time
from contextlib import closing

import installed_memory_repair as repair
import pytest
from reliable_memory import OPERATIONAL_SCAN_BATCH_ROWS

from tests.test_breadcrumb_health import _runtime
from tests.test_breadcrumb_storage import _publish


class WriterCommitted(Exception):
    """Stop at the measured boundary, before intentionally absent fixture rows."""


def _larger_than_one_scan_batch(queue, identity):
    base = int(identity, 16)
    rows = [
        (f"{base + index + 1:064x}", f"run/capture-intents/pending/ff/{base + index + 1:064x}.json",
         "a" * 64, 2, "pending", "2000-01-01T00:00:00.000000+00:00")
        for index in range(OPERATIONAL_SCAN_BATCH_ROWS)
    ]
    with queue.connection() as database:
        database.executemany("INSERT INTO capture_intents VALUES (?,?,?,?,?,?)", rows)


def test_source_inspection_releases_the_read_cursor_before_file_io(tmp_path, monkeypatch):
    state, queue, coordinator = _runtime(tmp_path)
    publication = _publish(queue, coordinator)
    _larger_than_one_scan_batch(queue, publication.intent_id)
    validate = repair._validate_capture_intent

    def validate_while_writer_commits(row, root, *, deadline):
        assert row["intent_id"] == publication.intent_id
        validate(row, root, deadline=deadline)
        with closing(sqlite3.connect(queue.db_path, timeout=0)) as writer:
            writer.execute("UPDATE tasks SET priority=priority+1")
            writer.commit()
        raise WriterCommitted

    monkeypatch.setattr(repair, "_validate_capture_intent", validate_while_writer_commits)
    with queue.connection() as database, pytest.raises(WriterCommitted):
        repair._capture_intent_blockers(database, state, time.monotonic() + 30)
