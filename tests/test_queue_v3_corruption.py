from __future__ import annotations

import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import markdown_transaction  # noqa: E402
import memory_queue  # noqa: E402
import operational_ownership  # noqa: E402
from reliable_memory import sha256_bytes  # noqa: E402


def _queue(tmp_path: Path):
    coordinator = tmp_path / "run" / "markdown-transactions-v3.candidate.sqlite3"
    candidate = tmp_path / "run" / "queue-v3.candidate.sqlite3"
    markdown_transaction.initialize_coordinator_v3_candidate(
        coordinator, source_v2=None
    )
    memory_queue.initialize_queue_v3_candidate(candidate, source_v2=None)
    return memory_queue.MemoryQueue._from_v3_candidate(
        candidate, state_root=tmp_path
    )


def _insert_corrupt_task(queue, task_id: str) -> None:
    now = "2026-08-12T12:00:00+00:00"
    raw = b'{"payload":"corrupt"}'
    with sqlite3.connect(queue.db_path) as database:
        database.execute(
            """INSERT INTO tasks(
                   id,kind,handler_version,payload_blob,input_hash,state,
                   priority,created_at,updated_at,available_at,error_code
               ) VALUES (?,?,?,?,?,'dead',0,?,?,?,'payload_hash_mismatch')""",
            (task_id, "query", 1, raw, "0" * 64, now, now, now),
        )


def _quarantined_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    task_id: str,
    now: datetime | None = None,
    resolve_intent: bool = True,
    children: list[dict[str, object]] | None = None,
):
    queue = _queue(tmp_path)
    coordinator = markdown_transaction.MarkdownCoordinator._from_v3_candidate(
        tmp_path / "run" / "markdown-transactions-v3.candidate.sqlite3",
        state_root=tmp_path,
    )
    registry = operational_ownership.OwnershipRegistry(tmp_path)
    intent_id = sha256_bytes(f"intent:{task_id}".encode())
    intent_sha256 = "b" * 64
    intent_path = f"run/capture-intents/{intent_id}.json"
    queue.publish_capture_intent(
        intent_id=intent_id,
        intent_path=intent_path,
        intent_sha256=intent_sha256,
        byte_size=128,
    )
    capture_owner = registry.acquire(
        "capture", scope=f"intent:{intent_id}", actor_id=f"capture-{task_id}"
    )
    capture_fence = coordinator.acquire_intent_fence(
        intent_id, mode="capture", owner=capture_owner
    )
    binding = queue.enqueue_capture_task(
        "flush",
        1,
        {"prompt": "corrupt later"},
        intent_id=intent_id,
        intent_path=intent_path,
        intent_sha256=intent_sha256,
        capture_fence=capture_fence,
        owner=capture_owner,
    )
    coordinator.release_intent_fence(capture_fence)
    registry.release(capture_owner)
    with sqlite3.connect(queue.db_path) as database:
        for child in children or []:
            child_id = str(child["id"])
            child_state = str(child.get("state", "succeeded"))
            child_time = str(child.get("updated_at", "2000-01-01T00:00:00+00:00"))
            parent_id = str(child.get("redrive_of", binding.task_id))
            database.execute(
                """INSERT INTO tasks(
                       id,kind,handler_version,payload_blob,input_hash,state,
                       priority,created_at,updated_at,available_at,redrive_of
                   ) VALUES (?,'query',1,?,?,?,0,?,?,?,?)""",
                (
                    child_id,
                    b"{}",
                    sha256_bytes(b"{}"),
                    child_state,
                    child_time,
                    child_time,
                    child_time,
                    parent_id,
                ),
            )
    with sqlite3.connect(queue.db_path) as database:
        database.execute(
            """UPDATE tasks SET payload_blob=?,state='dead',
                   error_code='payload_hash_mismatch' WHERE id=?""",
            (b'{"tampered":true}', binding.task_id),
        )
    if now is not None:
        monkeypatch.setattr(memory_queue, "_utc_now", lambda: now)
    owner = registry.acquire(
        "repair",
        scope=f"repair:{sha256_bytes(task_id.encode('utf-8'))}",
        actor_id=f"repair-{task_id}",
    )
    progress = queue.quarantine_corrupt(
        binding.task_id, reason="Retain and disposition.", owner=owner
    )
    while progress.state == "quarantine_pending":
        progress = queue.quarantine_corrupt(
            binding.task_id, reason="Retain and disposition.", owner=owner
        )
    assert progress.state == "quarantined"
    if resolve_intent:
        terminal = {
            "schema_version": "capture-terminal/v1",
            "intent_id": intent_id,
            "intent_sha256": intent_sha256,
            "semantic_decisions": [],
            "processing_binding": {
                "kind": "task",
                "task_id": binding.task_id,
                "active_link_digest": binding.active_digest,
            },
            "disposition": {
                "kind": "operator_discard",
                "operation_id": f"discard:{intent_id}",
                "actor_identity": "test-operator",
                "reason": "Disposition corrupt capture evidence.",
                "disposed_at": (now or datetime.now(timezone.utc)).isoformat(),
            },
        }
        terminal_path = tmp_path / "run" / "queue-results" / f"capture-{intent_id}.json"
        terminal_bytes = memory_queue.canonical_json_bytes(terminal)
        memory_queue._write_durable_file(
            terminal_path, terminal_bytes
        )
        with sqlite3.connect(queue.db_path) as database:
            database.execute(
                """UPDATE tasks SET result_reference=?,result_sha256=?,
                       result_operation_id=? WHERE id=?""",
                (
                    f"run/queue-results/capture-{intent_id}.json",
                    sha256_bytes(terminal_bytes),
                    f"capture-terminal:{intent_id}",
                    binding.task_id,
                ),
            )
    return queue, registry, owner, binding.task_id, intent_id


