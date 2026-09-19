"""A migration that died between the insert and the unlink can finish next time.

`_import_legacy_record` inserts, then compares all ten columns of the row it read
back. Six of them move as soon as a worker claims the task, so a crash between
the commit and `source.unlink()` left a record whose next import always answered
`legacy_import_conflict` — on every session start, for ever, on a vault that has
not migrated yet.

See `docs/research/2026-09-18-an-import-that-already-happened-is-not-a-conflict.md`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import memory_queue  # noqa: E402
from memory_queue import MemoryQueue, QueueOperationError  # noqa: E402


def _record(task_id: str = "legacy-1", **changes: object) -> dict[str, object]:
    task: dict[str, object] = {
        "id": task_id,
        "type": "query",
        "enqueued_at": "2026-07-01T12:00:00+00:00",
        "attempts": 0,
        "payload": {"prompt": "hello"},
    }
    task.update(changes)
    return task


def _staged(root: Path, name: str, task: object) -> Path:
    queue_dir = root / "run" / "queue"
    queue_dir.mkdir(parents=True, exist_ok=True)
    path = queue_dir / name
    path.write_text(json.dumps(task), encoding="utf-8")
    return path


def _crashed_after_the_insert(tmp_path: Path, record: dict[str, object]) -> Path:
    """The import commits and the staged file survives, as a killed run leaves it."""
    source = _staged(tmp_path, "legacy.json", record)
    memory_queue._import_legacy_record(MemoryQueue(tmp_path), record, source)
    return source


def test_a_record_whose_task_was_claimed_meanwhile_imports_again(
    tmp_path: Path,
) -> None:
    record = _record()
    source = _crashed_after_the_insert(tmp_path, record)
    queue = MemoryQueue(tmp_path)
    claimed = queue.claim("worker")

    imported = memory_queue._import_legacy_record(queue, record, source)

    assert (claimed.id, imported) == ("legacy-1", "legacy-1")


def test_a_different_task_holding_the_same_id_is_still_a_conflict(
    tmp_path: Path,
) -> None:
    source = _crashed_after_the_insert(tmp_path, _record())
    other = _record(payload={"prompt": "something else"})

    with pytest.raises(QueueOperationError, match="legacy_import_conflict"):
        memory_queue._import_legacy_record(MemoryQueue(tmp_path), other, source)


def test_the_whole_migration_finishes_after_such_a_crash(tmp_path: Path) -> None:
    record = _record()
    _crashed_after_the_insert(tmp_path, record)
    MemoryQueue(tmp_path).claim("worker")

    receipt = memory_queue.migrate_legacy_queue(tmp_path)

    assert (receipt.imported, receipt.quarantined) == (1, 0)
