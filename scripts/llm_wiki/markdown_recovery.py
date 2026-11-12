"""Transaction recovery: promotion, rollback and corrupt after-image repair."""

from __future__ import annotations

import json
import sqlite3
import time
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from operational_ownership import OwnerLease
from llm_wiki import markdown_store
from llm_wiki.formats.canonical import canonical_json_bytes, sha256_bytes
from llm_wiki.formats.schema import validate_schema
from llm_wiki.markdown_records import (
    ProjectPendingPriorError,
    TargetBoundaryFailure,
    TargetStateMismatch,
    TransactionFailure,
    TransactionImageError,
    TransactionRecord,
)
from llm_wiki.markdown_store import (
    ABSENT,
    CONFLICTED_STATE_CODES,
    UNAVAILABLE_STATE,
    UNDO_RETENTION_DAYS,
    encode_parent_identity,
    inverse_change_kind,
    is_target_boundary_error,
    optional_before_state,
    parse_stored_timestamp,
    plan_schema,
    preparer_alive,
    stored_before_state,
)
from llm_wiki.sqlite_transactions import begin_immediate
from model_dlp import DLPContentBlocked, DLPPolicyError
from reliable_memory import (
    read_runtime_bytes,
)

_RECOVERY_MANIFEST_FIELDS = {
    "schema_version",
    "transaction_id",
    "request_hash",
    "plan_hash",
    "operations",
}
_PERSISTED_OPERATION_FIELDS = {
    "position",
    "before_hash",
    "after_hash",
    "parent_device",
    "parent_inode",
}


# Which side of a recorded operation must be absent, per kind.
_OPERATION_ABSENCE = {
    "create": (True, False),
    "replace": (False, False),
    "delete": (False, True),
}


class _BuiltOperation(NamedTuple):
    row: tuple
    change: dict
    parent_mismatch: bool


class _PromotionPlan(NamedTuple):
    manifest: dict
    operations: list
    parent_mismatch: bool


def _manifest_identity_matches(
    manifest: Mapping[str, object], transaction_id: str, plan_bytes: bytes
) -> bool:
    if manifest["schema_version"] != "markdown-transaction-recovery/v1":
        return False
    if manifest["transaction_id"] != transaction_id:
        return False
    return manifest["plan_hash"] == sha256_bytes(plan_bytes)


def _comparable_operation_lists(plan_operations: object, manifest_operations: object) -> bool:
    if not isinstance(plan_operations, list) or not isinstance(manifest_operations, list):
        return False
    return len(plan_operations) == len(manifest_operations)


def _persisted_integers(persisted: Mapping[str, object]) -> bool:
    if type(persisted["position"]) is not int:
        return False
    if type(persisted["parent_device"]) is not int:
        return False
    return type(persisted["parent_inode"]) is int


def _persisted_shape_matches(persisted: object, position: int) -> bool:
    return (
        isinstance(persisted, dict)
        and set(persisted) == _PERSISTED_OPERATION_FIELDS
        and persisted["position"] == position
        and _persisted_integers(persisted)
    )


def _validated_persisted_operation(
    persisted: object, position: int, operation: object
) -> tuple | None:
    """The encoded parent identity, or None when the pair does not line up."""
    if not isinstance(operation, dict):
        return None
    if not _persisted_shape_matches(persisted, position):
        return None
    try:
        return encode_parent_identity(
            (persisted["parent_device"], persisted["parent_inode"])
        )
    except ValueError:
        return None


def _kind_transition_valid(kind: object, before_hash: str, after_hash: str) -> bool:
    expected = _OPERATION_ABSENCE.get(kind)
    if expected is None:
        return True
    return (before_hash == ABSENT, after_hash == ABSENT) == expected


def _operation_hashes_consistent(
    persisted: Mapping[str, object], kind: object, before_hash: str, after_hash: str
) -> bool:
    if persisted["before_hash"] != before_hash:
        return False
    if persisted["after_hash"] != after_hash:
        return False
    return _kind_transition_valid(kind, before_hash, after_hash)


def _require_recovery_bound(max_transactions: object) -> None:
    if max_transactions is None:
        return
    if (
        isinstance(max_transactions, bool)
        or not isinstance(max_transactions, int)
        or max_transactions < 0
    ):
        raise ValueError("max_transactions must be a non-negative integer or None")