@pytest.mark.parametrize(
    "task_id",
    [
        "../escape",
        "CON",
        "Mixed/Case\\Separators",
        "e\u0301",
        "x" * 256,
    ],
)
def test_quarantine_freezes_before_digest_named_package_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, task_id: str
) -> None:
    queue = _queue(tmp_path)
    _insert_corrupt_task(queue, task_id)
    registry = operational_ownership.OwnershipRegistry(tmp_path)
    owner = registry.acquire(
        "repair", scope=f"repair:{sha256_bytes(task_id.encode('utf-8'))}", actor_id="repair"
    )
    observed: list[str] = []

    def inspect_frozen(package: Path, *_args: object, **_kwargs: object) -> None:
        observed.append(package.name)
        assert re.fullmatch(r"corrupt-[0-9a-f]{64}", package.name)
        assert task_id not in package.as_posix()
        with sqlite3.connect(queue.db_path) as database:
            assert database.execute(
                "SELECT state FROM tasks WHERE id=?", (task_id,)
            ).fetchone() == ("quarantine_pending",)
            with pytest.raises(sqlite3.IntegrityError, match="lineage is frozen"):
                database.execute(
                    """INSERT INTO tasks(
                           id,kind,handler_version,payload_blob,input_hash,state,
                           priority,created_at,updated_at,available_at,redrive_of
                       ) VALUES ('late-child','query',1,?,?,'ready',0,?,?,?,?)""",
                    (b"{}", sha256_bytes(b"{}"), "now", "now", "now", task_id),
                )
        raise RuntimeError("stop before package publication")

    monkeypatch.setattr(queue, "_publish_corrupt_fixed_files", inspect_frozen)

    with pytest.raises(RuntimeError, match="stop before package publication"):
        queue.quarantine_corrupt(task_id, reason="Retain corrupt evidence.", owner=owner)

    assert len(observed) == 1
    registry.release(owner)


def test_quarantine_exports_lineage_in_pages_of_at_most_one_thousand(
    tmp_path: Path,
) -> None:
    queue = _queue(tmp_path)
    parent = "corrupt-parent"
    _insert_corrupt_task(queue, parent)
    now = "2000-01-01T00:00:00+00:00"
    with sqlite3.connect(queue.db_path) as database:
        database.executemany(
            """INSERT INTO tasks(
                   id,kind,handler_version,payload_blob,input_hash,state,
                   priority,created_at,updated_at,available_at,redrive_of
               ) VALUES (?,'query',1,?,?,'succeeded',0,?,?,?,?)""",
            [
                (
                    f"child-{index:04d}",
                    b"{}",
                    sha256_bytes(b"{}"),
                    now,
                    now,
                    now,
                    parent,
                )
                for index in range(2505)
            ],
        )
    registry = operational_ownership.OwnershipRegistry(tmp_path)
    owner = registry.acquire(
        "repair", scope="repair:paged-export", actor_id="paged-repair"
    )

    first = queue.quarantine_corrupt(parent, reason="Export bounded lineage.", owner=owner)
    second = queue.quarantine_corrupt(parent, reason="Export bounded lineage.", owner=owner)
    third = queue.quarantine_corrupt(parent, reason="Export bounded lineage.", owner=owner)

    assert (first.pages_written, first.links_exported, first.complete) == (1, 1000, False)
    assert (second.pages_written, second.links_exported, second.complete) == (2, 2000, False)
    assert (third.pages_written, third.links_exported, third.complete) == (3, 2505, False)
    assert third.state == "blocked"
    assert third.code == "capture_link_conflicted"
    with sqlite3.connect(queue.db_path) as database:
        pages = database.execute(
            """SELECT page_number,link_count,first_task_id,last_task_id
               FROM corrupt_export_pages ORDER BY page_number"""
        ).fetchall()
        operation = database.execute(
            """SELECT cursor_task_id,link_count,page_count,state
               FROM corrupt_export_operations WHERE task_id=?""",
            (parent,),
        ).fetchone()
        task = database.execute(
            "SELECT state FROM tasks WHERE id=?", (parent,)
        ).fetchone()
    assert [page[1] for page in pages] == [1000, 1000, 505]
    assert pages[0][2:] == ("child-0000", "child-0999")
    assert pages[-1][2:] == ("child-2000", "child-2504")
    assert operation == ("child-2504", 2505, 3, "manifested")
    assert task == ("quarantine_pending",)
    package = tmp_path / "run" / "queue-results" / f"corrupt-{third.operation_id.removeprefix('corrupt-export:')}"
    assert (package / "manifest.json").is_file()
    assert len(list(package.glob("lineage-page-*.json"))) == 3
    registry.release(owner)


