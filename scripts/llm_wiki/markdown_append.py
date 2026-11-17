"""Appends land exactly once, retried under contention and bounded by their stall budget."""
from __future__ import annotations

import hashlib
import sqlite3
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Literal

from bounded_io import VerifiedByteRange
from llm_wiki import markdown_store
from llm_wiki.formats.canonical import sha256_bytes
from llm_wiki.markdown_admission import default_coordinator, recover_initial_contention
from llm_wiki.markdown_coordinator import MarkdownCoordinator
from llm_wiki.markdown_mutation import relative_target, settle_operation
from llm_wiki.markdown_records import (
    MarkdownChange,
    MarkdownOperation,
    OperationBoundElsewhereError,
    PreconditionChangedError,
    TransactionFailure,
    TransactionRecord,
    TransactionStateError,
    require_bytes,
)
from llm_wiki.markdown_store import (
    ABSENT,
    image_bytes,
    is_transient_writer_contention,
    retry_writer_or_raise,
    writer_retry_delay,
)


def _append_request_matches(
    coordinator: MarkdownCoordinator,
    record: TransactionRecord,
    relative: str,
    block: bytes,
) -> bool:
    if not _single_operation_on(record, relative):
        return False
    stored_suffix = _append_suffix_bytes(coordinator, record)
    if stored_suffix is not None:
        return stored_suffix == block
    return _full_after_replays(coordinator, record, block)


def _full_after_replays(coordinator: MarkdownCoordinator, record: TransactionRecord, block: bytes) -> bool:
    """A v1 record's full after-image replays the block when it ends with it over its before."""
    content = _append_after_bytes(coordinator, record)
    if content is None or not content.endswith(block):
        return False
    prefix = content[: len(content) - len(block)] if block else content
    return _append_before_hash(record, prefix) == record.operations[0].before_hash


def _single_operation_on(record: TransactionRecord, relative: str) -> bool:
    return len(record.operations) == 1 and record.operations[0].path == relative


def _append_after_bytes(
    coordinator: MarkdownCoordinator, record: TransactionRecord
) -> bytes | None:
    plan = coordinator._load_verified_plan(record)
    after = plan["operations"][0]["after"]
    if not isinstance(after, dict):
        return None
    return image_bytes(coordinator.transaction_root / record.id / after["artifact"])


def _append_suffix_bytes(
    coordinator: MarkdownCoordinator, record: TransactionRecord
) -> bytes | None:
    """The bytes an append-encoded record added, or None for a full after-image.

    Its after is the before plus exactly these bytes by construction, so it replays a
    block exactly when they are the block (the before-hash is the record's own).
    """
    after = coordinator._load_verified_plan(record)["operations"][0]["after"]
    if not isinstance(after, dict) or "suffix" not in after:
        return None
    return image_bytes(coordinator.transaction_root / record.id / after["suffix"])


def _append_before_hash(record: TransactionRecord, prefix: bytes) -> str:
    if record.operations[0].kind == "create":
        return ABSENT
    return sha256_bytes(prefix)


_AppendAttemptResult = TransactionRecord | Literal["advance", "retry"]


def _coerce_legacy_append_arguments(
    operation_id: str | Path | None,
    path: Path | bytes | None,
    block: bytes | None,
) -> tuple[str | Path | None, Path | bytes | None, bytes | None]:
    legacy_positional = (
        block is None
        and isinstance(operation_id, Path)
        and isinstance(path, bytes)
    )
    if not legacy_positional:
        return operation_id, path, block
    return None, operation_id, path


def _append_operation_id(value: str | Path | None) -> str:
    if value is None:
        return f"append:{uuid.uuid4().hex}"
    if not value:
        raise ValueError("operation_id must be non-empty")
    return value  # type: ignore[return-value]