def _required_state_hash(value: object) -> str:
    """The recorded sha256 of one transaction state, refused unless it is a string."""
    if not isinstance(value, str):
        raise ValueError("invalid transaction state hash")
    return value


def _recovery_still_concerns(row: sqlite3.Row, cutoff: datetime) -> bool:
    """Every unfinished row, and an aborted one only inside the undo window."""
    if row["state"] != "aborted":
        return True
    return parse_stored_timestamp(row["updated_at"]) >= cutoff


# A full image, an append's before length, or an append's added bytes (plan v2).
_STATE_DESCRIPTION_SHAPES = frozenset(
    {
        frozenset({"sha256", "artifact"}),
        frozenset({"sha256", "length"}),
        frozenset({"sha256", "base_length", "suffix", "suffix_sha256"}),
    }
)


class _TargetBoundaryChanged(Exception):
    """A target's parent identity changed; recovery stops and quarantines."""


def _inverse_operation(transaction_id: str, row: sqlite3.Row) -> dict[str, object]:
    return {
        "transaction_id": transaction_id,
        "position": row["position"],
        "kind": inverse_change_kind(row["before_hash"], row["after_hash"]),
        "path": row["path"],
        "before_hash": row["after_hash"],
        "after_hash": row["before_hash"],
        "parent_device": row["parent_device"],
        "parent_inode": row["parent_inode"],
    }


def _abort_receipt_matches(
    receipt: Mapping[str, object],
    receipt_bytes: bytes,
    transaction_id: str,
    row: sqlite3.Row,
) -> bool:
    return (
        canonical_json_bytes(receipt) == receipt_bytes
        and receipt["transaction_id"] == transaction_id
        and receipt["abort_operation_id"] == row["abort_operation_id"]
        and receipt["before_manifest_sha256"] == row["abort_manifest_sha256"]
        and sha256_bytes(receipt_bytes) == row["abort_receipt_sha256"]
    )


def _promotion_plan(
    manifest: Mapping[str, object], built: Sequence[object]
) -> _PromotionPlan:
    return _PromotionPlan(
        dict(manifest),
        [item.row for item in built],
        any(item.parent_mismatch for item in built),
    )