def test_disposition_file_precedes_atomic_terminal_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = _queue(tmp_path)
    coordinator = markdown_transaction.MarkdownCoordinator._from_v3_candidate(
        tmp_path / "run" / "markdown-transactions-v3.candidate.sqlite3",
        state_root=tmp_path,
    )
    intent_id = "a" * 64
    intent_sha256 = "b" * 64
    intent_path = f"run/capture-intents/{intent_id}.json"
    queue.publish_capture_intent(
        intent_id=intent_id,
        intent_path=intent_path,
        intent_sha256=intent_sha256,
        byte_size=128,
    )
    registry = operational_ownership.OwnershipRegistry(tmp_path)
    capture_owner = registry.acquire(
        "capture", scope=f"intent:{intent_id}", actor_id="capture"
    )
    capture_fence = coordinator.acquire_intent_fence(
        intent_id, mode="capture", owner=capture_owner
    )
    binding = queue.enqueue_capture_task(
        "flush",
        1,
        {"prompt": "corrupt later"},
        intent_id=intent_id,
        intent_path=intent_path,
        intent_sha256=intent_sha256,
        capture_fence=capture_fence,
        owner=capture_owner,
    )
    coordinator.release_intent_fence(capture_fence)
    registry.release(capture_owner)
    with sqlite3.connect(queue.db_path) as database:
        database.execute(
            """UPDATE tasks SET payload_blob=?,state='dead',
                   error_code='payload_hash_mismatch' WHERE id=?""",
            (b'{"tampered":true}', binding.task_id),
        )
    owner = registry.acquire(
        "repair", scope="repair:disposition", actor_id="disposition-repair"
    )
    real_insert = queue._insert_corrupt_disposition

    def fail_insert(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected disposition database failure")

    monkeypatch.setattr(queue, "_insert_corrupt_disposition", fail_insert)
    with pytest.raises(RuntimeError, match="injected disposition database failure"):
        queue.quarantine_corrupt(
            binding.task_id, reason="Retain and disposition.", owner=owner
        )
    with sqlite3.connect(queue.db_path) as database:
        operation = database.execute(
            "SELECT disposition_key FROM corrupt_export_operations WHERE task_id=?",
            (binding.task_id,),
        ).fetchone()
        assert database.execute(
            "SELECT state FROM tasks WHERE id=?", (binding.task_id,)
        ).fetchone() == ("quarantine_pending",)
        assert database.execute(
            "SELECT COUNT(*) FROM corrupt_dispositions"
        ).fetchone() == (0,)
    package = (
        tmp_path / "run" / "queue-results" / f"corrupt-{operation[0]}"
    )
    assert (package / "disposition.json").is_file()

    monkeypatch.setattr(queue, "_insert_corrupt_disposition", real_insert)
    progress = queue.quarantine_corrupt(
        binding.task_id, reason="Retain and disposition.", owner=owner
    )

    assert progress.state == "quarantined"
    assert progress.complete is True
    with sqlite3.connect(queue.db_path) as database:
        assert database.execute(
            "SELECT state FROM tasks WHERE id=?", (binding.task_id,)
        ).fetchone() == ("quarantined",)
        assert database.execute(
            "SELECT COUNT(*) FROM corrupt_dispositions"
        ).fetchone() == (1,)
    registry.release(owner)


def test_quarantined_task_blocks_purge_for_thirty_days(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue, registry, owner, task_id, _ = _quarantined_task(
        tmp_path, monkeypatch, task_id="young-parent"
    )

    progress = queue.purge_quarantined(task_id, owner=owner)

    assert progress.state == "blocked"
    assert progress.code == "corrupt_retention_active"
    assert progress.complete is False
    with sqlite3.connect(queue.db_path) as database:
        assert database.execute(
            "SELECT state FROM tasks WHERE id=?", (task_id,)
        ).fetchone() == ("quarantined",)
        assert database.execute(
            "SELECT COUNT(*) FROM corrupt_purge_operations"
        ).fetchone() == (0,)
    registry.release(owner)


def test_old_quarantined_task_blocks_purge_while_capture_intent_is_unresolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = datetime(2000, 1, 1, tzinfo=timezone.utc)
    queue, registry, owner, task_id, _ = _quarantined_task(
        tmp_path,
        monkeypatch,
        task_id="unresolved-parent",
        now=old,
        resolve_intent=False,
    )
    monkeypatch.setattr(memory_queue, "_utc_now", lambda: datetime.now(timezone.utc))

    progress = queue.purge_quarantined(task_id, owner=owner)

    assert progress.state == "blocked"
    assert progress.code == "capture_intent_unresolved"
    with sqlite3.connect(queue.db_path) as database:
        assert database.execute(
            "SELECT state FROM tasks WHERE id=?", (task_id,)
        ).fetchone() == ("quarantined",)
        assert database.execute(
            "SELECT COUNT(*) FROM corrupt_purge_operations"
        ).fetchone() == (0,)
    registry.release(owner)


@pytest.mark.parametrize("corruption", ["payload", "frozen-root"])
def test_purge_rejects_invalid_package_or_changed_frozen_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corruption: str
) -> None:
    old = datetime(2000, 1, 1, tzinfo=timezone.utc)
    queue, registry, owner, task_id, _ = _quarantined_task(
        tmp_path, monkeypatch, task_id=f"{corruption}-parent", now=old
    )
    with sqlite3.connect(queue.db_path) as database:
        package_key = database.execute(
            "SELECT disposition_key FROM corrupt_export_operations WHERE task_id=?",
            (task_id,),
        ).fetchone()[0]
        if corruption == "frozen-root":
            database.execute("DROP TRIGGER corrupt_export_operations_monotonic_update")
            database.execute(
                """UPDATE corrupt_export_operations SET rolling_root=?
                   WHERE task_id=?""",
                ("f" * 64, task_id),
            )
    package = tmp_path / "run" / "queue-results" / f"corrupt-{package_key}"
    if corruption == "payload":
        (package / "payload.bin").write_bytes(b"changed")
    monkeypatch.setattr(memory_queue, "_utc_now", lambda: datetime.now(timezone.utc))

    progress = queue.purge_quarantined(task_id, owner=owner)

    assert (progress.state, progress.code) == (
        "blocked",
        "corrupt_package_invalid",
    )
    with sqlite3.connect(queue.db_path) as database:
        assert database.execute(
            "SELECT state FROM tasks WHERE id=?", (task_id,)
        ).fetchone() == ("quarantined",)
        assert database.execute(
            "SELECT COUNT(*) FROM corrupt_purge_operations"
        ).fetchone() == (0,)
    registry.release(owner)


def test_capture_terminal_file_without_task_result_binding_does_not_authorize_purge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = datetime(2000, 1, 1, tzinfo=timezone.utc)
    queue, registry, owner, task_id, intent_id = _quarantined_task(
        tmp_path,
        monkeypatch,
        task_id="unbound-terminal-parent",
        now=old,
        resolve_intent=False,
    )
    with sqlite3.connect(queue.db_path) as database:
        link = database.execute(
            "SELECT link_digest,intent_sha256 FROM capture_task_links WHERE task_id=?",
            (task_id,),
        ).fetchone()
    terminal = {
        "schema_version": "capture-terminal/v1",
        "intent_id": intent_id,
        "intent_sha256": link[1],
        "semantic_decisions": [],
        "processing_binding": {
            "kind": "task",
            "task_id": task_id,
            "active_link_digest": link[0],
        },
        "disposition": {
            "kind": "operator_discard",
            "operation_id": f"discard:{intent_id}",
            "actor_identity": "test-operator",
            "reason": "Disposition corrupt capture evidence.",
            "disposed_at": old.isoformat(),
        },
    }
    memory_queue._write_durable_file(
        tmp_path / "run" / "queue-results" / f"capture-{intent_id}.json",
        memory_queue.canonical_json_bytes(terminal),
    )
    monkeypatch.setattr(memory_queue, "_utc_now", lambda: datetime.now(timezone.utc))

    progress = queue.purge_quarantined(task_id, owner=owner)

    assert (progress.state, progress.code) == (
        "blocked",
        "capture_terminal_unbound",
    )
    with sqlite3.connect(queue.db_path) as database:
        assert database.execute(
            "SELECT state FROM tasks WHERE id=?", (task_id,)
        ).fetchone() == ("quarantined",)
    registry.release(owner)


def test_purge_moves_parent_to_pending_before_lineage_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = datetime(2000, 1, 1, tzinfo=timezone.utc)
    queue, registry, owner, task_id, _ = _quarantined_task(
        tmp_path, monkeypatch, task_id="old-parent", now=old
    )
    monkeypatch.setattr(memory_queue, "_utc_now", lambda: datetime.now(timezone.utc))

    def stop_after_state(*_args: object, **_kwargs: object) -> None:
        with sqlite3.connect(queue.db_path) as database:
            assert database.execute(
                "SELECT state FROM tasks WHERE id=?", (task_id,)
            ).fetchone() == ("purge_pending",)
            with pytest.raises(sqlite3.IntegrityError, match="lineage is frozen"):
                database.execute(
                    """INSERT INTO tasks(
                           id,kind,handler_version,payload_blob,input_hash,state,
                           priority,created_at,updated_at,available_at,redrive_of
                       ) VALUES ('late','query',1,?,?,'ready',0,'now','now','now',?)""",
                    (b"{}", sha256_bytes(b"{}"), task_id),
                )
        raise RuntimeError("stop after purge state")

    monkeypatch.setattr(queue, "_purge_corrupt_lineage_page", stop_after_state)
    with pytest.raises(RuntimeError, match="stop after purge state"):
        queue.purge_quarantined(task_id, owner=owner)

    with sqlite3.connect(queue.db_path) as database:
        assert database.execute(
            "SELECT state FROM corrupt_purge_operations WHERE task_id=?", (task_id,)
        ).fetchone() == ("purging",)
    registry.release(owner)


def test_forged_lineage_authorization_cannot_bypass_live_purge_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = datetime(2000, 1, 1, tzinfo=timezone.utc)
    queue, registry, owner, task_id, _ = _quarantined_task(
        tmp_path,
        monkeypatch,
        task_id="forged-authorization-parent",
        now=old,
        children=[{"id": "protected-child", "updated_at": old.isoformat()}],
    )
    monkeypatch.setattr(memory_queue, "_utc_now", lambda: datetime.now(timezone.utc))

    def stop_after_state(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("leave live purge operation")

    monkeypatch.setattr(queue, "_purge_corrupt_lineage_page", stop_after_state)
    with pytest.raises(RuntimeError, match="leave live purge operation"):
        queue.purge_quarantined(task_id, owner=owner)

    with sqlite3.connect(queue.db_path) as database:
        database.execute(
            """INSERT INTO task_purge_authorizations(
                   task_id,mode,operation_id,authorization_digest,created_at
               ) VALUES (?,'corrupt-lineage','forged-operation',?,?)""",
            (task_id, "e" * 64, old.isoformat()),
        )
        with pytest.raises(sqlite3.IntegrityError, match="lineage is frozen"):
            database.execute("DELETE FROM tasks WHERE id='protected-child'")
    registry.release(owner)


def test_task_and_evidence_deletes_require_live_operation_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = datetime(2000, 1, 1, tzinfo=timezone.utc)
    queue, registry, owner, task_id, _ = _quarantined_task(
        tmp_path,
        monkeypatch,
        task_id="delete-authorization-parent",
        now=old,
        children=[{"id": "protected-child", "updated_at": old.isoformat()}],
    )
    with sqlite3.connect(queue.db_path) as database:
        database.execute(
            """INSERT INTO tasks(
                   id,kind,handler_version,payload_blob,input_hash,state,
                   priority,created_at,updated_at,available_at
               ) VALUES ('isolated-task','query',1,?,?,'succeeded',0,?,?,?)""",
            (
                b"{}",
                sha256_bytes(b"{}"),
                old.isoformat(),
                old.isoformat(),
                old.isoformat(),
            ),
        )
        database.execute(
            """INSERT INTO attempt_history(
                   task_id,attempt,started_at,finished_at,outcome,error_code
               ) VALUES ('isolated-task',1,?,?,'succeeded',NULL)""",
            (old.isoformat(), old.isoformat()),
        )
        database.execute(
            """INSERT INTO attempt_history(
                   task_id,attempt,started_at,finished_at,outcome,error_code
               ) VALUES ('protected-child',1,?,?,'succeeded',NULL)""",
            (old.isoformat(), old.isoformat()),
        )
        with pytest.raises(sqlite3.IntegrityError, match="task delete is unauthorized"):
            database.execute("DELETE FROM tasks WHERE id='isolated-task'")
    monkeypatch.setattr(memory_queue, "_utc_now", lambda: datetime.now(timezone.utc))

    def stop_after_state(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("leave live purge operation")

    monkeypatch.setattr(queue, "_purge_corrupt_lineage_page", stop_after_state)
    with pytest.raises(RuntimeError, match="leave live purge operation"):
        queue.purge_quarantined(task_id, owner=owner)

    with sqlite3.connect(queue.db_path) as database:
        database.execute(
            """INSERT INTO task_purge_authorizations(
                   task_id,mode,operation_id,authorization_digest,created_at
               ) VALUES ('protected-child','corrupt-lineage','forged-operation',?,?)""",
            ("e" * 64, old.isoformat()),
        )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="attempt history delete is unauthorized",
        ):
            database.execute(
                "DELETE FROM attempt_history WHERE task_id='protected-child'"
            )
    registry.release(owner)


@pytest.mark.parametrize(
    "blocker", ["descendant", "nonterminal", "young", "dead-retained"]
)
def test_purge_deletes_only_terminal_retention_eligible_leaves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, blocker: str
) -> None:
    old = datetime(2000, 1, 1, tzinfo=timezone.utc)
    child_time = (
        datetime.now(timezone.utc).isoformat()
        if blocker == "young"
        else old.isoformat()
    )
    child_state = {
        "nonterminal": "ready",
        "dead-retained": "dead",
    }.get(blocker, "succeeded")
    children = [
        {
            "id": "blocked-child",
            "state": child_state,
            "updated_at": child_time,
        }
    ]
    if blocker == "descendant":
        children.append(
            {
                "id": "grandchild",
                "state": "succeeded",
                "updated_at": old.isoformat(),
                "redrive_of": "blocked-child",
            }
        )
    queue, registry, owner, task_id, _ = _quarantined_task(
        tmp_path,
        monkeypatch,
        task_id=f"{blocker}-parent",
        now=old,
        children=children,
    )
    current = datetime.now(timezone.utc)
    monkeypatch.setattr(memory_queue, "_utc_now", lambda: current)

    blocked = queue.purge_quarantined(task_id, owner=owner)

    expected_code = {
        "descendant": "corrupt_child_not_leaf",
        "nonterminal": "corrupt_child_nonterminal",
        "young": "corrupt_child_retention_active",
        "dead-retained": "corrupt_child_retained",
    }[blocker]
    assert (blocked.state, blocked.code, blocked.links_deleted) == (
        "blocked",
        expected_code,
        0,
    )
    with sqlite3.connect(queue.db_path) as database:
        operation = database.execute(
            """SELECT cursor_task_id,page_count FROM corrupt_purge_operations
               WHERE task_id=?""",
            (task_id,),
        ).fetchone()
        assert operation == ("", 0)
        if blocker == "descendant":
            database.execute(
                "UPDATE tasks SET redrive_of=NULL WHERE id='grandchild'"
            )
        elif blocker != "dead-retained":
            database.execute(
                """UPDATE tasks SET state='succeeded',updated_at=?
                   WHERE id='blocked-child'""",
                (old.isoformat(),),
            )

    if blocker == "dead-retained":
        registry.release(owner)
        return

    completed = queue.purge_quarantined(task_id, owner=owner)

    assert completed.state == "purged"
    assert completed.complete is True
    assert completed.links_deleted == 1
    with sqlite3.connect(queue.db_path) as database:
        assert database.execute(
            "SELECT COUNT(*) FROM tasks WHERE id IN (?, 'blocked-child')", (task_id,)
        ).fetchone() == (0,)
    registry.release(owner)


def test_purge_commits_eligible_prefix_before_first_blocked_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = datetime(2000, 1, 1, tzinfo=timezone.utc)
    queue, registry, owner, task_id, _ = _quarantined_task(
        tmp_path,
        monkeypatch,
        task_id="prefix-parent",
        now=old,
        children=[
            {"id": "a-eligible", "updated_at": old.isoformat()},
            {"id": "b-blocked", "state": "ready", "updated_at": old.isoformat()},
        ],
    )
    monkeypatch.setattr(memory_queue, "_utc_now", lambda: datetime.now(timezone.utc))

    progress = queue.purge_quarantined(task_id, owner=owner)

    assert (progress.state, progress.code, progress.links_deleted) == (
        "blocked",
        "corrupt_child_nonterminal",
        1,
    )
    with sqlite3.connect(queue.db_path) as database:
        assert database.execute(
            "SELECT 1 FROM tasks WHERE id='a-eligible'"
        ).fetchone() is None
        assert database.execute(
            "SELECT state FROM tasks WHERE id='b-blocked'"
        ).fetchone() == ("ready",)
        assert database.execute(
            """SELECT cursor_task_id,page_count FROM corrupt_purge_operations
               WHERE task_id=?""",
            (task_id,),
        ).fetchone() == ("a-eligible", 1)
    registry.release(owner)


def test_purge_processes_at_most_one_thousand_links_per_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = datetime(2000, 1, 1, tzinfo=timezone.utc)
    children = [
        {"id": f"child-{index:04d}", "updated_at": old.isoformat()}
        for index in range(1001)
    ]
    queue, registry, owner, task_id, _ = _quarantined_task(
        tmp_path,
        monkeypatch,
        task_id="bounded-parent",
        now=old,
        children=children,
    )
    monkeypatch.setattr(memory_queue, "_utc_now", lambda: datetime.now(timezone.utc))

    first = queue.purge_quarantined(task_id, owner=owner)
    second = queue.purge_quarantined(task_id, owner=owner)

    assert (first.state, first.pages_written, first.links_deleted) == (
        "purge_pending",
        1,
        1000,
    )
    assert (second.state, second.pages_written, second.links_deleted) == (
        "purged",
        2,
        1001,
    )
    with sqlite3.connect(queue.db_path) as database:
        assert database.execute(
            """SELECT deleted_link_count FROM corrupt_purge_pages
               ORDER BY page_number"""
        ).fetchall() == [(1000,), (1,)]
    registry.release(owner)


def test_purge_blocks_when_parent_generation_changes_after_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = datetime(2000, 1, 1, tzinfo=timezone.utc)
    queue, registry, owner, task_id, _ = _quarantined_task(
        tmp_path,
        monkeypatch,
        task_id="generation-parent",
        now=old,
        children=[{"id": "old-child", "updated_at": old.isoformat()}],
    )
    monkeypatch.setattr(memory_queue, "_utc_now", lambda: datetime.now(timezone.utc))
    real_page = queue._purge_corrupt_lineage_page

    def race_generation(*args: object, **kwargs: object):
        with sqlite3.connect(queue.db_path) as database:
            database.execute(
                "UPDATE tasks SET lineage_generation=lineage_generation+1 WHERE id=?",
                (task_id,),
            )
        return real_page(*args, **kwargs)

    monkeypatch.setattr(queue, "_purge_corrupt_lineage_page", race_generation)

    progress = queue.purge_quarantined(task_id, owner=owner)

    assert (progress.state, progress.code) == (
        "blocked",
        "corrupt_lineage_generation_changed",
    )
    with sqlite3.connect(queue.db_path) as database:
        assert database.execute(
            "SELECT state FROM tasks WHERE id='old-child'"
        ).fetchone() == ("succeeded",)
        assert database.execute(
            "SELECT COUNT(*) FROM corrupt_purge_pages"
        ).fetchone() == (0,)
    registry.release(owner)


def test_purge_adopts_exact_orphan_page_after_database_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = datetime(2000, 1, 1, tzinfo=timezone.utc)
    queue, registry, owner, task_id, _ = _quarantined_task(
        tmp_path,
        monkeypatch,
        task_id="orphan-page-parent",
        now=old,
        children=[{"id": "old-leaf", "updated_at": old.isoformat()}],
    )
    monkeypatch.setattr(memory_queue, "_utc_now", lambda: datetime.now(timezone.utc))
    real_insert = queue._insert_corrupt_purge_page

    def fail_insert(*args: object, **kwargs: object) -> None:
        raise RuntimeError("crash after purge page publication")

    monkeypatch.setattr(queue, "_insert_corrupt_purge_page", fail_insert)
    with pytest.raises(RuntimeError, match="crash after purge page publication"):
        queue.purge_quarantined(task_id, owner=owner)

    with sqlite3.connect(queue.db_path) as database:
        package_key = database.execute(
            """SELECT operation.disposition_key
               FROM corrupt_dispositions AS disposition
               JOIN corrupt_export_operations AS operation
                 ON operation.operation_id=disposition.operation_id
               WHERE disposition.task_id=?""",
            (task_id,),
        ).fetchone()[0]
        assert database.execute(
            "SELECT COUNT(*) FROM corrupt_purge_pages"
        ).fetchone() == (0,)
        assert database.execute(
            "SELECT state FROM tasks WHERE id='old-leaf'"
        ).fetchone() == ("succeeded",)
    page_path = (
        tmp_path
        / "run"
        / "queue-results"
        / f"corrupt-{package_key}"
        / "purge-page-00000001.json"
    )
    orphan_bytes = page_path.read_bytes()

    monkeypatch.setattr(queue, "_insert_corrupt_purge_page", real_insert)
    completed = queue.purge_quarantined(task_id, owner=owner)

    assert completed.state == "purged"
    assert page_path.read_bytes() == orphan_bytes
    with sqlite3.connect(queue.db_path) as database:
        assert database.execute(
            "SELECT COUNT(*) FROM corrupt_purge_pages"
        ).fetchone() == (1,)
    registry.release(owner)


def test_purge_retains_and_blocks_conflicting_orphan_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = datetime(2000, 1, 1, tzinfo=timezone.utc)
    queue, registry, owner, task_id, _ = _quarantined_task(
        tmp_path,
        monkeypatch,
        task_id="conflicting-page-parent",
        now=old,
        children=[{"id": "old-leaf", "updated_at": old.isoformat()}],
    )
    monkeypatch.setattr(memory_queue, "_utc_now", lambda: datetime.now(timezone.utc))
    with sqlite3.connect(queue.db_path) as database:
        package_key = database.execute(
            """SELECT operation.disposition_key
               FROM corrupt_dispositions AS disposition
               JOIN corrupt_export_operations AS operation
                 ON operation.operation_id=disposition.operation_id
               WHERE disposition.task_id=?""",
            (task_id,),
        ).fetchone()[0]
    page_path = (
        tmp_path
        / "run"
        / "queue-results"
        / f"corrupt-{package_key}"
        / "purge-page-00000001.json"
    )
    conflicting = b'{"conflict":true}'
    memory_queue._write_durable_file(page_path, conflicting)

    progress = queue.purge_quarantined(task_id, owner=owner)

    assert (progress.state, progress.code) == (
        "blocked",
        "orphan_corrupt_purge_page_conflict",
    )
    assert page_path.read_bytes() == conflicting
    with sqlite3.connect(queue.db_path) as database:
        assert database.execute(
            "SELECT state FROM tasks WHERE id='old-leaf'"
        ).fetchone() == ("succeeded",)
        assert database.execute(
            "SELECT COUNT(*) FROM corrupt_purge_pages"
        ).fetchone() == (0,)
    registry.release(owner)


def test_purge_authorizes_child_owned_history_before_leaf_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = datetime(2000, 1, 1, tzinfo=timezone.utc)
    queue, registry, owner, task_id, _ = _quarantined_task(
        tmp_path,
        monkeypatch,
        task_id="history-parent",
        now=old,
        children=[{"id": "history-leaf", "updated_at": old.isoformat()}],
    )
    with sqlite3.connect(queue.db_path) as database:
        database.execute(
            """INSERT INTO attempt_history(
                   task_id,attempt,started_at,finished_at,outcome,error_code
               ) VALUES ('history-leaf',1,?,?,'succeeded',NULL)""",
            (old.isoformat(), old.isoformat()),
        )
    monkeypatch.setattr(memory_queue, "_utc_now", lambda: datetime.now(timezone.utc))

    progress = queue.purge_quarantined(task_id, owner=owner)

    assert progress.state == "purged"
    with sqlite3.connect(queue.db_path) as database:
        assert database.execute(
            "SELECT COUNT(*) FROM attempt_history WHERE task_id='history-leaf'"
        ).fetchone() == (0,)
        assert database.execute(
            "SELECT COUNT(*) FROM task_purge_authorizations"
        ).fetchone() == (0,)
    registry.release(owner)


def test_purge_receipt_precedes_and_resumes_final_parent_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = datetime(2000, 1, 1, tzinfo=timezone.utc)
    queue, registry, owner, task_id, _ = _quarantined_task(
        tmp_path, monkeypatch, task_id="receipt-parent", now=old
    )
    monkeypatch.setattr(memory_queue, "_utc_now", lambda: datetime.now(timezone.utc))
    real_delete = queue._delete_corrupt_parent

    def fail_delete(*args: object, **kwargs: object) -> None:
        raise RuntimeError("crash before parent deletion")

    monkeypatch.setattr(queue, "_delete_corrupt_parent", fail_delete)
    with pytest.raises(RuntimeError, match="crash before parent deletion"):
        queue.purge_quarantined(task_id, owner=owner)

    with sqlite3.connect(queue.db_path) as database:
        operation = database.execute(
            """SELECT purge.state,export.disposition_key
               FROM corrupt_purge_operations AS purge
               JOIN corrupt_export_operations AS export ON export.task_id=purge.task_id
               WHERE purge.task_id=?""",
            (task_id,),
        ).fetchone()
        assert operation[0] == "receipt-published"
        assert database.execute(
            "SELECT state FROM tasks WHERE id=?", (task_id,)
        ).fetchone() == ("purge_pending",)
    receipt_path = (
        tmp_path
        / "run"
        / "queue-results"
        / f"corrupt-{operation[1]}"
        / "purge-receipt.json"
    )
    receipt_bytes = receipt_path.read_bytes()
    assert len(receipt_bytes) < 64 * 1024
    assert "pages" not in memory_queue.json.loads(receipt_bytes)

    monkeypatch.setattr(queue, "_delete_corrupt_parent", real_delete)
    completed = queue.purge_quarantined(task_id, owner=owner)

    assert completed.state == "purged"
    assert completed.complete is True
    assert receipt_path.read_bytes() == receipt_bytes
    with sqlite3.connect(queue.db_path) as database:
        assert database.execute(
            "SELECT 1 FROM tasks WHERE id=?", (task_id,)
        ).fetchone() is None
        assert database.execute(
            "SELECT state FROM corrupt_purge_operations WHERE task_id=?", (task_id,)
        ).fetchone() == ("receipt-published",)
    registry.release(owner)


def test_final_parent_delete_restreams_live_payload_after_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = datetime(2000, 1, 1, tzinfo=timezone.utc)
    queue, registry, owner, task_id, _ = _quarantined_task(
        tmp_path, monkeypatch, task_id="payload-race-parent", now=old
    )
    monkeypatch.setattr(memory_queue, "_utc_now", lambda: datetime.now(timezone.utc))
    real_delete = queue._delete_corrupt_parent

    def mutate_then_delete(*args: object, **kwargs: object) -> None:
        with sqlite3.connect(queue.db_path) as database:
            database.execute(
                "UPDATE tasks SET payload_blob=? WHERE id=?",
                (b'{"changed":true}', task_id),
            )
        real_delete(*args, **kwargs)

    monkeypatch.setattr(queue, "_delete_corrupt_parent", mutate_then_delete)

    with pytest.raises(
        memory_queue.QueueOperationError,
        match="corrupt_parent_delete_precondition_failed",
    ):
        queue.purge_quarantined(task_id, owner=owner)

    with sqlite3.connect(queue.db_path) as database:
        assert database.execute(
            "SELECT state FROM tasks WHERE id=?", (task_id,)
        ).fetchone() == ("purge_pending",)
        assert database.execute(
            "SELECT state FROM corrupt_purge_operations WHERE task_id=?", (task_id,)
        ).fetchone() == ("receipt-published",)
    registry.release(owner)


def test_final_parent_delete_restreams_live_history_after_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = datetime(2000, 1, 1, tzinfo=timezone.utc)
    queue, registry, owner, task_id, _ = _quarantined_task(
        tmp_path, monkeypatch, task_id="history-race-parent", now=old
    )
    monkeypatch.setattr(memory_queue, "_utc_now", lambda: datetime.now(timezone.utc))
    real_delete = queue._delete_corrupt_parent

    def mutate_then_delete(*args: object, **kwargs: object) -> None:
        with sqlite3.connect(queue.db_path) as database:
            database.execute(
                """INSERT INTO attempt_history(
                       task_id,attempt,started_at,finished_at,outcome,error_code
                   ) VALUES (?,1,?,?,'failed','late_history')""",
                (task_id, old.isoformat(), old.isoformat()),
            )
        real_delete(*args, **kwargs)

    monkeypatch.setattr(queue, "_delete_corrupt_parent", mutate_then_delete)

    with pytest.raises(
        memory_queue.QueueOperationError,
        match="corrupt_parent_delete_precondition_failed",
    ):
        queue.purge_quarantined(task_id, owner=owner)

    with sqlite3.connect(queue.db_path) as database:
        assert database.execute(
            "SELECT state FROM tasks WHERE id=?", (task_id,)
        ).fetchone() == ("purge_pending",)
    registry.release(owner)


def test_completed_corrupt_purge_is_idempotent_after_crash_before_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = datetime(2000, 1, 1, tzinfo=timezone.utc)
    queue, registry, owner, task_id, _ = _quarantined_task(
        tmp_path,
        monkeypatch,
        task_id="idempotent-parent",
        now=old,
        children=[{"id": "old-child", "updated_at": old.isoformat()}],
    )
    monkeypatch.setattr(memory_queue, "_utc_now", lambda: datetime.now(timezone.utc))

    first = queue.purge_quarantined(task_id, owner=owner)
    second = queue.purge_quarantined(task_id, owner=owner)

    assert first == second
    assert second.state == "purged"
    assert second.complete is True
    registry.release(owner)