def _normalize_append_request(
    operation_id: str | Path | None,
    path: Path | bytes | None,
    block: bytes | None,
) -> tuple[str, Path, bytes]:
    operation_id, path, block = _coerce_legacy_append_arguments(
        operation_id, path, block
    )
    if not isinstance(path, Path):
        raise TypeError("knowledge append path must be a Path")
    content = require_bytes(block)
    if len(content) > markdown_store.MAX_KNOWLEDGE_TARGET_BYTES:
        raise ValueError("knowledge append block size exceeds limit")
    return _append_operation_id(operation_id), path, content


def append_candidate_id(operation_id: str, attempt: int) -> str:
    if attempt == 0:
        return operation_id
    return f"{operation_id}:cas:{attempt}"


def _capture_append_before(
    coordinator: MarkdownCoordinator,
    relative: str,
    *,
    deadline: float,
    cancelled: Callable[[], bool] | None,
) -> bytes | None:
    for _attempt in range(64):
        try:
            return _read_append_target_under_gate(
                coordinator, relative, deadline=deadline, cancelled=cancelled
            )
        except TimeoutError:
            continue
    raise TimeoutError("timed out capturing the knowledge append target")


def _read_append_target_under_gate(
    coordinator: MarkdownCoordinator,
    relative: str,
    *,
    deadline: float,
    cancelled: Callable[[], bool] | None,
) -> bytes | None:
    coordinator._require_operation_active(deadline, cancelled)
    with coordinator.writer_gate(
        wait_seconds=min(
            markdown_store.WRITER_WAIT_SECONDS,
            max(0.0, deadline - time.monotonic()),
        )
    ):
        coordinator._require_operation_active(deadline, cancelled)
        return coordinator._read_bounded_target(
            coordinator._target(relative), markdown_store.MAX_KNOWLEDGE_TARGET_BYTES
        )


def _append_change(relative: str, before: bytes | None, block: bytes) -> MarkdownChange:
    content = (before or b"") + block
    if len(content) > markdown_store.MAX_KNOWLEDGE_TARGET_BYTES:
        raise ValueError("prospective knowledge target size exceeds limit")
    if before is None:
        return MarkdownChange.create(
            relative, content, max_before_bytes=markdown_store.MAX_KNOWLEDGE_TARGET_BYTES
        )
    return MarkdownChange.replace(
        relative, content, max_before_bytes=markdown_store.MAX_KNOWLEDGE_TARGET_BYTES
    )


def _append_expected_hash(before: bytes | None) -> str:
    if before is None:
        return ABSENT
    return sha256_bytes(before)


def append_preconditions(
    relative: str,
    before: bytes | None,
    extra: Mapping[str, object] | None,
) -> dict[str, object]:
    result = dict(extra or {})
    expected = _append_expected_hash(before)
    if relative in result and result[relative] != expected:
        raise ValueError("append target precondition conflicts with captured bytes")
    result[relative] = expected
    return result


def _classify_settled_append(
    coordinator: MarkdownCoordinator,
    record: TransactionRecord | None,
    relative: str,
    block: bytes,
) -> _AppendAttemptResult:
    if record is None:
        return "retry"
    if _nothing_to_compare(coordinator, record):
        return _pruned_append_or_advance(coordinator, record, relative, block)
    return _classify_comparable_append(coordinator, record, relative, block)


def _block_offsets(content: bytes, block: bytes):
    if not block:
        return
    position = content.find(block)
    while position >= 0:
        yield position
        position = content.find(block, position + 1)


def _append_prefix_before_matches(operation: MarkdownOperation, position: int, digest) -> bool:
    if operation.kind == "create":
        return position == 0 and operation.before_hash == ABSENT
    return digest.hexdigest() == operation.before_hash


def _append_prefix_hashes_match(operation: MarkdownOperation, position: int, digest, block: bytes) -> bool:
    if not _append_prefix_before_matches(operation, position, digest):
        return False
    after = digest.copy()
    after.update(block)
    return after.hexdigest() == operation.after_hash


def committed_append_content_matches(record: TransactionRecord, relative: str, block: bytes, content: bytes) -> bool:
    """Prove the original append from retained hashes, even after undo pruning."""
    if record.state != "committed" or not _single_operation_on(record, relative):
        return False
    return _committed_block_matches(record.operations[0], block, content)


