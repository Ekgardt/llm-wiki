"""Whole-file knowledge mutations are prepared, applied and settled once per operation."""
from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from llm_wiki import markdown_store
from llm_wiki.formats.canonical import sha256_bytes
from llm_wiki.markdown_admission import default_coordinator, recover_initial_contention
from llm_wiki.markdown_coordinator import MarkdownCoordinator
from llm_wiki.markdown_records import (
    MarkdownChange,
    OperationBoundElsewhereError,
    TargetPathBoundaryError,
    TransactionDriftError,
    TransactionRecord,
    TransactionStateError,
    require_bytes,
)
from llm_wiki.markdown_store import ABSENT, Validator, preparer_alive


def relative_target(coordinator: MarkdownCoordinator, path: Path) -> str:
    absolute = Path(path).absolute()
    try:
        relative = absolute.relative_to(coordinator.vault).as_posix()
    except ValueError as exc:
        raise TargetPathBoundaryError(
            f"knowledge target is outside the vault: {path}"
        ) from exc
    coordinator.ensure_target_parent(relative)
    return relative


def _relative_changes(
    coordinator: MarkdownCoordinator, changes: Mapping[Path, bytes | None]
) -> dict[str, bytes | None]:
    relative_changes = {
        relative_target(coordinator, Path(path)): content
        for path, content in changes.items()
    }
    for content in relative_changes.values():
        if content is None:
            continue
        if len(require_bytes(content)) > markdown_store.MAX_KNOWLEDGE_TARGET_BYTES:
            raise ValueError("knowledge target size exceeds limit")
    return relative_changes


def _desired_hashes(relative_changes: Mapping[str, bytes | None]) -> dict[str, str]:
    return {
        path: ABSENT if content is None else sha256_bytes(content)
        for path, content in relative_changes.items()
    }


def _persisted_hashes(record: TransactionRecord) -> dict[str, str]:
    return {operation.path: operation.after_hash for operation in record.operations}


def _require_committed_state(record: TransactionRecord) -> None:
    if record.state != "committed":
        raise RuntimeError(
            f"duplicate mutation ended in noncommitted state {record.state}"
        )


def _require_committed_duplicate(
    existing: TransactionRecord, relative_changes: Mapping[str, bytes | None]
) -> None:
    """A duplicate may only be reused when it asked for exactly this request."""
    if _desired_hashes(relative_changes) != _persisted_hashes(existing):
        raise OperationBoundElsewhereError("operation_id is already bound to a different request")
    _require_committed_state(existing)


def _existing_committed_record(
    coordinator: MarkdownCoordinator,
    operation_id: str,
    relative_changes: Mapping[str, bytes | None],
) -> TransactionRecord | None:
    """The committed record already bound to this operation id, if there is one."""
    if coordinator._record_for_operation_id(operation_id) is None:
        return None
    existing = settle_operation(coordinator, operation_id)
    if existing is None:
        return None
    _require_committed_duplicate(existing, relative_changes)
    _verify_committed_targets(coordinator, existing)
    return existing


def _captured_targets(
    coordinator: MarkdownCoordinator, relative_changes: Mapping[str, bytes | None]
) -> dict[str, bytes | None]:
    with coordinator.writer_gate():
        return {
            relative: coordinator._read_bounded_target(
                coordinator._target(relative), markdown_store.MAX_KNOWLEDGE_TARGET_BYTES
            )
            for relative in relative_changes
        }


def _prepared_delete(relative: str, before: bytes | None) -> MarkdownChange:
    if before is None:
        raise FileNotFoundError(relative)
    return MarkdownChange.delete(relative)


def _prepared_change(
    relative: str, content: bytes | None, before: bytes | None
) -> MarkdownChange:
    if content is None:
        return _prepared_delete(relative, before)
    if before is None:
        return MarkdownChange.create(relative, require_bytes(content))
    return MarkdownChange.replace(relative, require_bytes(content))


def _prepared_changes(
    relative_changes: Mapping[str, bytes | None],
    captured: Mapping[str, bytes | None],
    preconditions: Mapping[str, object] | None,
) -> tuple[list[MarkdownChange], dict[str, object]]:
    """The changes to prepare, plus the preconditions they were captured under."""
    expected = dict(preconditions or {})
    prepared: list[MarkdownChange] = []
    for relative, content in relative_changes.items():
        before = captured[relative]
        captured_hash = ABSENT if before is None else sha256_bytes(before)
        _require_caller_precondition(expected, relative, captured_hash)
        expected[relative] = captured_hash
        prepared.append(_prepared_change(relative, content, before))
    return prepared, expected


def _require_caller_precondition(
    expected: Mapping[str, object], relative: str, captured_hash: str
) -> None:
    caller_expected = expected.get(relative)
    if caller_expected is not None and caller_expected != captured_hash:
        raise ValueError(f"caller precondition does not match target: {relative}")


def _recovered_duplicate(
    coordinator: MarkdownCoordinator,
    operation_id: str,
    relative_changes: Mapping[str, bytes | None],
    exc: ValueError,
) -> TransactionRecord:
    record = settle_operation(coordinator, operation_id)
    if record is None:
        raise RuntimeError(
            "winning transaction disappeared during duplicate recovery"
        ) from exc
    if _desired_hashes(relative_changes) != _persisted_hashes(record):
        raise exc
    _require_committed_state(record)
    _verify_committed_targets(coordinator, record)
    return record


def _prepare_or_recover(
    coordinator: MarkdownCoordinator,
    prepared: list[MarkdownChange],
    operation_id: str,
    validators: Sequence[Validator],
    expected: Mapping[str, object],
    relative_changes: Mapping[str, bytes | None],
) -> TransactionRecord | None:
    """None when preparation won the race, else the record that already won."""
    try:
        coordinator.prepare(
            prepared,
            operation_id=operation_id,
            validators=validators,
            preconditions=expected,
        )
    except OperationBoundElsewhereError as exc:
        return _recovered_duplicate(coordinator, operation_id, relative_changes, exc)
    return None


