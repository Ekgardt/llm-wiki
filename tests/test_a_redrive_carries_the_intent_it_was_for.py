"""A redriven capture task must keep its intent, and get exactly one more round.

`claim_capture` selects only tasks that have a row in `capture_task_links`, and
a redrive copied the task without it — so a redriven capture task was invisible
to the capture worker for good. The sweeper that hands a task to an intent
looks for intents with *no* task, so an intent whose only task was dead could
be neither adopted nor redriven. Twenty-three session classifications sat in
that gap on this vault.

Carrying the link is not a way around the admission fence: the fence exists so
a task cannot appear for an intent nobody holds, and this intent passed it
once. What travels is the same record — intent, its digest, the handler
version — re-signed for the child's id.

See `docs/research/2026-09-07-how-many-second-chances-an-intent-gets.md`.
"""

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import memory_queue  # noqa: E402
from memory_queue import QueueOperationError  # noqa: E402

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)


def _database() -> sqlite3.Connection:
    database = sqlite3.connect(":memory:")
    database.row_factory = sqlite3.Row
    database.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, state TEXT, lineage_generation INTEGER)"
    )
    database.execute(
        "CREATE TABLE capture_task_links (task_id TEXT PRIMARY KEY, intent_id TEXT, "
        "intent_sha256 TEXT, handler_version INTEGER, link_digest TEXT, created_at TEXT)"
    )
    return database


def _linked(database, task_id="parent", generation=0) -> None:
    database.execute(
        "INSERT INTO tasks VALUES (?, 'dead', ?)", (task_id, generation)
    )
    database.execute(
        "INSERT INTO capture_task_links VALUES (?,?,?,?,?,?)",
        (task_id, "intent-1", "d" * 64, 1, "digest-of-parent", "2026-09-06"),
    )


class _Queue:
    """The three methods under test, borrowed so the fake cannot drift."""

    _parent_capture_link = staticmethod(
        memory_queue._QueueV3CandidateReader._parent_capture_link
    )
    _insert_capture_link = staticmethod(
        memory_queue._QueueV3CandidateReader._insert_capture_link
    )
    _carry_capture_link = memory_queue._QueueV3CandidateReader._carry_capture_link


def test_the_child_gets_a_link_to_the_same_intent():
    database = _database()
    _linked(database)

    _Queue()._carry_capture_link(database, "parent", "child", NOW)

    row = database.execute(
        "SELECT * FROM capture_task_links WHERE task_id='child'"
    ).fetchone()
    assert row["intent_id"] == "intent-1"
    assert row["intent_sha256"] == "d" * 64
    assert row["handler_version"] == 1


def test_the_child_link_is_signed_for_the_child_and_not_copied():
    database = _database()
    _linked(database)

    _Queue()._carry_capture_link(database, "parent", "child", NOW)

    child = database.execute(
        "SELECT link_digest FROM capture_task_links WHERE task_id='child'"
    ).fetchone()
    assert child["link_digest"] != "digest-of-parent"
    expected = memory_queue.sha256_bytes(
        memory_queue.canonical_json_bytes(
            memory_queue._capture_link_record("child", "intent-1", "d" * 64, 1)
        )
    )
    assert child["link_digest"] == expected


def test_a_task_that_was_never_a_capture_gets_no_link():
    database = _database()
    database.execute("INSERT INTO tasks VALUES ('parent', 'dead', 0)")

    _Queue()._carry_capture_link(database, "parent", "child", NOW)

    assert database.execute("SELECT COUNT(*) FROM capture_task_links").fetchone()[0] == 0


def test_a_first_redrive_is_allowed():
    database = _database()
    _linked(database, generation=0)

    assert memory_queue._require_dead_task(database, "parent")["id"] == "parent"


def test_a_second_redrive_is_refused():
    database = _database()
    _linked(database, generation=memory_queue.MAX_REDRIVE_GENERATIONS)

    with pytest.raises(QueueOperationError, match="redrive_generations_exhausted"):
        memory_queue._require_dead_task(database, "parent")


def test_a_task_that_is_not_dead_is_still_refused_first():
    database = _database()
    database.execute("INSERT INTO tasks VALUES ('parent', 'ready', 0)")

    with pytest.raises(QueueOperationError, match="redrive_requires_dead"):
        memory_queue._require_dead_task(database, "parent")


def test_one_round_is_the_bound_the_research_chose():
    assert memory_queue.MAX_REDRIVE_GENERATIONS == 1