def _committed_block_matches(operation: MarkdownOperation, block: bytes, content: bytes) -> bool:
    digest, hashed = hashlib.sha256(), 0
    for position in _block_offsets(content, block):
        _update_append_prefix(digest, content, hashed, position)
        hashed = position
        if _append_prefix_hashes_match(operation, position, digest, block):
            return True
    return False


def _update_append_prefix(digest, content, start, end):
    if type(content) is VerifiedByteRange:
        for chunk in content.iter_chunks(start, end):
            digest.update(chunk)
        return
    digest.update(memoryview(content)[start:end])


def _pruned_append_or_advance(coordinator, record, relative: str, block: bytes) -> _AppendAttemptResult:
    if record.state != "committed" or not record.operations:
        return "advance"
    content = coordinator._read_target(coordinator._target(relative))
    if content is None:
        return "advance"
    return _classify_pruned_content(record, relative, block, content)


def _classify_pruned_content(record, relative: str, block: bytes, content: bytes) -> _AppendAttemptResult:
    if committed_append_content_matches(record, relative, block, content):
        return record
    if block and block in content:
        raise OperationBoundElsewhereError("pruned append conflicts with retained hash evidence")
    return "advance"


def _nothing_to_compare(
    coordinator: MarkdownCoordinator, record: TransactionRecord
) -> bool:
    """A record without comparable undo images needs current-content proof.

    Either its images are gone with the undo window, or it was recovered before it
    planned anything (its writer died inside `prepare`) and so wrote nothing.
    """
    if not record.operations:
        return True
    return coordinator._artifacts_pruned(record.id)


def _classify_comparable_append(
    coordinator: MarkdownCoordinator,
    record: TransactionRecord,
    relative: str,
    block: bytes,
) -> _AppendAttemptResult:
    """A record whose images still exist: the same request, or a different one."""
    if not _append_request_matches(coordinator, record, relative, block):
        raise OperationBoundElsewhereError("operation_id is already bound to a different request")
    if record.state != "committed":
        return "advance"
    return _committed_append_or_rewrite(coordinator, record, relative)


def _committed_append_or_rewrite(
    coordinator: MarkdownCoordinator, record: TransactionRecord, relative: str
) -> _AppendAttemptResult:
    """The earlier append still stands, unless its target is no longer there.

    A committed record used to be answered with `committed` whatever had become
    of the file, so append, delete, append said "written" over a file that did
    not exist. A target that is gone took the block with it, so the next
    candidate id writes again. Bytes that merely changed are not checked, nor
    even read: a daily log grows under every other writer, and that is not this
    block's disappearance. See
    `docs/research/2026-09-18-a-committed-append-still-needs-its-file.md` and
    `docs/research/2026-09-23-a-presence-check-does-not-hash.md`.
    """
    if coordinator._target_present(relative):
        return record
    return "advance"


def _append_transaction_failure(error: TransactionFailure) -> Literal["advance"]:
    retryable = {"before_hash_mismatch", "unknown_target_bytes", "precondition_failed"}
    if error.code not in retryable:
        raise error
    return "advance"


def _settle_append_candidate(
    coordinator: MarkdownCoordinator,
    candidate_id: str,
    relative: str,
    block: bytes,
    *,
    deadline: float,
    cancelled: Callable[[], bool] | None,
) -> _AppendAttemptResult:
    try:
        record = settle_operation(
            coordinator, candidate_id, deadline=deadline, cancelled=cancelled
        )
    except TransactionFailure as exc:
        return _append_transaction_failure(exc)
    except TransactionStateError:
        # The candidate settled somewhere else; this id is spent, the next writes.
        return "advance"
    except TimeoutError:
        return "retry"
    return _classify_settled_append(coordinator, record, relative, block)


