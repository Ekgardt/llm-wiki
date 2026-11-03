"""Queue data contracts; legacy public pickle names remain in memory_queue."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

# Shared existing result ceiling for queue storage and child byte transport.
_MAX_RESULT_BYTES = 16 * 1024 * 1024

# Existing base filter states; v3 schema also has quarantine/purge states.
_STATES = ("ready", "leased", "blocked", "succeeded", "dead", "cancelled")

# Ceiling on a task's configured attempts and on its attempt history. basis unknown — value predates measurement; review when a task reaches it in normal operation.
_MAX_RUNTIME_ATTEMPTS = 100


class LeaseFenceError(RuntimeError):
    """Raised when a lease token no longer owns an unexpired task."""


class ResultConflictError(RuntimeError):
    """Raised when an operation ID already names different result bytes."""


class QueueOperationError(RuntimeError):
    """Operator-visible queue failure with a stable, non-sensitive code."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail

    def __reduce__(self):
        """Keep the code and detail apart when the error crosses a process."""
        return (self.__class__, (self.code, self.detail))


class CompileCompletionError(QueueOperationError):
    """A new-schema compile task lacks a valid whole-work attestation."""


class MigrationBusy(QueueOperationError):
    """Raised when migration cannot prove exclusive legacy ownership."""


@dataclass(frozen=True)
class QueueFailure:
    error_code: str
    permanent: bool = False
    blocked_capability: str | None = None
    retry_after: float | datetime | str | None = None


@dataclass(frozen=True)
class DeferredResult:
    data: bytes


@dataclass(frozen=True)
class AttemptRecord:
    attempt: int
    started_at: datetime
    finished_at: datetime
    outcome: str
    error_code: str | None


@dataclass(frozen=True)
class QueueTask:
    id: str
    kind: str
    handler_version: int
    payload: dict[str, object]
    input_hash: str
    dedupe_key: str | None
    state: str
    priority: int
    created_at: datetime
    updated_at: datetime
    available_at: datetime
    attempts: int
    last_attempt_at: datetime | None
    lease_owner: str | None
    lease_token: str | None
    lease_expires_at: datetime | None
    lease_heartbeat_at: datetime | None
    error_code: str | None
    blocked_capability: str | None
    result_reference: str | None
    result_sha256: str | None
    redrive_of: str | None
    attempt_history: tuple[AttemptRecord, ...]


@dataclass(frozen=True)
class QueueLease:
    id: str
    kind: str
    handler_version: int
    payload: dict[str, object]
    input_hash: str
    owner: str
    token: str
    expires_at: datetime
    attempt: int
    created_at: datetime
    last_attempt_at: datetime | None
    prior_attempts: int


@dataclass(frozen=True)
class CaptureTaskBinding:
    task_id: str
    intent_id: str | None
    intent_sha256: str | None
    handler_version: int
    active_digest: str
    seal_digest: str | None


@dataclass(frozen=True)
class SemanticDecision:
    task_id: str
    intent_id: str
    stage: Literal["flush", "feedback", "feedback-verify"]
    decision_path: str
    decision_sha256: str
    active_link_digest: str
    seal_digest: str
    published_at: datetime


@dataclass(frozen=True)
class CorruptExportProgress:
    task_id: str
    operation_id: str
    state: Literal["quarantine_pending", "quarantined", "blocked"]
    pages_written: int
    links_exported: int
    complete: bool
    code: str | None


@dataclass(frozen=True)
class CorruptPurgeProgress:
    task_id: str
    operation_id: str
    state: Literal["purge_pending", "purged", "blocked"]
    pages_written: int
    links_deleted: int
    complete: bool
    code: str | None


@dataclass(frozen=True)
class SourceFence:
    daily_id: str
    source_digest: str
    token: str
    owner_pid: int
    acquired_at: datetime
    heartbeat_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class WorkerSummary:
    processed: int
    succeeded: int
    failed: int
    dead: int
    skipped: int
    remaining_eligible: int = 0


@dataclass(frozen=True)
class PurgeReceipt:
    purged: int
    task_ids: tuple[str, ...]
    # Finished rows retained by unresolved capture or compile history;
    # see `_rows_with_releasable_evidence`.
    retained: tuple[str, ...] = ()


@dataclass(frozen=True)
class DeadCaptureRedrive:
    redriven: tuple[tuple[str, str], ...]
    refused: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class RestoreReceipt:
    restored: int
    task_ids: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class LeaseProcessor:
    """Opt in to worker-owned queue/lease/deadline arguments outside task payloads.

    The callable receives (queue, lease, deadline, compatibility_task). It must be
    spawn-picklable, as ordinary processors must be. The parent retains heartbeat
    and child cleanup ownership; the child verifies authority at each transition.
    """

    process: Callable[..., bool | DeferredResult]


# Preserve the existing public serialization ABI across process updates.
_PUBLIC_TYPES = (
    LeaseFenceError,
    ResultConflictError,
    QueueOperationError,
    CompileCompletionError,
    MigrationBusy,
    QueueFailure,
    DeferredResult,
    AttemptRecord,
    QueueTask,
    QueueLease,
    CaptureTaskBinding,
    SemanticDecision,
    CorruptExportProgress,
    CorruptPurgeProgress,
    SourceFence,
    WorkerSummary,
    PurgeReceipt,
    DeadCaptureRedrive,
    RestoreReceipt,
    LeaseProcessor,
)
__all__ = [kind.__name__ for kind in _PUBLIC_TYPES]
for _kind in _PUBLIC_TYPES:
    _kind.__module__ = "memory_queue"
del _kind
