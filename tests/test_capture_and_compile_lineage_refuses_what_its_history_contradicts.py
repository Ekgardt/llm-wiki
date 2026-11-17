from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing
from types import SimpleNamespace

import compile_progress
import pytest
from llm_wiki import queue_task_lineage
from llm_wiki.formats.canonical import canonical_json_bytes, sha256_bytes
from llm_wiki.queue_contracts import (
    CaptureTaskBinding,
    CompileCompletionError,
    QueueOperationError,
)

LINK = "1" * 64
INTENT = "2" * 64
OTHER_INTENT = "3" * 64
RESOLUTION = "4" * 64
LATER_RESOLUTION = "5" * 64
ACTIVE = "6" * 64
SCHEMA = (
    "CREATE TABLE tasks(id TEXT PRIMARY KEY, kind TEXT, handler_version INTEGER, "
    "input_hash TEXT, payload_blob BLOB, redrive_of TEXT, state TEXT, blocked_capability TEXT)",
    "CREATE TABLE capture_intents(intent_id TEXT PRIMARY KEY, intent_sha256 TEXT)",
    "CREATE TABLE capture_task_links(task_id TEXT PRIMARY KEY, intent_id TEXT, "
    "intent_sha256 TEXT, handler_version INTEGER, link_digest TEXT)",
    "CREATE TABLE capture_task_link_resolutions(resolution_digest TEXT PRIMARY KEY, "
    "task_id TEXT, supersedes_digest TEXT, selected_intent_id TEXT, created_at TEXT)",
    "CREATE TABLE capture_task_link_seals(task_id TEXT PRIMARY KEY, active_digest TEXT, "
    "consumer_kind TEXT, consumer_id TEXT, seal_digest TEXT)",
    "CREATE TABLE compile_work_events(work_id TEXT, task_id TEXT, sequence INTEGER)",
)
TaskReader = Callable[[sqlite3.Connection, str], object]
operator_progress: TaskReader = queue_task_lineage.compile_operator_progress
dispatch_identity: Callable[[object, str], None] = queue_task_lineage.require_dispatch_identity
recovery_position: Callable[[object, object], None] = queue_task_lineage._require_recovery_position
complete_attestation: Callable[[sqlite3.Connection, sqlite3.Row], None] = (
    queue_task_lineage.require_complete_compile_attestation
)


def uncertain_dispatch(database: sqlite3.Connection, task_id: str) -> object:
    require: Callable[[sqlite3.Connection, str, str, str], object] = (
        queue_task_lineage.require_uncertain_compile_dispatch
    )
    return require(database, task_id, ACTIVE, ACTIVE)


@pytest.fixture
def database() -> Iterator[sqlite3.Connection]:
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.row_factory = sqlite3.Row
        for statement in SCHEMA:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO capture_task_links VALUES ('task', ?, ?, 3, ?)", (INTENT, INTENT, LINK)
        )
        yield connection


def _resolve(
    database: sqlite3.Connection, digest: str, supersedes: str | None, selected: str | None
) -> None:
    database.execute(
        "INSERT INTO capture_task_link_resolutions VALUES (?, 'task', ?, ?, ?)",
        (digest, supersedes, selected, f"2026-01-01T00:00:00Z-{digest}"),
    )


def _intent(database: sqlite3.Connection, intent_id: str) -> None:
    database.execute("INSERT INTO capture_intents VALUES (?, ?)", (intent_id, intent_id))


def test_a_task_without_a_capture_link_has_no_binding(database: sqlite3.Connection) -> None:
    with pytest.raises(QueueOperationError, match="capture_link_conflicted"):
        queue_task_lineage.capture_binding_of(database, "unlinked")


def test_an_unresolved_link_binds_its_own_intent(database: sqlite3.Connection) -> None:
    binding = queue_task_lineage.capture_binding_of(database, "task")

    assert binding == CaptureTaskBinding("task", INTENT, INTENT, 3, LINK, None)


def test_a_resolution_that_selects_no_intent_binds_nothing(database: sqlite3.Connection) -> None:
    _resolve(database, RESOLUTION, LINK, None)

    binding = queue_task_lineage.capture_binding_of(database, "task")

    assert (binding.intent_id, binding.active_digest) == (None, RESOLUTION)


def test_the_latest_resolution_binds_the_intent_it_selected(database: sqlite3.Connection) -> None:
    _intent(database, OTHER_INTENT)
    _resolve(database, RESOLUTION, LINK, None)
    _resolve(database, LATER_RESOLUTION, RESOLUTION, OTHER_INTENT)

    binding = queue_task_lineage.capture_binding_of(database, "task")

    assert (binding.intent_id, binding.active_digest) == (OTHER_INTENT, LATER_RESOLUTION)


def test_a_resolution_selecting_an_unknown_intent_is_a_conflict(
    database: sqlite3.Connection,
) -> None:
    _resolve(database, RESOLUTION, LINK, OTHER_INTENT)

    with pytest.raises(QueueOperationError, match="capture_link_conflicted"):
        queue_task_lineage.capture_binding_of(database, "task")


