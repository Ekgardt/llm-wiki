"""Public Markdown transaction values and failures, independent of execution."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from llm_wiki.ownership_contracts import OwnerLease


ChangeKind = Literal["create", "replace", "delete"]


@dataclass(frozen=True)
class MarkdownChange:
    kind: ChangeKind
    path: str
    content: bytes | None
    max_before_bytes: int | None = None

    @classmethod
    def create(
        cls, path: str, content: bytes, *, max_before_bytes: int | None = None
    ) -> MarkdownChange:
        return cls("create", path, _require_bytes(content), max_before_bytes)

    @classmethod
    def replace(
        cls, path: str, content: bytes, *, max_before_bytes: int | None = None
    ) -> MarkdownChange:
        return cls("replace", path, _require_bytes(content), max_before_bytes)

    @classmethod
    def delete(cls, path: str, *, max_before_bytes: int | None = None) -> MarkdownChange:
        return cls("delete", path, None, max_before_bytes)


@dataclass(frozen=True)
class MarkdownOperation:
    kind: ChangeKind
    path: str
    before_hash: str
    after_hash: str


@dataclass(frozen=True)
class CommittedWriteAuthority:
    """Recorded write evidence; does not claim to contain the complete transaction."""

    id: str
    operation_id: str
    state: str
    operations: tuple[MarkdownOperation, ...]


@dataclass(frozen=True)
class TransactionRecord:
    id: str
    operation_id: str
    state: str
    operations: tuple[MarkdownOperation, ...]
    preconditions: Mapping[str, object]
    created_at: str
    updated_at: str
    parent_transaction_id: str | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class IntentFence:
    intent_id: str
    mode: Literal["capture", "worker", "operator"]
    token: str
    epoch: int
    owner: OwnerLease
    expires_at: datetime


@dataclass(frozen=True)
class TransactionAbortReceipt:
    transaction_id: str
    intent_id: str
    abort_operation_id: str
    receipt_path: str
    receipt_sha256: str
    aborted_at: str


@dataclass(frozen=True)
class ProjectCheckpointReservation:
    project: str
    sequence: int
    occurrence_id: str
    idempotency_key: str
    event_json: str
    operation_id: str
    attempt_number: int
    state: str
    transaction_id: str | None = None
    parent_operation_id: str | None = None
    duplicate: bool = False


class OperationBoundElsewhereError(ValueError):
    """Another request already owns this operation id; the event is retried."""


class TransactionFailure(RuntimeError):
    """An apply failure with a stable machine-readable disposition."""

    def __init__(self, message: str, code: str, state: str):
        super().__init__(message)
        self.code = code
        self.state = state

    def __reduce__(self):
        """Carry the disposition across a process boundary, not just the text."""
        return (self.__class__, (str(self), self.code, self.state))


class TransactionDriftError(RuntimeError):
    """A committed operation no longer matches its persisted after-state."""

    def __init__(self, transaction_id: str, paths: Sequence[str]):
        self.transaction_id = transaction_id
        self.paths = tuple(paths)
        super().__init__(
            "committed transaction target drift detected; use a new operation_id: "
            + ", ".join(paths)
        )

    def __reduce__(self):
        return (self.__class__, (self.transaction_id, self.paths))


class WriterAdmissionTimeout(TimeoutError):
    """The caller deadline expired before acquiring the Markdown writer gate."""


class ProjectPendingPriorError(TransactionFailure):
    """A project checkpoint is blocked by an unapplied lower sequence."""

    status = "pending_prior"

    def __init__(self, project: str, sequence: int, prior_sequence: int):
        super().__init__(
            f"project {project!r} sequence {sequence} waits for sequence {prior_sequence}",
            "pending_prior",
            "prepared",
        )
        self.project = project
        self.sequence = sequence
        self.prior_sequence = prior_sequence

    def __reduce__(self):
        """This subclass takes its own arguments, not the base three."""
        return (self.__class__, (self.project, self.sequence, self.prior_sequence))


class TargetBoundaryFailure(RuntimeError):
    """A persisted target no longer has its prepared containment identity."""


class TargetTooLargeError(ValueError):
    """A transaction target exceeds the authoritative target-size contract."""


class TargetPathBoundaryError(ValueError):
    """A requested path is not a containable target inside the vault."""


class PreconditionChangedError(ValueError):
    """A target changed between the caller's read and its prepare."""