def _append_value_failure(
    error: ValueError,
    coordinator: MarkdownCoordinator,
    candidate_id: str,
    relative: str,
    block: bytes,
    *,
    deadline: float,
    cancelled: Callable[[], bool] | None,
) -> _AppendAttemptResult:
    if isinstance(error, OperationBoundElsewhereError):
        return _settle_append_candidate(
            coordinator,
            candidate_id,
            relative,
            block,
            deadline=deadline,
            cancelled=cancelled,
        )
    if isinstance(error, PreconditionChangedError):
        return "retry"
    raise error


def _append_prepare_failure(
    error: Exception,
    coordinator: MarkdownCoordinator,
    candidate_id: str,
    relative: str,
    block: bytes,
    *,
    deadline: float,
    cancelled: Callable[[], bool] | None,
) -> _AppendAttemptResult:
    if isinstance(error, TransactionFailure):
        return _append_transaction_failure(error)
    if isinstance(error, ValueError):
        return _append_value_failure(
            error,
            coordinator,
            candidate_id,
            relative,
            block,
            deadline=deadline,
            cancelled=cancelled,
        )
    return _append_other_failure(error)


def _append_other_failure(error: Exception) -> _AppendAttemptResult:
    """A candidate that settled elsewhere is spent; a timeout is retried."""
    if isinstance(error, TransactionStateError):
        return "advance"
    if isinstance(error, RuntimeError):
        raise error
    return "retry"


def _prepare_append_candidate(
    coordinator: MarkdownCoordinator,
    candidate_id: str,
    relative: str,
    before: bytes | None,
    block: bytes,
    *,
    extra_preconditions: Mapping[str, object] | None,
    content_guard: Literal["model_output"] | None,
    parent_transaction_id: str | None,
    deadline: float,
    cancelled: Callable[[], bool] | None,
) -> _AppendAttemptResult:
    change = _append_change(relative, before, block)
    try:
        coordinator.prepare(
            [change],
            operation_id=candidate_id,
            preconditions=append_preconditions(relative, before, extra_preconditions),
            content_guard=content_guard,
            _parent_transaction_id=parent_transaction_id,
            deadline=deadline,
            cancelled=cancelled,
        )
    except (
        FileExistsError,
        FileNotFoundError,
        ValueError,
        TransactionFailure,
        RuntimeError,
        TimeoutError,
    ) as exc:
        return _append_prepare_failure(
            exc,
            coordinator,
            candidate_id,
            relative,
            block,
            deadline=deadline,
            cancelled=cancelled,
        )
    return _settle_append_candidate(
        coordinator,
        candidate_id,
        relative,
        block,
        deadline=deadline,
        cancelled=cancelled,
    )


def run_append_candidate(
    coordinator: MarkdownCoordinator,
    candidate_id: str,
    relative: str,
    block: bytes,
    *,
    extra_preconditions: Mapping[str, object] | None,
    content_guard: Literal["model_output"] | None,
    parent_transaction_id: str | None,
    deadline: float,
    cancelled: Callable[[], bool] | None,
) -> _AppendAttemptResult:
    if coordinator._record_for_operation_id(candidate_id) is not None:
        return _settle_append_candidate(
            coordinator,
            candidate_id,
            relative,
            block,
            deadline=deadline,
            cancelled=cancelled,
        )
    before = _capture_append_before(
        coordinator, relative, deadline=deadline, cancelled=cancelled
    )
    return _prepare_append_candidate(
        coordinator,
        candidate_id,
        relative,
        before,
        block,
        extra_preconditions=extra_preconditions,
        content_guard=content_guard,
        parent_transaction_id=parent_transaction_id,
        deadline=deadline,
        cancelled=cancelled,
    )


