"""Result publication under the lease fence: stable owner-only no-clobber files, reconciliation after a crash, corruption that goes dead, and the hardened directory, database and journal."""

from __future__ import annotations

import os
import random
import sqlite3
import stat
import sys
from pathlib import Path

import pytest
import queue_core  # noqa: E402
from llm_wiki import (
    memory_queue_results,  # noqa: E402
)

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from queue_core import (  # noqa: E402
    DeferredResult,
    MemoryQueue,
    ResultConflictError,
)
from reliable_memory import sha256_bytes  # noqa: E402

from tests.memory_queue_scenario_helpers import (  # noqa: E402
    FakeClock,
    _counts,
    _task_facts,
    _worked,
)
from tests.memory_queue_scenario_helpers import (  # noqa: E402
    clock as clock,
)
from tests.memory_queue_scenario_helpers import (  # noqa: E402
    queue as queue,
)


def _modes(*paths: Path) -> tuple[int, ...]:
    return tuple(stat.S_IMODE(path.stat().st_mode) for path in paths)


def _assert_posix_modes(queue: MemoryQueue, journal: Path) -> None:
    """Owner-only runtime, database and journal, where modes mean anything."""
    if os.name != "posix":
        return
    assert (
        _modes(queue.run_dir, queue.db_path),
        stat.S_IMODE(journal.stat().st_mode) & 0o077,
    ) == ((0o700, 0o600), 0)


def _read_if_present(path: Path) -> bytes | None:
    """What a result file holds, or None where it is gone."""
    if not path.is_file():
        return None
    return path.read_bytes()


def _hide_result_from_exists(monkeypatch, result_path: Path, branch: str) -> None:
    """In the link-race branch the file appears absent to the pre-check."""
    if branch != "link_race":
        return
    real_exists = Path.exists
    monkeypatch.setattr(
        Path,
        "exists",
        lambda path: False if path == result_path else real_exists(path),
    )


def _corrupt_stored_result(result_path: Path, corruption: str) -> None:
    if corruption == "missing":
        result_path.unlink()
        return
    result_path.write_bytes(b"tampered")


def _forget_result_reference(queue: MemoryQueue, task_id: str) -> None:
    """Leave the published file behind with no row pointing at it."""
    with sqlite3.connect(queue.db_path) as connection:
        connection.execute(
            """UPDATE tasks SET result_reference=NULL, result_operation_id=NULL,
                   result_sha256=NULL WHERE id=?""",
            (task_id,),
        )


def _chmod_or_unshare(monkeypatch, result_path: Path) -> None:
    """Make the file readable by others, or say so where modes do not apply."""
    if os.name == "posix":
        result_path.chmod(0o644)
        return
    monkeypatch.setattr(memory_queue_results, "is_owner_only", lambda path: False)


def _mutate_published_result(monkeypatch, result_path: Path, mutation: str) -> None:
    """One way a published result stops being the bytes that were published."""
    if mutation == "delete":
        result_path.unlink()
        return
    if mutation == "chmod":
        _chmod_or_unshare(monkeypatch, result_path)
        return
    result_path.write_bytes(b"corrupt")


def test_result_publication_is_stable_owner_only_and_no_clobber(
    queue: MemoryQueue,
) -> None:
    queue.enqueue("query", 1, {"prompt": "x"})
    lease = queue.claim("worker")
    assert lease is not None
    reference = queue.publish_result(lease, operation_id="stable-op", result=b"answer")
    again = queue.publish_result(lease, operation_id="stable-op", result=b"answer")
    with pytest.raises(ResultConflictError):
        queue.publish_result(lease, operation_id="stable-op", result=b"different")
    result_path = queue.state_root / reference

    assert (again, queue.get(lease.id).result_sha256) == (
        reference,
        sha256_bytes(b"answer"),
    )
    if sys.platform != "win32":
        assert _modes(result_path) == (0o600,)


@pytest.mark.parametrize("branch", ["precheck", "link_race"])
def test_publish_existing_same_digest_is_bounded_idempotent_without_read_bytes(
    queue: MemoryQueue, monkeypatch: pytest.MonkeyPatch, branch: str
) -> None:
    task_id = queue.enqueue("query", 1, {})
    lease = queue.claim("worker")
    assert lease is not None
    reference = queue.publish_result(lease, operation_id=task_id, result=b"same")
    result_path = queue.state_root / reference
    _hide_result_from_exists(monkeypatch, result_path, branch)
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda path: pytest.fail("publish_result must use bounded validator"),
    )

    again = queue.publish_result(lease, operation_id=task_id, result=b"same")

    assert (again, queue.get(task_id).state) == (reference, "leased")