def test_two_unsuperseded_histories_are_a_conflict(database: sqlite3.Connection) -> None:
    _resolve(database, RESOLUTION, None, None)

    with pytest.raises(QueueOperationError, match="capture_link_conflicted"):
        queue_task_lineage.capture_binding_of(database, "task")


def _seal(database: sqlite3.Connection, task_id: str, seal_digest: str) -> None:
    database.execute(
        "INSERT INTO capture_task_link_seals VALUES (?, ?, 'semantic-decision', ?, ?)",
        (task_id, ACTIVE, f"{INTENT}:classify", seal_digest),
    )


def _decision_row(database: sqlite3.Connection) -> sqlite3.Row:
    row = database.execute(
        "SELECT ? AS active_link_digest, 'path' AS decision_path, 'published' AS publication_state",
        (ACTIVE,),
    ).fetchone()
    assert isinstance(row, sqlite3.Row)
    return row


def test_a_decision_nobody_sealed_is_a_conflict(database: sqlite3.Connection) -> None:
    with pytest.raises(QueueOperationError, match="semantic_decision_conflict"):
        queue_task_lineage.decision_sealer(database, _decision_row(database), INTENT, "classify")


def test_a_seal_whose_digest_does_not_prove_it_is_refused(database: sqlite3.Connection) -> None:
    _seal(database, "task", "f" * 64)

    with pytest.raises(QueueOperationError, match="semantic_decision_conflict"):
        queue_task_lineage.decision_sealer(database, _decision_row(database), INTENT, "classify")
    assert queue_task_lineage.decision_seal_of(database, "task", INTENT, "classify") is None


def test_a_proven_seal_is_the_tasks_own(database: sqlite3.Connection) -> None:
    digest = queue_task_lineage.capture_semantic_seal_digest("task", INTENT, "classify", ACTIVE)
    _seal(database, "task", digest)

    sealer = queue_task_lineage.decision_sealer(
        database, _decision_row(database), INTENT, "classify"
    )

    assert sealer["task_id"] == "task"
    assert queue_task_lineage.decision_seal_of(database, "task", INTENT, "classify") is not None
    assert queue_task_lineage.decision_seal_of(database, "other", INTENT, "classify") is None


def test_an_indexed_decision_for_another_intent_is_a_conflict(
    database: sqlite3.Connection,
) -> None:
    active = CaptureTaskBinding("task", OTHER_INTENT, OTHER_INTENT, 3, ACTIVE, None)

    with pytest.raises(QueueOperationError, match="semantic_decision_conflict"):
        queue_task_lineage.require_indexed_capture_decision(
            _decision_row(database),
            active,
            intent_id=INTENT,
            stage="classify",
            active_link_digest=ACTIVE,
        )


def test_an_inherited_decision_for_another_intent_is_a_conflict(
    database: sqlite3.Connection,
) -> None:
    active = CaptureTaskBinding("task", OTHER_INTENT, OTHER_INTENT, 3, ACTIVE, None)

    with pytest.raises(QueueOperationError, match="semantic_decision_conflict"):
        queue_task_lineage.require_inherited_capture_decision(
            database,
            _decision_row(database),
            active,
            intent_id=INTENT,
            stage="classify",
            active_link_digest=ACTIVE,
        )


def _compile_task(database: sqlite3.Connection, task_id: str, redrive_of: str | None) -> None:
    payload = canonical_json_bytes({"daily": task_id})
    database.execute(
        "INSERT INTO tasks VALUES (?, 'compile', 1, ?, ?, ?, 'ready', NULL)",
        (task_id, sha256_bytes(payload), payload, redrive_of),
    )


@pytest.mark.parametrize("reader", [operator_progress, uncertain_dispatch])
def test_compile_recovery_for_a_missing_task_names_it(
    database: sqlite3.Connection, reader: TaskReader
) -> None:
    with pytest.raises(KeyError, match="missing-task"):
        reader(database, "missing-task")


def test_compile_work_checkpointed_by_another_family_is_corrupt(
    database: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _compile_task(database, "first", None)
    _compile_task(database, "second", None)
    database.execute("INSERT INTO compile_work_events VALUES (?, 'first', 0)", (ACTIVE,))
    monkeypatch.setattr(
        compile_progress,
        "latest_event",
        lambda connection, work_id: SimpleNamespace(task_id="second"),
    )

    with pytest.raises(compile_progress.ProgressCorrupt, match="changed task family"):
        operator_progress(database, "first")


def test_a_recovery_without_a_dispatch_is_a_conflict() -> None:
    with pytest.raises(compile_progress.ProgressConflict, match="compile dispatch is absent"):
        dispatch_identity(None, ACTIVE)


def test_a_recovery_after_progress_moved_on_is_refused() -> None:
    with pytest.raises(QueueOperationError, match="compile_recovery_progress_advanced"):
        recovery_position(SimpleNamespace(sequence=2), SimpleNamespace(sequence=4))


def test_a_compile_attestation_over_a_broken_lineage_names_the_break(
    database: sqlite3.Connection,
) -> None:
    _compile_task(database, "orphan", "vanished")
    row = database.execute("SELECT * FROM tasks WHERE id='orphan'").fetchone()

    with pytest.raises(CompileCompletionError, match="redrive_lineage_missing"):
        complete_attestation(database, row)