class TransactionImageError(RuntimeError):
    """A staged plan or after-image is not the one that was prepared."""


class TransactionStateError(RuntimeError):
    """A transaction cannot be applied from the state it is in."""

    def __init__(self, state: str):
        super().__init__(f"transaction cannot be applied from state {state}")
        self.state = state

    def __reduce__(self):
        """Carry the state across a process boundary, not just the text."""
        return (self.__class__, (self.state,))


class TargetStateMismatch(RuntimeError):
    """A target's bytes are not the ones the transaction recorded for it."""

    def __init__(self, state_name: str, path: str):
        super().__init__(f"{state_name} state mismatch for {path}")
        self.state_name = state_name
        self.path = path

    def __reduce__(self):
        return (self.__class__, (self.state_name, self.path))


class RefusedOperation(RuntimeError):
    """A refusal the CLI reports by its own code, never by its wording."""

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code

    def __reduce__(self):
        return (self.__class__, (str(self), self.code))


class RefusedArgument(ValueError):
    """An argument refusal the CLI reports by its own code."""

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code

    def __reduce__(self):
        return (self.__class__, (str(self), self.code))


def _require_bytes(content: bytes) -> bytes:
    if not isinstance(content, bytes):
        raise TypeError("Markdown content must be bytes")
    return content


# Historical public import paths remain the serialization identity.
__all__ = ['ChangeKind', 'MarkdownChange', 'MarkdownOperation', 'CommittedWriteAuthority', 'TransactionRecord', 'IntentFence', 'TransactionAbortReceipt', 'ProjectCheckpointReservation', 'OperationBoundElsewhereError', 'TransactionFailure', 'TransactionDriftError', 'WriterAdmissionTimeout', 'ProjectPendingPriorError', 'TargetBoundaryFailure', 'TargetTooLargeError', 'TargetPathBoundaryError', 'PreconditionChangedError', 'TransactionImageError', 'TransactionStateError', 'TargetStateMismatch', 'RefusedOperation', 'RefusedArgument']
MarkdownChange.__module__ = 'markdown_transaction'
MarkdownOperation.__module__ = 'markdown_transaction'
CommittedWriteAuthority.__module__ = 'markdown_transaction'
TransactionRecord.__module__ = 'markdown_transaction'
IntentFence.__module__ = 'markdown_transaction'
TransactionAbortReceipt.__module__ = 'markdown_transaction'
ProjectCheckpointReservation.__module__ = 'markdown_transaction'
OperationBoundElsewhereError.__module__ = 'markdown_transaction'
TransactionFailure.__module__ = 'markdown_transaction'
TransactionDriftError.__module__ = 'markdown_transaction'
WriterAdmissionTimeout.__module__ = 'markdown_transaction'
ProjectPendingPriorError.__module__ = 'markdown_transaction'
TargetBoundaryFailure.__module__ = 'markdown_transaction'
TargetTooLargeError.__module__ = 'markdown_transaction'
TargetPathBoundaryError.__module__ = 'markdown_transaction'
PreconditionChangedError.__module__ = 'markdown_transaction'
TransactionImageError.__module__ = 'markdown_transaction'
TransactionStateError.__module__ = 'markdown_transaction'
TargetStateMismatch.__module__ = 'markdown_transaction'
RefusedOperation.__module__ = 'markdown_transaction'
RefusedArgument.__module__ = 'markdown_transaction'
_require_bytes.__module__ = 'markdown_transaction'