def test_publish_existing_mismatch_is_bounded_conflict_without_overwrite(
    queue: MemoryQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = queue.enqueue("query", 1, {})
    lease = queue.claim("worker")
    assert lease is not None
    reference = queue.publish_result(lease, operation_id=task_id, result=b"original")
    result_path = queue.state_root / reference
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda path: pytest.fail("publish_result must use bounded validator"),
    )

    with pytest.raises(ResultConflictError, match="different result bytes"):
        queue.publish_result(lease, operation_id=task_id, result=b"different")
    with result_path.open("rb") as handle:
        assert handle.read() == b"original"
    assert queue.get(task_id).state == "leased"


def _corrupt_as_symlink(monkeypatch, _queue, result_path) -> None:
    real_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: path == result_path or real_is_symlink(path),
    )


def _corrupt_as_oversize(monkeypatch, _queue, _result_path) -> None:
    monkeypatch.setattr(memory_queue_results, "MAX_RESULT_BYTES", 1)


def _corrupt_as_changed(monkeypatch, queue, _result_path) -> None:
    monkeypatch.setattr(queue, "_validated_result_digest", lambda reference: None)


def _corrupt_as_shared(monkeypatch, _queue, _result_path) -> None:
    monkeypatch.setattr(memory_queue_results, "is_owner_only", lambda path: False)


_RESULT_CORRUPTIONS = {
    "symlink": _corrupt_as_symlink,
    "oversize": _corrupt_as_oversize,
    "changed": _corrupt_as_changed,
}


def _corrupt_published_result(monkeypatch, queue, result_path, invalid: str) -> None:
    """One way an already-published result can stop being trustworthy."""
    corrupt = _RESULT_CORRUPTIONS.get(invalid, _corrupt_as_shared)
    corrupt(monkeypatch, queue, result_path)


@pytest.mark.parametrize("invalid", ["symlink", "oversize", "changed", "owner"])
def test_publish_existing_invalid_metadata_dead_letters_without_overwrite(
    queue: MemoryQueue,
    monkeypatch: pytest.MonkeyPatch,
    invalid: str,
) -> None:

    task_id = queue.enqueue("query", 1, {})
    lease = queue.claim("worker")
    assert lease is not None
    reference = queue.publish_result(lease, operation_id=task_id, result=b"original")
    result_path = queue.state_root / reference
    _corrupt_published_result(monkeypatch, queue, result_path, invalid)
    incoming = b"x" if invalid == "oversize" else b"original"
    with pytest.raises(ResultConflictError, match="result_corrupt"):
        queue.publish_result(lease, operation_id=task_id, result=incoming)
    task = queue.get(task_id)

    assert ((task.state, task.error_code), _read_if_present(result_path)) == (
        ("dead", "result_corrupt"),
        b"original",
    )


def test_expired_lease_with_published_result_reconciles_without_redelivery(
    queue: MemoryQueue, clock: FakeClock
) -> None:
    task_id = queue.enqueue("query", 1, {"prompt": "nondeterministic"})
    lease = queue.claim("crashed", lease_seconds=5)
    assert lease is not None
    reference = queue.publish_result(
        lease, operation_id=task_id, result=b"first-and-only-result"
    )
    leased_before_crash = queue.get(task_id).state  # Crash before acknowledge.
    clock.advance(6)

    claimed_again = queue.claim("replacement")
    task = queue.get(task_id)

    assert (
        (leased_before_crash, claimed_again),
        (task.state, task.result_reference, task.result_sha256),
        [item.outcome for item in task.attempt_history],
    ) == (
        ("leased", None),
        ("succeeded", reference, sha256_bytes(b"first-and-only-result")),
        ["succeeded"],
    )