class TransactionRecovery:
    def _incomplete_transaction_rows(self, max_transactions: int | None) -> list[tuple]:
        """Work that can still move goes first; a fresh abort is checked last.

        An `aborted` row is terminal. It is looked at only while it has no
        verdict and the undo window is open — the time a crash around its
        receipt can still matter — so old aborts neither cost every `prepare`
        nor use up a bounded caller's budget ahead of a crashed `applying` row.
        See `docs/research/2026-09-17-a-transaction-that-is-over-stays-over.md`.
        """
        query = (
            'SELECT id, state, owner_pid, updated_at FROM "transaction" '
            "WHERE state IN ('aborting','preparing','prepared','applying') "
            "OR (state = 'aborted' AND error_code IS NULL) "
            "ORDER BY CASE state WHEN 'aborting' THEN 0 WHEN 'aborted' THEN 2 ELSE 1 END, "
            "created_at, id"
        )
        cutoff = datetime.now(UTC) - timedelta(days=UNDO_RETENTION_DAYS)
        with self._connect() as database:
            selected = [
                (row["id"], row["state"], row["owner_pid"])
                for row in database.execute(query)
                if _recovery_still_concerns(row, cutoff)
            ]
        return selected if max_transactions is None else selected[:max_transactions]

    def _quarantine_dlp(self, transaction_id: str, exc: BaseException) -> None:
        code = "dlp_content_blocked"
        if isinstance(exc, DLPPolicyError):
            code = "dlp_policy_error"
        self._rollback_for_quarantine(transaction_id, code)

    def _settle_transaction_failure(
        self, transaction_id: str, exc: TransactionFailure
    ) -> None:
        if exc.code == "precondition_failed":
            self._rollback_for_quarantine(transaction_id, exc.code)
            return
        self._set_transaction_state(transaction_id, exc.state, error_code=exc.code)

    def _conflicted(self, transaction_id: str, code: str) -> TransactionRecord:
        self._set_transaction_state(transaction_id, "conflicted", error_code=code)
        return self._record(transaction_id)

    def _before_mismatch_code(self, transaction_id: str) -> str:
        create_conflict = any(
            row["kind"] == "create" and self._operation_hash(row) != row["before_hash"]
            for row in self._operation_rows(transaction_id)
        )
        if create_conflict:
            return "before_hash_mismatch"
        return "unknown_target_bytes"

    def _conflicted_from_mismatch(
        self, transaction_id: str, exc: BaseException
    ) -> TransactionRecord:
        code = self._mismatch_conflict_code(transaction_id, exc)
        if code is None:
            raise exc
        return self._conflicted(transaction_id, code)

    def _mismatch_conflict_code(
        self, transaction_id: str, exc: BaseException
    ) -> str | None:
        """The conflict this mismatch settles as, or None when it is not ours."""
        if not isinstance(exc, TargetStateMismatch):
            return None
        if exc.state_name == "before":
            return self._before_mismatch_code(transaction_id)
        return CONFLICTED_STATE_CODES.get(exc.state_name)

    def _recovered_from_failure(
        self, transaction_id: str, exc: BaseException
    ) -> TransactionRecord:
        """Turn a known apply failure into the state it should settle in."""
        if is_target_boundary_error(exc):
            self._set_transaction_state(
                transaction_id, "quarantined", error_code="parent_identity_changed"
            )
            return self._record(transaction_id)
        if isinstance(exc, TransactionImageError):
            return self._recover_corrupt_after_image(transaction_id)
        return self._conflicted_from_mismatch(transaction_id, exc)

    def _apply_recovered(
        self, transaction_id: str, recovered: list[TransactionRecord]
    ) -> None:
        try:
            recovered.append(self._apply_locked(transaction_id))
        except (DLPContentBlocked, DLPPolicyError) as exc:
            self._quarantine_dlp(transaction_id, exc)
            recovered.append(self._record(transaction_id))
        except TransactionFailure as exc:
            self._settle_transaction_failure(transaction_id, exc)
            recovered.append(self._record(transaction_id))
        except (RuntimeError, ValueError) as exc:
            recovered.append(self._recovered_from_failure(transaction_id, exc))

    def _promoted_for_recovery(
        self, transaction_id: str, recovered: list[TransactionRecord]
    ) -> bool:
        """False when the record settled here and needs no apply."""
        record = self._record(transaction_id)
        if record.state != "preparing":
            return True
        promotion = self._promote_preparing(record)
        if promotion == "invalid":
            self._set_transaction_state(transaction_id, "discarded")
            self._remove_artifacts(self.transaction_root / transaction_id)
            recovered.append(self._record(transaction_id))
            return False
        return self._survived_promotion(transaction_id, promotion, recovered)

    def _survived_promotion(
        self,
        transaction_id: str,
        promotion: str,
        recovered: list[TransactionRecord],
    ) -> bool:
        """False when promotion quarantined the record instead of leaving it appliable."""
        if promotion != "quarantined":
            return True
        recovered.append(self._record(transaction_id))
        return False

    def _recovered_terminal_state(
        self,
        transaction_id: str,
        selected_state: str,
        recovered: list[TransactionRecord],
    ) -> bool:
        """True when an aborting or aborted record was recovered right here."""
        if selected_state == "aborting":
            self._recover_aborting(transaction_id)
            recovered.append(self._record(transaction_id))
            return True
        if selected_state == "aborted":
            self._validate_aborted(transaction_id)
            recovered.append(self._record(transaction_id))
            return True
        return False

    def _preparing_writer_alive(
        self, transaction_id: str, selected_state: str, owner_pid: object
    ) -> bool:
        if selected_state != "preparing":
            return False
        return preparer_alive(self.transaction_root / transaction_id, owner_pid)

    def _recover_one(
        self,
        transaction_id: str,
        selected_state: str,
        owner_pid: object,
        recovered: list[TransactionRecord],
    ) -> None:
        if self._recovered_terminal_state(
            transaction_id, selected_state, recovered
        ):
            return
        if self._preparing_writer_alive(transaction_id, selected_state, owner_pid):
            return
        self._promote_and_apply(transaction_id, recovered)

    def _promote_and_apply(
        self, transaction_id: str, recovered: list[TransactionRecord]
    ) -> None:
        if self._promoted_for_recovery(transaction_id, recovered):
            self._apply_recovered(transaction_id, recovered)

    def _recover_selected(
        self,
        max_transactions: int | None,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> list[TransactionRecord]:
        rows = self._incomplete_transaction_rows(max_transactions)
        self._local.recovery_deadline = deadline
        self._local.recovery_cancelled = cancelled
        recovered: list[TransactionRecord] = []
        try:
            self._recover_rows(rows, deadline, cancelled, recovered)
        finally:
            self._local.recovery_deadline = None
            self._local.recovery_cancelled = None
        return recovered

    def _recover_rows(
        self,
        rows: list,
        deadline: float,
        cancelled: Callable[[], bool] | None,
        recovered: list[TransactionRecord],
    ) -> None:
        for transaction_id, selected_state, owner_pid in rows:
            if self._recovery_stopped(deadline, cancelled):
                return
            self._recover_one(transaction_id, selected_state, owner_pid, recovered)

    def recover(
        self,
        *,
        owner: OwnerLease | None = None,
        writer_wait_seconds: float | None = None,
        max_transactions: int | None = None,
        deadline: float = float("inf"),
        cancelled: Callable[[], bool] | None = None,
    ) -> list[TransactionRecord]:
        """Converge every incomplete transaction without overwriting unknown bytes."""
        _require_recovery_bound(max_transactions)
        if max_transactions == 0 or self._recovery_stopped(deadline, cancelled):
            return []
        with self.writer_gate(owner=owner, wait_seconds=writer_wait_seconds):
            if self._recovery_stopped(deadline, cancelled):
                return []
            # A prune that died mid-way is settled here too, so `doctor --repair`
            # heals what it reports (audit 2026-09-26 B-22).
            self._recover_interrupted_prunes()
            return self._recover_selected(max_transactions, deadline, cancelled)

    def _recover_aborting(self, transaction_id: str) -> None:
        error_code = "abort_receipt_pending"
        for operation in reversed(self._operation_rows(transaction_id)):
            outcome = self._restore_aborted_operation(transaction_id, operation)
            if outcome is not None:
                error_code = outcome
                break
        self._set_transaction_state(transaction_id, "aborting", error_code=error_code)

    def _restore_aborted_operation(
        self, transaction_id: str, operation: sqlite3.Row
    ) -> str | None:
        """None means this operation is settled; a code means recovery stopped."""
        current = self._operation_hash(operation)
        if current == operation["before_hash"]:
            return None
        if current != operation["after_hash"]:
            return "abort_target_conflict"
        try:
            before_state = stored_before_state(
                self.transaction_root, transaction_id, operation
            )
        except (OSError, RuntimeError):
            return "abort_before_image_corrupt"
        inverse = dict(operation)
        inverse.update(
            kind=inverse_change_kind(operation["before_hash"], operation["after_hash"]),
            before_hash=operation["after_hash"],
            after_hash=operation["before_hash"],
        )
        self._apply_operation(inverse, {"after": before_state})
        self._require_operation_state(
            inverse, operation["before_hash"], "restored"
        )
        return None

    def _validate_aborted(self, transaction_id: str) -> None:
        with self._connect() as database:
            row = database.execute(
                'SELECT * FROM "transaction" WHERE id=?', (transaction_id,)
            ).fetchone()
        if row is None or not self._abort_receipt_valid(transaction_id, row):
            self._set_transaction_state(
                transaction_id, "aborted", error_code="abort_receipt_invalid"
            )

    def _abort_receipt_valid(self, transaction_id: str, row: sqlite3.Row) -> bool:
        """An unreadable, unparsable or unmatched receipt is simply not valid."""
        try:
            receipt_bytes = read_runtime_bytes(
                self.transaction_root / transaction_id / "abort-receipt.json",
                self.state_root,
                max_bytes=64 * 1024,
                owner_only=True,
            )
            receipt = json.loads(receipt_bytes)
            validate_schema(
                receipt,
                markdown_store.SCHEMA_DIRECTORY / "transaction-abort-v1.json",
            )
        except (OSError, TypeError, ValueError):
            return False
        # Whether the targets still hold their before-state is not asked: that
        # was true when the abort finished, and any later write makes it false.
        return _abort_receipt_matches(receipt, receipt_bytes, transaction_id, row)

    @staticmethod
    def _recovery_stopped(
        deadline: float, cancelled: Callable[[], bool] | None
    ) -> bool:
        return time.monotonic() >= deadline or bool(cancelled and cancelled())

    def _quarantine_parent_identity(self, record: TransactionRecord) -> str:
        self._set_transaction_state(
            record.id, "quarantined", error_code="parent_identity_changed"
        )
        return "quarantined"

    def _require_target_within_boundary(self, path: str) -> None:
        try:
            self._target(path)
        except (FileNotFoundError, ValueError) as exc:
            raise TargetBoundaryFailure from exc

    def _current_parent_identity(self, path: str) -> tuple[int, int]:
        try:
            return self._parent_identity(self._target(path).parent)
        except (FileNotFoundError, RuntimeError, ValueError) as exc:
            raise TargetBoundaryFailure from exc

    def _validated_plan_document(
        self, record: TransactionRecord, artifact_root: Path
    ) -> tuple[dict | None, bytes]:
        plan_bytes = (artifact_root / "plan.json").read_bytes()
        plan = json.loads(plan_bytes)
        validate_schema(plan, plan_schema(plan))
        if plan_bytes != canonical_json_bytes(plan):
            return None, plan_bytes
        if plan["transaction_id"] != record.id:
            return None, plan_bytes
        return plan, plan_bytes

    def _validated_manifest_document(
        self, record: TransactionRecord, artifact_root: Path, plan_bytes: bytes
    ) -> dict | None:
        manifest_bytes = (artifact_root / "manifest.json").read_bytes()
        manifest = json.loads(manifest_bytes)
        recognised = (
            manifest_bytes == canonical_json_bytes(manifest)
            and set(manifest) == _RECOVERY_MANIFEST_FIELDS
            and _manifest_identity_matches(manifest, record.id, plan_bytes)
        )
        if not recognised:
            return None
        return manifest

    def _manifest_matches_request(
        self, record: TransactionRecord, manifest: Mapping[str, object]
    ) -> bool:
        with self._connect() as database:
            row = database.execute(
                'SELECT request_hash FROM "transaction" WHERE id = ?',
                (record.id,),
            ).fetchone()
        if row is None:
            return False
        return manifest["request_hash"] == row["request_hash"]

    def _built_operation(
        self,
        record: TransactionRecord,
        position: int,
        operation: object,
        persisted: object,
        seen_paths: set[str],
    ) -> _BuiltOperation | None:
        encoded_parent_identity = _validated_persisted_operation(
            persisted, position, operation
        )
        if encoded_parent_identity is None:
            return None
        path = str(operation["path"])
        self._require_target_within_boundary(path)
        normalized = unicodedata.normalize("NFC", path).casefold()
        if normalized in seen_paths:
            return None
        seen_paths.add(normalized)
        return self._built_operation_record(
            record, position, operation, persisted, encoded_parent_identity
        )

    def _built_operation_record(
        self,
        record: TransactionRecord,
        position: int,
        operation: object,
        persisted: object,
        encoded_parent_identity: tuple,
    ) -> _BuiltOperation | None:
        """The row and change one persisted operation promotes to."""
        path = str(operation["path"])
        before_hash = self._state_description_hash(operation["before"])
        after_hash = self._state_description_hash(operation["after"])
        kind = operation["kind"]
        if not _operation_hashes_consistent(persisted, kind, before_hash, after_hash):
            return None
        current_parent = self._current_parent_identity(path)
        parent_identity = (persisted["parent_device"], persisted["parent_inode"])
        return _BuiltOperation(
            (
                record.id,
                position,
                kind,
                path,
                before_hash,
                after_hash,
                *encoded_parent_identity,
                0,
            ),
            {"kind": kind, "path": path, "content_hash": after_hash},
            current_parent != parent_identity,
        )

    def _request_hash_matches(
        self,
        record: TransactionRecord,
        request_changes: list[dict[str, object]],
        manifest: Mapping[str, object],
    ) -> bool:
        request = {
            "changes": request_changes,
            "preconditions": dict(record.preconditions),
        }
        return sha256_bytes(canonical_json_bytes(request)) == manifest["request_hash"]

    def _built_operations(
        self,
        record: TransactionRecord,
        plan: Mapping[str, object],
        manifest: Mapping[str, object],
    ) -> _PromotionPlan | None:
        plan_operations = plan["operations"]
        manifest_operations = manifest["operations"]
        if not _comparable_operation_lists(plan_operations, manifest_operations):
            return None
        built = self._all_built_operations(record, plan_operations, manifest_operations)
        if built is None or not self._request_hash_matches(
            record, [item.change for item in built], manifest
        ):
            return None
        return _promotion_plan(manifest, built)

    def _all_built_operations(
        self,
        record: TransactionRecord,
        plan_operations: list,
        manifest_operations: list,
    ) -> list[_BuiltOperation] | None:
        seen_paths: set[str] = set()
        built: list[_BuiltOperation] = []
        for position, (operation, persisted) in enumerate(
            zip(plan_operations, manifest_operations, strict=True)
        ):
            item = self._built_operation(
                record, position, operation, persisted, seen_paths
            )
            if item is None:
                return None
            built.append(item)
        return built

    def _validated_promotion_plan(
        self, record: TransactionRecord
    ) -> _PromotionPlan | None:
        """Everything the artifacts must agree on before a transaction promotes."""
        artifact_root = self.transaction_root / record.id
        plan, plan_bytes = self._validated_plan_document(record, artifact_root)
        if plan is None:
            return None
        self._verify_plan_artifacts(plan, artifact_root)
        manifest = self._validated_manifest_document(record, artifact_root, plan_bytes)
        if manifest is None or not self._manifest_matches_request(record, manifest):
            return None
        return self._built_operations(record, plan, manifest)

    def _promotion_plan_or_verdict(self, record: TransactionRecord):
        try:
            plan = self._validated_promotion_plan(record)
        except TargetBoundaryFailure:
            return self._quarantine_parent_identity(record)
        except (AssertionError, KeyError, OSError, TypeError, ValueError):
            return "invalid"
        if plan is None:
            return "invalid"
        return plan

    def _bind_existing_reservation(
        self, database: sqlite3.Connection, record: TransactionRecord
    ) -> None:
        reservation_row = database.execute(
            "SELECT * FROM project_checkpoints WHERE operation_id = ?",
            (record.operation_id,),
        ).fetchone()
        if reservation_row is None:
            return
        self._bind_project_reservation(
            database,
            self._project_reservation(reservation_row),
            record.id,
            record.preconditions,
        )

    def _quarantine_failed_promotion(
        self, record: TransactionRecord, exc: TransactionFailure
    ) -> str:
        self._set_transaction_state(record.id, "quarantined", error_code=exc.code)
        with self._connect() as database, begin_immediate(
            database, before_commit=self._require_current_operation_active
        ):
            database.execute(
                "UPDATE project_checkpoints SET state = 'quarantined' "
                "WHERE operation_id = ?",
                (record.operation_id,),
            )
            database.execute(
                "UPDATE project_checkpoint_attempts SET state = 'quarantined' "
                "WHERE operation_id = ?",
                (record.operation_id,),
            )
        return "quarantined"

    def _commit_promotion(
        self, record: TransactionRecord, promotion: _PromotionPlan
    ) -> str | None:
        """The verdict when the promotion could not commit, otherwise None."""
        try:
            return self._write_promotion(record, promotion)
        except ProjectPendingPriorError:
            return "invalid"
        except TransactionFailure as exc:
            return self._quarantine_failed_promotion(record, exc)

    def _write_promotion(
        self, record: TransactionRecord, promotion: _PromotionPlan
    ) -> str | None:
        with self._connect() as database, begin_immediate(
            database, before_commit=self._require_current_operation_active
        ):
            self._bind_existing_reservation(database, record)
            cursor = database.execute(
                'UPDATE "transaction" SET state = \'prepared\', plan_hash = ?, '
                "updated_at = ?, owner_pid = NULL WHERE id = ? AND state = 'preparing'",
                (promotion.manifest["plan_hash"], markdown_store.now(), record.id),
            )
            if cursor.rowcount != 1:
                return "invalid"
            database.executemany(
                'INSERT INTO "operation" '
                "(transaction_id, position, kind, path, before_hash, after_hash, "
                "parent_device, parent_inode, applied) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                promotion.operations,
            )
        return None

    def _promote_preparing(self, record: TransactionRecord) -> str:
        promotion = self._promotion_plan_or_verdict(record)
        if isinstance(promotion, str):
            return promotion
        verdict = self._commit_promotion(record, promotion)
        if verdict is not None:
            return verdict
        return (
            self._quarantine_parent_identity(record)
            if promotion.parent_mismatch
            else "promoted"
        )

    def _state_description_hash(self, state: object) -> str:
        if state == ABSENT:
            return ABSENT
        if not isinstance(state, dict) or frozenset(state) not in _STATE_DESCRIPTION_SHAPES:
            raise ValueError("invalid transaction state description")
        return _required_state_hash(state["sha256"])

    def _rollback_for_quarantine(self, transaction_id: str, error_code: str) -> None:
        """The undo runs to the end: the deadline that stopped the write must
        not also stop the rollback and leave the vault half-written."""
        try:
            with self._uncancellable():
                self._rollback_applied_operations(transaction_id)
        except _TargetBoundaryChanged:
            self._set_transaction_state(
                transaction_id, "quarantined", error_code="parent_identity_changed"
            )
            return
        self._set_transaction_state(
            transaction_id, "quarantined", error_code=error_code
        )

    def _rollback_applied_operations(self, transaction_id: str) -> None:
        for row in self._operation_rows(transaction_id):
            if row["applied"]:
                self._rollback_one_operation(transaction_id, row)

    def _rollback_one_operation(self, transaction_id: str, row: sqlite3.Row) -> None:
        """An operation that cannot be rolled back is left as it stands."""
        current = self._boundary_checked_hash(row)
        if current != row["after_hash"]:
            return
        before_state = optional_before_state(
            self.transaction_root, transaction_id, row
        )
        if before_state is UNAVAILABLE_STATE or not self._try_apply_inverse(
            transaction_id, row, before_state
        ):
            return
        self._mark_operation_unapplied(transaction_id, row["position"])

    def _boundary_checked_hash(self, row: sqlite3.Row) -> str:
        try:
            return self._operation_hash(row)
        except (OSError, RuntimeError, ValueError) as exc:
            if not is_target_boundary_error(exc):
                raise
            raise _TargetBoundaryChanged from exc

    def _try_apply_inverse(
        self, transaction_id: str, row: sqlite3.Row, before_state: object
    ) -> bool:
        """False means this one could not be undone; the parent changing raises."""
        try:
            self._apply_inverse_under_fence(
                _inverse_operation(transaction_id, row), before_state
            )
        except (OSError, RuntimeError, ValueError) as exc:
            if is_target_boundary_error(exc):
                raise _TargetBoundaryChanged from exc
            return False
        return True

    def _mark_operation_unapplied(self, transaction_id: str, position: int) -> None:
        with self._connect() as database, begin_immediate(
            database, before_commit=self._require_current_operation_active
        ):
            database.execute(
                'UPDATE "operation" SET applied = 0 '
                "WHERE transaction_id = ? AND position = ?",
                (transaction_id, position),
            )

    def _apply_inverse_under_fence(
        self, inverse: Mapping[str, object], before_state: object
    ) -> None:
        with self._connect() as database, begin_immediate(
            database, before_commit=self._require_current_operation_active
        ):
            self._assert_writer_ownership(database)
            self._apply_operation(inverse, {"after": before_state})

    def _recover_corrupt_after_image(self, transaction_id: str) -> TransactionRecord:
        """Undo what can be explained; anything else leaves the transaction dirty."""
        rows = self._operation_rows(transaction_id)
        try:
            before_states, current_hashes, ambiguous = self._undo_images(
                transaction_id, rows
            )
            ambiguous = self._undo_applied_operations(
                transaction_id, rows, before_states, current_hashes, ambiguous
            )
        except _TargetBoundaryChanged:
            self._set_transaction_state(
                transaction_id, "quarantined", error_code="parent_identity_changed"
            )
            return self._record(transaction_id)
        self._set_transaction_state(
            transaction_id,
            "quarantined" if ambiguous else "discarded",
            error_code="after_image_corrupt",
        )
        return self._record(transaction_id)