def _contended_append_candidate(coordinator: MarkdownCoordinator, candidate_id: str, *args, **kwargs):
    """One attempt; a database busy past its timeout repeats the same attempt.

    Four writers on Windows let `database is locked` escape the append and fail
    the caller's write (CI run 36204706220, audit 2026-09-26 B-27). The stall
    guard and the caller's deadline still bound how long it may repeat.
    Research: docs/research/2026-09-26-a-busy-append-repeats-its-attempt.md
    """
    try:
        return run_append_candidate(coordinator, candidate_id, *args, **kwargs)
    except PreconditionChangedError:
        # Capture can lose its snapshot before prepare's own retry boundary.
        return "retry"
    except (OSError, sqlite3.Error) as exc:
        if not is_transient_writer_contention(exc):
            raise
        time.sleep(writer_retry_delay(0, kwargs["deadline"]))
        return "retry"


def refused_parent(coordinator: MarkdownCoordinator, candidate_id: str) -> str | None:
    """The quarantined attempt a retry follows, when the refusal was recorded.

    A lost compare-and-swap is quarantined, and quarantine is kept as evidence.
    Naming it here is what lets the record resolve itself: doctor clears a
    refused attempt once a committed transaction in its own chain names it, the
    same lineage `idempotent-retry-after-quarantine-decision.md` gave the
    compile path. Without it an append to an existing file — a `replace`, so
    the create-outcome proof cannot speak for it — stays an open finding
    forever, and a health check that can never go green stops being read.
    """
    record = _record_when_readable(coordinator, candidate_id)
    if record is None or record.state != "quarantined":
        return None
    return record.id


def _record_when_readable(coordinator: MarkdownCoordinator, operation_id: str):
    """The record, read again while the database is only busy (audit 2026-09-26 B-27)."""
    deadline = time.monotonic() + markdown_store.WRITER_WAIT_SECONDS
    attempt = 0
    while True:
        try:
            return coordinator._record_for_operation_id(operation_id)
        except (OSError, sqlite3.Error) as exc:
            attempt = retry_writer_or_raise(exc, attempt, deadline)


# How long an append may repeat one attempt without advancing before it gives up:
# several full settle windows, so ordinary contention never reaches it. See
# `docs/research/2026-09-17-a-transaction-that-is-over-stays-over.md`.
_APPEND_STALL_SECONDS = 6 * markdown_store.WRITER_WAIT_SECONDS


class _AppendStall:
    """The time an append has spent without advancing to a new attempt."""

    def __init__(self, seconds: float) -> None:
        self._seconds = seconds
        self._since = time.monotonic()

    def progressed(self) -> None:
        self._since = time.monotonic()

    def require_moving(self) -> None:
        if time.monotonic() - self._since >= self._seconds:
            raise TimeoutError(
                "knowledge append stalled behind an attempt that never settled"
            )


def _require_append_retry_preconditions(
    coordinator: MarkdownCoordinator,
    extra_preconditions: Mapping[str, object] | None,
) -> None:
    """Retry a fresh target CAS only while the caller's claim still holds."""
    if not extra_preconditions:
        return
    expected = coordinator._validate_preconditions(extra_preconditions)
    with coordinator._connect() as database:
        coordinator._check_preconditions(expected, {}, database=database)


def append_until_committed(
    coordinator: MarkdownCoordinator,
    operation_id: str,
    relative: str,
    block: bytes,
    *,
    extra_preconditions: Mapping[str, object] | None = None,
    content_guard: Literal["model_output"] | None = None,
    deadline: float,
    cancelled: Callable[[], bool] | None,
    stall_seconds: float = _APPEND_STALL_SECONDS,
) -> TransactionRecord:
    attempt = 0
    parent: str | None = None
    stall = _AppendStall(stall_seconds)
    while True:
        # `retry` repeats the same attempt, so the loop itself has to notice the
        # caller's deadline and an attempt that never settles.
        coordinator._require_operation_active(deadline, cancelled)
        stall.require_moving()
        candidate_id = append_candidate_id(operation_id, attempt)
        outcome = _contended_append_candidate(
            coordinator,
            candidate_id,
            relative,
            block,
            extra_preconditions=extra_preconditions,
            content_guard=content_guard,
            parent_transaction_id=parent,
            deadline=deadline,
            cancelled=cancelled,
        )
        if isinstance(outcome, TransactionRecord):
            return outcome
        if outcome == "advance":
            _require_append_retry_preconditions(coordinator, extra_preconditions)
            parent = refused_parent(coordinator, candidate_id) or parent
            attempt += 1
            stall.progressed()


