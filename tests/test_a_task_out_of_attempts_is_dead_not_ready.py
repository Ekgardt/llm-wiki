"""A task that has spent every attempt must not keep the state of pending work.

`_failure_state` sent an ordinary failure back to `ready` whatever the attempt
count was, so a task failing on its last allowed attempt kept the state of
work still to do and the attempt count of work given up on. The worker will
not claim it — the budget is spent. `redrive` will not take it — redrive
requires a dead task. Twenty-three session classifications sat in that gap on
this vault, all eight attempts old, all failing for a reason since fixed.
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import memory_queue  # noqa: E402
import repair_exhausted_queue_tasks as repair  # noqa: E402


def _row(attempts: int) -> sqlite3.Row:
    database = sqlite3.connect(":memory:")
    database.row_factory = sqlite3.Row
    database.execute("CREATE TABLE t (attempts INTEGER)")
    database.execute("INSERT INTO t VALUES (?)", (attempts,))
    return database.execute("SELECT * FROM t").fetchone()


def _failure(**kwargs):
    return memory_queue.QueueFailure(error_code="processor_failed", **kwargs)


def test_the_last_allowed_attempt_ends_the_task():
    assert repair  # the repair ships with the fix that stops making this state

    assert memory_queue._failure_state(_row(8), _failure(), 8) == ("dead", 8)


def test_an_attempt_still_left_goes_back_to_ready():
    assert memory_queue._failure_state(_row(7), _failure(), 8) == ("ready", 7)


def test_a_blocked_capability_is_not_an_attempt_at_all():
    state, attempts = memory_queue._failure_state(
        _row(8), _failure(blocked_capability="provider"), 8
    )

    assert (state, attempts) == ("blocked", 7)


def test_the_repair_only_looks_at_tasks_with_no_attempts_left():
    database = sqlite3.connect(":memory:")
    database.row_factory = sqlite3.Row
    database.execute(
        "CREATE TABLE tasks (id TEXT, kind TEXT, attempts INTEGER, state TEXT, "
        "lease_token TEXT, error_code TEXT, updated_at TEXT)"
    )
    database.executemany(
        "INSERT INTO tasks VALUES (?,?,?,?,?,?,?)",
        [
            ("stranded", "flush", 8, "ready", None, "processor_failed", "2026-09-04"),
            ("retrying", "flush", 3, "ready", None, "processor_failed", "2026-09-04"),
            ("leased", "flush", 8, "ready", "token", "processor_failed", "2026-09-04"),
            ("fresh", "flush", 8, "ready", None, None, "2026-09-04"),
            ("done", "flush", 8, "succeeded", None, None, "2026-09-04"),
        ],
    )

    assert [row["id"] for row in repair.stranded_tasks(database, 8)] == ["stranded"]