def test_a_handler_is_not_rerun_after_a_publish_before_ack_crash(
    tmp_path: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    import queue_core

    queue = MemoryQueue(tmp_path, clock=clock, rng=random.Random(12))
    monkeypatch.setattr(queue_core, "default_queue", lambda **_kwargs: queue)
    task_id = queue.enqueue("query", 1, {"prompt": "changes-every-time"})
    lease = queue.claim("crashed", lease_seconds=5)
    assert lease is not None
    queue.publish_result(lease, operation_id=task_id, result=b"stable")
    clock.advance(6)
    calls: list[str] = []

    summary = _worked(
        lambda task: calls.append(task["id"]) or DeferredResult(b"different"),
    )

    assert (_counts(summary), calls, queue.get(task_id).state) == (
        (0, 0, 0, 0),
        [],
        "succeeded",
    )


@pytest.mark.parametrize("corruption", ["missing", "mismatch"])
def test_expired_published_result_corruption_goes_dead_without_overwrite(
    queue: MemoryQueue, clock: FakeClock, corruption: str
) -> None:
    task_id = queue.enqueue("query", 1, {})
    lease = queue.claim("crashed", lease_seconds=5)
    assert lease is not None
    reference = queue.publish_result(lease, operation_id=task_id, result=b"original")
    result_path = queue.state_root / reference
    _corrupt_stored_result(result_path, corruption)
    clock.advance(6)

    claimed_again = queue.claim("replacement")
    task = queue.get(task_id)

    # A mismatched file keeps its own bytes; a missing one stays missing.
    assert (
        claimed_again,
        (task.state, task.error_code, task.attempt_history[-1].error_code),
        _read_if_present(result_path),
    ) == (
        None,
        ("dead", "result_corrupt", "result_corrupt"),
        None if corruption == "missing" else b"tampered",
    )


def test_an_orphaned_valid_result_is_adopted_before_the_handler(
    tmp_path: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    import queue_core

    queue = MemoryQueue(tmp_path, clock=clock, rng=random.Random(13))
    monkeypatch.setattr(queue_core, "default_queue", lambda **_kwargs: queue)
    task_id = queue.enqueue("query", 1, {})
    lease = queue.claim("crashed", lease_seconds=5)
    assert lease is not None
    reference = queue.publish_result(lease, operation_id=task_id, result=b"orphaned")
    _forget_result_reference(queue, task_id)
    clock.advance(6)
    calls: list[str] = []

    summary = _worked(
        lambda task: calls.append(task["id"]) or DeferredResult(b"new-output"),
    )
    task = queue.get(task_id)

    assert (
        _counts(summary),
        calls,
        (task.state, task.result_reference, task.result_sha256),
    ) == ((1, 0, 0, 0), [], ("succeeded", reference, sha256_bytes(b"orphaned")))


@pytest.mark.parametrize("mutation", ["delete", "chmod", "corrupt"])
def test_acknowledge_rejects_result_mutated_after_publish(
    queue: MemoryQueue,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:

    task_id = queue.enqueue("query", 1, {})
    lease = queue.claim("worker")
    assert lease is not None
    reference = queue.publish_result(lease, operation_id=task_id, result=b"original")
    _mutate_published_result(monkeypatch, queue.state_root / reference, mutation)

    terminal = queue.acknowledge(lease)

    assert ((terminal.state, terminal.error_code), _task_facts(queue.get(task_id))) == (
        ("dead", "result_corrupt"),
        ("dead", "result_corrupt", "failed", "result_corrupt"),
    )


def test_acknowledge_rejects_result_reference_outside_results_dir(
    queue: MemoryQueue,
) -> None:
    task_id = queue.enqueue("query", 1, {})
    lease = queue.claim("worker")
    assert lease is not None
    queue.publish_result(lease, operation_id=task_id, result=b"original")
    with sqlite3.connect(queue.db_path) as connection:
        connection.execute(
            "UPDATE tasks SET result_reference='run/outside.result' WHERE id=?",
            (task_id,),
        )

    queue.acknowledge(lease)
    assert queue.get(task_id).state == "dead"
    assert queue.get(task_id).error_code == "result_corrupt"


def test_acknowledge_rejects_symlink_result(
    queue: MemoryQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = queue.enqueue("query", 1, {})
    lease = queue.claim("worker")
    assert lease is not None
    reference = queue.publish_result(lease, operation_id=task_id, result=b"original")
    result_path = queue.state_root / reference
    real_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: path == result_path or real_is_symlink(path),
    )

    queue.acknowledge(lease)
    assert queue.get(task_id).state == "dead"
    assert queue.get(task_id).error_code == "result_corrupt"


def test_acknowledge_rejects_result_over_size_bound(
    queue: MemoryQueue, monkeypatch: pytest.MonkeyPatch
) -> None:

    task_id = queue.enqueue("query", 1, {})
    lease = queue.claim("worker")
    assert lease is not None
    queue.publish_result(lease, operation_id=task_id, result=b"two bytes")
    monkeypatch.setattr(memory_queue_results, "MAX_RESULT_BYTES", 1)

    queue.acknowledge(lease)
    assert queue.get(task_id).state == "dead"
    assert queue.get(task_id).error_code == "result_corrupt"


def test_orphan_adoption_rejects_invalid_full_metadata_before_handler(
    tmp_path: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    import queue_core

    queue = MemoryQueue(tmp_path, clock=clock, rng=random.Random(14))
    monkeypatch.setattr(queue_core, "default_queue", lambda **_kwargs: queue)
    task_id = queue.enqueue("query", 1, {})
    lease = queue.claim("crashed", lease_seconds=5)
    assert lease is not None
    queue.publish_result(lease, operation_id=task_id, result=b"too-large")
    _forget_result_reference(queue, task_id)
    monkeypatch.setattr(memory_queue_results, "MAX_RESULT_BYTES", 1)
    clock.advance(6)
    calls: list[str] = []

    summary = _worked(
        lambda task: calls.append(task["id"]) or DeferredResult(b"new")
    )
    task = queue.get(task_id)

    assert (_counts(summary), calls, (task.state, task.error_code)) == (
        (0, 1, 1, 0),
        [],
        ("dead", "result_corrupt"),
    )


def test_result_directory_is_fsynced_before_database_reference(
    queue: MemoryQueue, monkeypatch: pytest.MonkeyPatch
) -> None:

    task_id = queue.enqueue("query", 1, {})
    lease = queue.claim("worker")
    assert lease is not None
    monkeypatch.setattr(
        memory_queue_results,
        "fsync_directory",
        lambda path: (_ for _ in ()).throw(OSError("simulated power loss")),
    )
    with pytest.raises(OSError, match="power loss"):
        queue.publish_result(lease, operation_id=task_id, result=b"answer")
    assert queue.get(task_id).result_reference is None


def test_queue_hardens_directory_database_temp_and_result(
    tmp_path: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:

    protected: list[tuple[Path, int]] = []
    def record(path, mode):
        protected.append((Path(path), mode))

    monkeypatch.setattr(queue_core, "harden_owner_only", record)
    monkeypatch.setattr(memory_queue_results, "harden_owner_only", record)
    monkeypatch.setattr("llm_wiki.queue_artifacts.harden_owner_only", queue_core.harden_owner_only)
    queue = MemoryQueue(tmp_path, clock=clock, rng=random.Random(1))
    task_id = queue.enqueue("query", 1, {})
    lease = queue.claim("worker")
    assert lease is not None
    queue.publish_result(lease, operation_id=task_id, result=b"answer")
    paths = {path for path, _mode in protected}
    result = queue.state_root / queue.get(task_id).result_reference

    assert (
        {queue.run_dir, queue.results_dir, queue.db_path, result} <= paths,
        any(path.suffix == ".tmp" for path in paths),
    ) == (True, True)


def test_queue_acl_failure_is_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    monkeypatch.setattr(
        queue_core,
        "harden_owner_only",
        lambda path, mode: (_ for _ in ()).throw(PermissionError("ACL denied")),
    )
    with pytest.raises(PermissionError, match="ACL denied"):
        MemoryQueue(tmp_path)


def test_run_acl_is_hardened_before_first_sqlite_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    hardened: list[Path] = []
    real_open = queue_core.open_operational_db

    def record_hardening(path: Path, mode: int) -> None:
        del mode
        hardened.append(Path(path))

    def checked_open(path: Path, *, busy_ms: int, deadline: float | None = None) -> sqlite3.Connection:
        assert tmp_path.resolve() / "run" in hardened
        return real_open(path, busy_ms=busy_ms, deadline=deadline)

    monkeypatch.setattr(queue_core, "harden_owner_only", record_hardening)
    monkeypatch.setattr(queue_core, "open_operational_db", checked_open)
    queue = MemoryQueue(tmp_path)
    assert queue.run_dir in hardened


def test_run_acl_failure_prevents_sqlite_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    opened: list[Path] = []
    real_open = queue_core.open_operational_db

    def deny_run(path: Path, mode: int) -> None:
        del mode
        if Path(path).name == "run":
            raise PermissionError("Windows owner-only ACL denied")

    monkeypatch.setattr(queue_core, "harden_owner_only", deny_run)

    def record_open(path: Path, *, busy_ms: int) -> sqlite3.Connection:
        opened.append(Path(path))
        return real_open(path, busy_ms=busy_ms)

    monkeypatch.setattr(queue_core, "open_operational_db", record_open)
    with pytest.raises(PermissionError, match="owner-only ACL denied"):
        MemoryQueue(tmp_path)
    assert opened == []


def test_queue_uses_secure_rollback_journal_without_wal_files(queue: MemoryQueue) -> None:
    task_id = queue.enqueue("query", 1, {})
    journal = Path(f"{queue.db_path}-journal")
    with queue._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("UPDATE tasks SET priority=1 WHERE id=?", (task_id,))
        assert (
            journal.is_file(),
            Path(f"{queue.db_path}-wal").exists(),
            Path(f"{queue.db_path}-shm").exists(),
            connection.execute("PRAGMA journal_mode").fetchone()[0].lower(),
        ) == (True, False, False, "delete")
        _assert_posix_modes(queue, journal)
        connection.rollback()
