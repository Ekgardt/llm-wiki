"""Tests for memory_queue.py — persistent task queue.

Locks in:
1. enqueue/list_pending/mark_attempt round-trip.
2. Drain processes tasks via callback, marks success/failure correctly.
3. Failed tasks increment attempt counter; permanently-failed (>=5) skipped.
4. status() returns the expected shape for the metacognitive block.
5. Crash-safe: corrupted JSON files are skipped, not crash.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


@pytest.fixture
def clean_queue(tmp_path, monkeypatch):
    """Point memory_queue at a tmp dir for isolation."""
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path))
    # Force re-import so module-level env reads pick up monkeypatched value.
    if "memory_queue" in sys.modules:
        del sys.modules["memory_queue"]
    import memory_queue

    queue_path = tmp_path / "queue"
    queue_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(memory_queue, "_queue_dir", lambda: queue_path)
    return memory_queue


def test_enqueue_creates_json_file(clean_queue):
    task_id = clean_queue.enqueue("compile", {"daily": "2026-07-03.md"})
    assert task_id.startswith("20260703-") or task_id  # date prefix or any id

    pending = clean_queue.list_pending()
    assert len(pending) == 1
    assert pending[0]["type"] == "compile"
    assert pending[0]["payload"]["daily"] == "2026-07-03.md"
    assert pending[0]["attempts"] == 0


def test_mark_attempt_success_deletes_task(clean_queue):
    task_id = clean_queue.enqueue("query", {"prompt": "hello"})
    assert len(clean_queue.list_pending()) == 1

    clean_queue.mark_attempt(task_id, success=True)
    assert len(clean_queue.list_pending()) == 0


def test_mark_attempt_failure_increments_counter(clean_queue):
    task_id = clean_queue.enqueue("query", {"prompt": "hello"})
    clean_queue.mark_attempt(task_id, success=False)
    pending = clean_queue.list_pending()
    assert len(pending) == 1
    assert pending[0]["attempts"] == 1
    assert pending[0]["last_attempt_at"] is not None


def test_drain_processes_all_success(clean_queue):
    for i in range(3):
        clean_queue.enqueue("query", {"prompt": f"q{i}"})

    seen: list[str] = []
    def processor(task):
        seen.append(task["payload"]["prompt"])
        return True

    counts = clean_queue.drain_with(processor)
    assert counts == {"ok": 3, "failed": 0, "skipped": 0}
    assert sorted(seen) == ["q0", "q1", "q2"]
    assert len(clean_queue.list_pending()) == 0


def test_drain_marks_failed_and_continues(clean_queue):
    clean_queue.enqueue("query", {"prompt": "ok"})
    clean_queue.enqueue("query", {"prompt": "fail"})

    def processor(task):
        return task["payload"]["prompt"] != "fail"

    counts = clean_queue.drain_with(processor)
    assert counts["ok"] == 1
    assert counts["failed"] == 1
    pending = clean_queue.list_pending()
    assert len(pending) == 1
    assert pending[0]["payload"]["prompt"] == "fail"
    assert pending[0]["attempts"] == 1


def test_drain_skips_permanently_failed(clean_queue):
    task_id = clean_queue.enqueue("query", {"prompt": "stuck"})
    # Pre-fail 5 times to mark as permanently failed.
    for _ in range(5):
        clean_queue.mark_attempt(task_id, success=False)

    called = []
    def processor(task):
        called.append(task["id"])
        return True

    counts = clean_queue.drain_with(processor)
    assert counts["skipped"] == 1
    assert counts["ok"] == 0
    assert called == []  # processor never invoked


def test_drain_skips_recently_attempted(clean_queue):
    """Tasks attempted <60s ago are skipped (backoff)."""
    task_id = clean_queue.enqueue("query", {"prompt": "x"})
    clean_queue.mark_attempt(task_id, success=False)

    called = []
    def processor(task):
        called.append(task["id"])
        return True

    counts = clean_queue.drain_with(processor)
    # Task was just attempted (failed) → should be skipped on immediate retry.
    assert counts["skipped"] == 1
    assert called == []


def test_drain_respects_max_tasks_limit(clean_queue):
    for i in range(10):
        clean_queue.enqueue("query", {"prompt": f"q{i}"})

    counts = clean_queue.drain_with(lambda t: True, max_tasks=3)
    assert counts["ok"] == 3
    assert len(clean_queue.list_pending()) == 7


def test_status_returns_expected_shape(clean_queue):
    clean_queue.enqueue("compile", {"daily": "2026-07-03.md"})
    clean_queue.enqueue("query", {"prompt": "x"})
    task_id = clean_queue.enqueue("query", {"prompt": "stuck"})
    for _ in range(5):
        clean_queue.mark_attempt(task_id, success=False)

    s = clean_queue.status()
    assert s["pending_total"] == 3
    assert s["by_type"]["compile"] == 1
    assert s["by_type"]["query"] == 2
    assert s["permanently_failed"] == 1
    assert "queue_dir" in s


def test_list_pending_skips_corrupt_json(clean_queue, tmp_path):
    """A corrupted queue file must not crash list_pending."""
    clean_queue.enqueue("query", {"prompt": "good"})
    # Drop a garbage file in the same queue dir.
    queue_dir = tmp_path / "queue"
    (queue_dir / "garbage.json").write_text("{not valid json", encoding="utf-8")

    pending = clean_queue.list_pending()
    assert len(pending) == 1
    assert pending[0]["payload"]["prompt"] == "good"


def test_list_pending_filters_by_age(clean_queue):
    """max_age_days filters out ancient tasks."""
    clean_queue.enqueue("query", {"prompt": "fresh"})
    # Manually write an old task.
    queue_dir = clean_queue._queue_dir()
    queue_dir.mkdir(parents=True, exist_ok=True)
    old_task = {
        "id": "old-task",
        "type": "query",
        "enqueued_at": "2020-01-01T00:00:00",
        "attempts": 0,
        "last_attempt_at": None,
        "payload": {"prompt": "ancient"},
    }
    (queue_dir / "old-task.json").write_text(
        json.dumps(old_task), encoding="utf-8"
    )

    fresh_only = clean_queue.list_pending(max_age_days=30)
    assert len(fresh_only) == 1
    assert fresh_only[0]["payload"]["prompt"] == "fresh"

    all_tasks = clean_queue.list_pending()
    assert len(all_tasks) == 2