def _require_knowledge_changes(changes: Mapping[Path, bytes | None]) -> None:
    if not changes:
        raise ValueError("a knowledge mutation requires at least one change")


def _settled_or_missing(
    coordinator: MarkdownCoordinator, operation_id: str
) -> TransactionRecord:
    """The settled record, or the failure that it vanished before apply."""
    settled = settle_operation(coordinator, operation_id)
    if settled is None:
        raise RuntimeError("prepared transaction disappeared before apply")
    return settled


def mutate_knowledge(
    operation_id: str,
    changes: Mapping[Path, bytes | None],
    *,
    validators: Sequence[Validator] = (),
    preconditions: Mapping[str, object] | None = None,
) -> TransactionRecord:
    """Apply one recoverable mutation with caller-independent before hashes."""
    return mutate_knowledge_in_transaction(
        default_coordinator(), operation_id, changes, validators, preconditions
    )


def mutate_owned_knowledge(
    coordinator: MarkdownCoordinator,
    owner: object,
    operation_id: str,
    changes: Mapping[Path, bytes | None],
    *,
    validators: Sequence[Validator] = (),
    preconditions: Mapping[str, object] | None = None,
) -> TransactionRecord:
    """The same mutation inside a writer gate the caller already owns.

    `mutate_knowledge` claims the canonical writer lease itself, so a caller that
    already holds an owner lease — the capture worker does — got
    `owner_identity_conflict` and, because its own writer swallows failure, lost
    the write in silence. Every queued session record was dropped this way.
    """
    with coordinator.writer_gate(owner=owner):
        return mutate_knowledge_in_transaction(
            coordinator, operation_id, changes, validators, preconditions
        )


def mutate_knowledge_in_transaction(
    coordinator: MarkdownCoordinator,
    operation_id: str,
    changes: Mapping[Path, bytes | None],
    validators: Sequence[Validator],
    preconditions: Mapping[str, object] | None,
) -> TransactionRecord:
    _require_knowledge_changes(changes)
    relative_changes = _relative_changes(coordinator, changes)
    recover_initial_contention(coordinator)
    existing = _existing_committed_record(coordinator, operation_id, relative_changes)
    if existing is not None:
        return existing
    captured = _captured_targets(coordinator, relative_changes)
    prepared, expected = _prepared_changes(relative_changes, captured, preconditions)
    recovered = _prepare_or_recover(
        coordinator, prepared, operation_id, validators, expected, relative_changes
    )
    if recovered is not None:
        return recovered
    return _settled_or_missing(coordinator, operation_id)


def _require_settle_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise TimeoutError("timed out waiting for duplicate transaction preparation")


def settle_operation(
    coordinator: MarkdownCoordinator,
    operation_id: str,
    *,
    deadline: float = float("inf"),
    cancelled: Callable[[], bool] | None = None,
) -> TransactionRecord | None:
    deadline = min(deadline, time.monotonic() + markdown_store.WRITER_WAIT_SECONDS)
    while True:
        coordinator._require_operation_active(deadline, cancelled)
        record = coordinator._record_for_operation_id(operation_id)
        if record is None:
            return None
        settled = _settle_nonpreparing_record(
            coordinator, record, deadline=deadline, cancelled=cancelled
        )
        if settled is not None:
            return settled
        _recover_abandoned_preparation(
            coordinator, operation_id, deadline=deadline, cancelled=cancelled
        )
        _require_settle_deadline(deadline)
        time.sleep(0.01)


def _settle_nonpreparing_record(
    coordinator: MarkdownCoordinator,
    record: TransactionRecord,
    *,
    deadline: float,
    cancelled: Callable[[], bool] | None,
) -> TransactionRecord | None:
    if record.state == "preparing":
        return None
    if record.state in {"prepared", "applying"}:
        return _apply_or_reread_terminal(
            coordinator, record, deadline=deadline, cancelled=cancelled
        )
    return record


def _apply_or_reread_terminal(
    coordinator: MarkdownCoordinator,
    record: TransactionRecord,
    *,
    deadline: float,
    cancelled: Callable[[], bool] | None,
) -> TransactionRecord:
    try:
        return coordinator.apply(record.id, deadline=deadline, cancelled=cancelled)
    except TransactionStateError as exc:
        current = coordinator._record(record.id)
        if exc.state != current.state or current.state in {"prepared", "applying"}:
            raise
        return current


def _preparer_is_alive(coordinator: MarkdownCoordinator, operation_id: str) -> bool:
    with coordinator._connect() as database:
        owner = database.execute(
            'SELECT id, owner_pid FROM "transaction" WHERE operation_id = ?',
            (operation_id,),
        ).fetchone()
    if owner is None:
        return False
    return preparer_alive(
        coordinator.transaction_root / owner["id"], owner["owner_pid"]
    )


def _recover_abandoned_preparation(
    coordinator: MarkdownCoordinator,
    operation_id: str,
    *,
    deadline: float,
    cancelled: Callable[[], bool] | None,
) -> None:
    if _preparer_is_alive(coordinator, operation_id):
        return
    coordinator.recover(deadline=deadline, cancelled=cancelled)


def _verify_committed_targets(
    coordinator: MarkdownCoordinator, record: TransactionRecord
) -> None:
    with coordinator.writer_gate():
        drifted = [
            operation.path
            for operation in record.operations
            if coordinator._current_hash(operation.path) != operation.after_hash
        ]
    if drifted:
        raise TransactionDriftError(record.id, drifted)