def append_knowledge(
    operation_id: str | Path | None = None,
    path: Path | bytes | None = None,
    block: bytes | None = None,
    *,
    deadline: float = float("inf"),
    cancelled: Callable[[], bool] | None = None,
) -> TransactionRecord:
    """CAS-append Markdown or project JSONL bytes, retrying concurrent winners."""
    return _append_knowledge_with_coordinator(
        default_coordinator(), operation_id, path, block,
        deadline=deadline, cancelled=cancelled,
    )


def append_owned_knowledge(
    coordinator: MarkdownCoordinator,
    owner: object,
    operation_id: str,
    path: Path,
    block: bytes,
    *,
    deadline: float = float("inf"),
) -> TransactionRecord:
    """Use the canonical CAS append inside a lifecycle worker's existing gate."""
    with coordinator.writer_gate(owner=owner):
        return _append_knowledge_with_coordinator(
            coordinator, operation_id, path, block, deadline=deadline, cancelled=None
        )


def _append_knowledge_with_coordinator(
    coordinator: MarkdownCoordinator,
    operation_id: str | Path | None,
    path: Path | bytes | None,
    block: bytes | None,
    *,
    deadline: float,
    cancelled: Callable[[], bool] | None,
) -> TransactionRecord:
    operation_id, append_path, block = _normalize_append_request(
        operation_id, path, block
    )
    relative = relative_target(coordinator, append_path)
    recover_initial_contention(
        coordinator, deadline=deadline, cancelled=cancelled
    )
    return append_until_committed(
        coordinator,
        operation_id,
        relative,
        block,
        deadline=deadline,
        cancelled=cancelled,
    )


def _capture_append_context_matches(
    stored: Mapping[str, object],
    current: Mapping[str, object],
    inherited: Sequence[Mapping[str, object]] = (),
) -> bool:
    """The committed append was made under this binding, or one a redrive inherited."""
    stored_fence = stored.get("intent_fence")
    current_fence = current.get("intent_fence")
    if not isinstance(stored_fence, Mapping) or not isinstance(current_fence, Mapping):
        return False
    bindings = [current.get("capture_binding"), *inherited]
    return stored.get("capture_binding") in bindings and (
        stored_fence.get("intent_id"),
        stored_fence.get("mode"),
    ) == (
        current_fence.get("intent_id"),
        current_fence.get("mode"),
    )


def append_captured_knowledge(
    coordinator: MarkdownCoordinator,
    owner: object,
    operation_id: str,
    path: Path,
    block: bytes,
    *,
    preconditions: Mapping[str, object],
    inherited_bindings: Sequence[Mapping[str, object]] = (),
    deadline: float = float("inf"),
    cancelled: Callable[[], bool] | None = None,
) -> TransactionRecord:
    """CAS-append one provider decision under live capture preconditions.

    `inherited_bindings` are the sealed bindings of the task's redrive ancestors for
    the same decision: a block one of them committed before it died is this
    redrive's own, not a conflict (audit 2026-09-27 B-14).
    """
    operation_id, append_path, content = _normalize_append_request(
        operation_id, path, block
    )
    relative = relative_target(coordinator, append_path)
    expected = coordinator._validate_preconditions(preconditions)
    with coordinator.writer_gate(owner=owner):
        with coordinator._connect() as database:
            coordinator._check_capture_preconditions(database, expected)
        recover_initial_contention(
            coordinator, deadline=deadline, cancelled=cancelled
        )
        record = append_until_committed(
            coordinator,
            operation_id,
            relative,
            content,
            extra_preconditions=preconditions,
            content_guard="model_output",
            deadline=deadline,
            cancelled=cancelled,
        )
    if not _capture_append_context_matches(record.preconditions, expected, inherited_bindings):
        raise ValueError("capture append transaction preconditions conflict")
    return record
