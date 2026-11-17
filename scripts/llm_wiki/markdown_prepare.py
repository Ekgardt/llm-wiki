"""Transaction preparation: staged artifacts, plan documents and request hashes."""

from __future__ import annotations

import contextlib
import copy
import json
import os
import sqlite3
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Literal, NamedTuple

from bounded_io import IO_CHUNK_BYTES, VerifiedByteRange
from llm_wiki import markdown_store
from llm_wiki.formats.canonical import canonical_json_bytes, sha256_bytes
from llm_wiki.formats.schema import validate_schema
from llm_wiki.markdown_records import (
    MarkdownChange,
    MarkdownOperation,
    OperationBoundElsewhereError,
    PreconditionChangedError,
    ProjectCheckpointReservation,
    ProjectPendingPriorError,
    TransactionFailure,
    TransactionRecord,
)
from llm_wiki.markdown_store import (
    ABSENT,
    PLAN_SCHEMA,
    PREPARER_RECORD,
    Validator,
    content_digest_text,
    encode_parent_identity,
    harden_owner_only,
    image_digest,
)
from llm_wiki.restricted_paths import restricted_relative_path
from llm_wiki.sqlite_transactions import begin_immediate
from reliable_memory import (
    fsync_directory,
    fsync_file,
)


def _preparer_record_bytes() -> bytes | None:
    """This process's number and start; None where the platform names no start."""
    from operational_ownership import (
        OperationalOwnershipError,
        current_process_identity,
    )

    try:
        identity = current_process_identity()
    except OperationalOwnershipError:
        return None
    return canonical_json_bytes(
        {"pid": identity.pid, "start_identity": identity.start_identity}
    )


IMAGE_COMPRESSION_PRESET = 1


class _ArtifactRoots(NamedTuple):
    artifact: Path
    before: Path
    after: Path


class _StagedOperation(NamedTuple):
    operation: MarkdownOperation
    parent_identity: tuple[int, int]
    plan_entry: dict[str, object]


class _StagedPlan(NamedTuple):
    operations: list[MarkdownOperation]
    parent_identities: list[tuple[int, int]]
    plan_operations: list[dict[str, object]]


def _harden_stage_roots(*directories: Path) -> None:
    if os.name == "nt":
        return
    for directory in directories:
        harden_owner_only(directory, 0o700)


def _require_prepare_operation_id(operation_id: object) -> None:
    if not isinstance(operation_id, str) or not operation_id:
        raise ValueError("operation_id must be a non-empty string")


def _require_prepare_arguments(
    operation_id: object, changes: Sequence[MarkdownChange], content_guard: object
) -> None:
    _require_prepare_operation_id(operation_id)
    if not changes:
        raise ValueError("a transaction requires at least one change")
    if content_guard not in {None, "model_output"}:
        raise ValueError("content_guard must be 'model_output' or None")


def _require_reservation_binding(
    project_reservation: ProjectCheckpointReservation,
    operation_id: str,
    persisted_preconditions: Mapping[str, object],
) -> None:
    if project_reservation.operation_id != operation_id:
        raise ValueError("project reservation operation_id does not match")
    if "project_lease" not in persisted_preconditions:
        raise ValueError("project reservation requires a project lease precondition")


def _require_matching_reservation(
    project_reservation: object,
    operation_id: str,
    persisted_preconditions: Mapping[str, object],
) -> None:
    if project_reservation is None:
        return
    if not isinstance(project_reservation, ProjectCheckpointReservation):
        raise TypeError("project_reservation must be a ProjectCheckpointReservation")
    _require_reservation_binding(
        project_reservation, operation_id, persisted_preconditions
    )


def _require_capture_matches_kind(change: MarkdownChange, before: bytes | VerifiedByteRange | None) -> None:
    if change.kind == "create" and before is not None:
        raise FileExistsError(change.path)
    if change.kind in {"replace", "delete"} and before is None:
        raise FileNotFoundError(change.path)


def _require_change_precondition(
    change: MarkdownChange,
    before_hash: str,
    persisted_preconditions: Mapping[str, object],
) -> None:
    expected_before = persisted_preconditions.get(change.path)
    if expected_before is not None and expected_before != before_hash:
        raise PreconditionChangedError(
            f"target precondition changed before prepare: {change.path}"
        )


def _run_plan_validators(plan: dict, validators: Sequence[Validator]) -> None:
    for validator in validators:
        if not callable(validator):
            raise TypeError("validators must be callable")
        if validator(copy.deepcopy(plan)) is False:
            raise ValueError("transaction validator rejected the plan")


def _is_append(before: bytes | VerifiedByteRange | None, after: bytes | None) -> bool:
    """After-bytes that are the before-bytes plus something: what a capture writes."""
    if before is None or after is None:
        return False
    return len(after) > len(before) and _matches_before_prefix(before, after)


def _matches_before_prefix(before, after):
    offset = 0
    view = memoryview(after)
    for chunk in _content_chunks(before):
        if view[offset:offset + len(chunk)] != chunk:
            return False
        offset += len(chunk)
    return True


def _content_chunks(content):
    if isinstance(content, VerifiedByteRange):
        yield from content.iter_chunks(0, len(content))
        return
    view = memoryview(content)
    for offset in range(0, len(view), IO_CHUNK_BYTES):
        yield view[offset:offset + IO_CHUNK_BYTES]


def _stored_image_keys(state: Mapping[str, object]) -> tuple[str, str]:
    """Where a state's stored bytes are and the hash they must have: a suffix or a full image."""
    if "suffix" in state:
        return "suffix", "suffix_sha256"
    return "artifact", "sha256"


def verify_stored_image(state: Mapping[str, object], state_name: str, artifact_root: Path) -> None:
    """The bytes a state keeps on disk hash as recorded; an append's before keeps none."""
    if "length" in state:
        return
    key, digest = _stored_image_keys(state)
    relative = restricted_relative_path(str(state[key]), (state_name,))
    artifact = artifact_root.joinpath(*relative.parts)
    if image_digest(artifact) != state[digest]:
        raise RuntimeError(f"transaction artifact hash mismatch: {relative}")


def _validated_plan(
    transaction_id: str,
    plan_operations: list[dict[str, object]],
    content_guard: object,
    validators: Sequence[Validator],
) -> dict[str, object]:
    plan: dict[str, object] = {
        "schema_version": "markdown-transaction/v2",
        "transaction_id": transaction_id,
        "operations": plan_operations,
    }
    if content_guard is not None:
        plan["content_guard"] = content_guard
    validate_schema(plan, PLAN_SCHEMA)
    _run_plan_validators(plan, validators)
    validate_schema(plan, PLAN_SCHEMA)
    return plan


def _recovery_manifest(
    transaction_id: str, request_hash: str, plan_bytes: bytes, staged: _StagedPlan
) -> dict[str, object]:
    return {
        "schema_version": "markdown-transaction-recovery/v1",
        "transaction_id": transaction_id,
        "request_hash": request_hash,
        "plan_hash": sha256_bytes(plan_bytes),
        "operations": [
            {
                "position": position,
                "before_hash": operation.before_hash,
                "after_hash": operation.after_hash,
                "parent_device": staged.parent_identities[position][0],
                "parent_inode": staged.parent_identities[position][1],
            }
            for position, operation in enumerate(staged.operations)
        ],
    }


def _require_same_lease_fence(
    previous: object, refreshed: Mapping[str, object]
) -> None:
    if not isinstance(previous, dict) or any(
        previous.get(field) != refreshed[field]
        for field in ("project", "lease_token", "fencing_epoch")
    ):
        raise TransactionFailure(
            "project lease changed before transaction refresh",
            "precondition_failed",
            "quarantined",
        )


class TransactionPreparation:
    def _create_artifact_roots(
        self, transaction_id: str, deadline: float, cancelled: Callable[[], bool] | None
    ) -> _ArtifactRoots:
        artifact_root = self.transaction_root / transaction_id
        before_root = artifact_root / "before"
        after_root = artifact_root / "after"
        try:
            self._require_operation_active(deadline, cancelled)
            artifact_root.mkdir(parents=True)
            harden_owner_only(artifact_root, 0o700)
            self._record_preparer(artifact_root)
            before_root.mkdir()
            after_root.mkdir()
            _harden_stage_roots(before_root, after_root)
        except BaseException:
            self._remove_artifacts(artifact_root)
            raise
        return _ArtifactRoots(artifact_root, before_root, after_root)

    def _record_preparer(self, artifact_root: Path) -> None:
        """Name this writer durably before its `preparing` row can exist."""
        record = _preparer_record_bytes()
        if record is None:
            return
        self._write_new_file(artifact_root / PREPARER_RECORD, record)
        fsync_directory(artifact_root)

    def _insert_preparing_row(
        self,
        transaction_id: str,
        operation_id: str,
        request_hash: str,
        persisted_preconditions: Mapping[str, object],
        timestamp: str,
        parent_transaction_id: str | None,
        artifact_root: Path,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> TransactionRecord | None:
        """None once the row is inserted, or the record that already owns the id."""
        try:
            self._require_operation_active(deadline, cancelled)
            with self._connect() as database, begin_immediate(
                database,
                before_commit=lambda: self._require_operation_active(
                    deadline, cancelled
                ),
            ):
                database.execute(
                    'INSERT INTO "transaction" '
                    "(id, operation_id, request_hash, state, preconditions_json, plan_hash, "
                    "created_at, updated_at, parent_transaction_id, owner_pid) "
                    "VALUES (?, ?, ?, 'preparing', ?, '', ?, ?, ?, ?)",
                    (
                        transaction_id,
                        operation_id,
                        request_hash,
                        canonical_json_bytes(persisted_preconditions).decode("utf-8"),
                        timestamp,
                        timestamp,
                        parent_transaction_id,
                        os.getpid(),
                    ),
                )
        except sqlite3.IntegrityError:
            self._remove_artifacts(artifact_root)
            existing = self._record_for_operation_id(operation_id)
            if existing is None:
                raise OperationBoundElsewhereError(
                    "operation_id is already bound to a different request"
                ) from None
            if self._request_hash_for_operation_id(operation_id) != request_hash:
                raise OperationBoundElsewhereError(
                    "operation_id is already bound to a different request"
                ) from None
            return existing
        # Any other failure keeps the directory: whether the row committed is not
        # known here, and a committed row must keep its images. An unnamed one is
        # removed by the prune an hour later (C-8, revised 2026-09-25).
        return None

    def _staged_change(
        self,
        position: int,
        change: MarkdownChange,
        roots: _ArtifactRoots,
        persisted_preconditions: Mapping[str, object],
    ) -> _StagedOperation:
        target = self._target(change.path)
        with self._capture_target(
            target, max_before_bytes=change.max_before_bytes
        ) as (before, parent_identity):
            _require_capture_matches_kind(change, before)
            before_hash = content_digest_text(before)
            _require_change_precondition(change, before_hash, persisted_preconditions)
            before_description, after_description = self._staged_states(
                roots, position, before, change.content
            )
            return _StagedOperation(
                MarkdownOperation(
                    change.kind, change.path, before_hash, content_digest_text(change.content)
                ),
                parent_identity,
                {
                    "kind": change.kind,
                    "path": change.path,
                    "before": before_description,
                    "after": after_description,
                },
            )

    def _staged_operations(
        self,
        normalized: tuple[MarkdownChange, ...],
        roots: _ArtifactRoots,
        persisted_preconditions: Mapping[str, object],
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> _StagedPlan:
        operations: list[MarkdownOperation] = []
        parent_identities: list[tuple[int, int]] = []
        plan_operations: list[dict[str, object]] = []
        for position, change in enumerate(normalized):
            self._require_operation_active(deadline, cancelled)
            staged = self._staged_change(
                position, change, roots, persisted_preconditions
            )
            operations.append(staged.operation)
            parent_identities.append(staged.parent_identity)
            plan_operations.append(staged.plan_entry)
        return _StagedPlan(operations, parent_identities, plan_operations)

    def _write_plan_artifacts(
        self,
        plan: dict[str, object],
        roots: _ArtifactRoots,
        transaction_id: str,
        request_hash: str,
        staged: _StagedPlan,
    ) -> bytes:
        self._verify_plan_artifacts(plan, roots.artifact)
        plan_bytes = canonical_json_bytes(plan)
        self._write_new_file(roots.artifact / "plan.json", plan_bytes)
        self._write_new_file(
            roots.artifact / "manifest.json",
            canonical_json_bytes(
                _recovery_manifest(transaction_id, request_hash, plan_bytes, staged)
            ),
        )
        for directory in (
            roots.before,
            roots.after,
            roots.artifact,
            self.transaction_root,
        ):
            fsync_directory(directory)
        return plan_bytes

    def _operation_rows_for_insert(
        self, transaction_id: str, staged: _StagedPlan
    ) -> list[tuple]:
        return [
            (
                transaction_id,
                position,
                operation.kind,
                operation.path,
                operation.before_hash,
                operation.after_hash,
                *encode_parent_identity(staged.parent_identities[position]),
            )
            for position, operation in enumerate(staged.operations)
        ]

    def _commit_prepared_row(
        self,
        transaction_id: str,
        plan_bytes: bytes,
        timestamp: str,
        staged: _StagedPlan,
        project_reservation: ProjectCheckpointReservation | None,
        persisted_preconditions: Mapping[str, object],
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> None:
        try:
            self._require_operation_active(deadline, cancelled)
            self._persist_prepared_row(
                transaction_id,
                plan_bytes,
                timestamp,
                staged,
                project_reservation,
                persisted_preconditions,
                deadline,
                cancelled,
            )
        except sqlite3.IntegrityError:
            raise RuntimeError(
                "transaction operations could not be persisted"
            ) from None

    def _persist_prepared_row(
        self,
        transaction_id: str,
        plan_bytes: bytes,
        timestamp: str,
        staged: _StagedPlan,
        project_reservation: ProjectCheckpointReservation | None,
        persisted_preconditions: Mapping[str, object],
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> None:
        with self._connect() as database, begin_immediate(
            database,
            before_commit=lambda: self._require_operation_active(
                deadline, cancelled
            ),
        ):
            if project_reservation is not None:
                self._bind_project_reservation(
                    database,
                    project_reservation,
                    transaction_id,
                    persisted_preconditions,
                )
            database.execute(
                'UPDATE "transaction" SET state = \'prepared\', plan_hash = ?, '
                "updated_at = ?, owner_pid = NULL "
                "WHERE id = ? AND state = 'preparing'",
                (sha256_bytes(plan_bytes), timestamp, transaction_id),
            )
            database.executemany(
                'INSERT INTO "operation" '
                "(transaction_id, position, kind, path, before_hash, after_hash, "
                "parent_device, parent_inode) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                self._operation_rows_for_insert(transaction_id, staged),
            )

    def _discard_failed_preparation(
        self,
        transaction_id: str,
        artifact_root: Path,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> None:
        record = self._record_if_present(transaction_id)
        if record is not None and record.state != "preparing":
            return
        with self._connect() as database, begin_immediate(
            database,
            before_commit=lambda: self._require_operation_active(deadline, cancelled),
        ):
            database.execute(
                'DELETE FROM "transaction" WHERE id = ? AND state = \'preparing\'',
                (transaction_id,),
            )
        self._remove_artifacts(artifact_root)

    def _stage_and_commit(
        self,
        normalized: tuple[MarkdownChange, ...],
        roots: _ArtifactRoots,
        persisted_preconditions: Mapping[str, object],
        request_hash: str,
        transaction_id: str,
        timestamp: str,
        *,
        validators: Sequence[Validator],
        content_guard: object,
        project_reservation: ProjectCheckpointReservation | None,
        parent_transaction_id: str | None,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> TransactionRecord:
        staged = self._staged_operations(
            normalized, roots, persisted_preconditions, deadline, cancelled
        )
        self._killpoint("after_images_fsynced", parent_transaction_id)
        plan = _validated_plan(
            transaction_id, staged.plan_operations, content_guard, validators
        )
        plan_bytes = self._write_plan_artifacts(
            plan, roots, transaction_id, request_hash, staged
        )
        self._killpoint("after_plan_fsynced", parent_transaction_id)
        self._commit_prepared_row(
            transaction_id,
            plan_bytes,
            timestamp,
            staged,
            project_reservation,
            persisted_preconditions,
            deadline,
            cancelled,
        )
        self._killpoint("after_prepared", parent_transaction_id)
        return self._record(transaction_id)

    def _prepare_new_transaction(
        self,
        normalized: tuple[MarkdownChange, ...],
        persisted_preconditions: Mapping[str, object],
        request_hash: str,
        *,
        operation_id: str,
        validators: Sequence[Validator],
        content_guard: object,
        project_reservation: ProjectCheckpointReservation | None,
        parent_transaction_id: str | None,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> TransactionRecord:
        transaction_id = uuid.uuid4().hex
        roots = self._create_artifact_roots(transaction_id, deadline, cancelled)
        timestamp = markdown_store.now()
        existing = self._insert_preparing_row(
            transaction_id,
            operation_id,
            request_hash,
            persisted_preconditions,
            timestamp,
            parent_transaction_id,
            roots.artifact,
            deadline,
            cancelled,
        )
        if existing is not None:
            return existing
        self._killpoint("after_preparing", parent_transaction_id)
        try:
            return self._stage_and_commit(
                normalized,
                roots,
                persisted_preconditions,
                request_hash,
                transaction_id,
                timestamp,
                validators=validators,
                content_guard=content_guard,
                project_reservation=project_reservation,
                parent_transaction_id=parent_transaction_id,
                deadline=deadline,
                cancelled=cancelled,
            )
        except BaseException:
            self._discard_failed_preparation(
                transaction_id, roots.artifact, deadline, cancelled
            )
            raise

    def prepare(
        self,
        changes: Sequence[MarkdownChange],
        *,
        operation_id: str,
        preconditions: Mapping[str, object] | None = None,
        validators: Sequence[Validator] = (),
        content_guard: Literal["model_output"] | None = None,
        project_reservation: ProjectCheckpointReservation | None = None,
        _parent_transaction_id: str | None = None,
        deadline: float = float("inf"),
        cancelled: Callable[[], bool] | None = None,
        writer_wait_seconds: float | None = None,
    ) -> TransactionRecord:
        self._require_operation_active(deadline, cancelled)
        _require_prepare_arguments(operation_id, changes, content_guard)
        normalized = self._normalized_changes(changes)
        persisted_preconditions = self._validate_preconditions(preconditions or {})
        _require_matching_reservation(
            project_reservation, operation_id, persisted_preconditions
        )
        request_hash = self._request_hash(normalized, persisted_preconditions)
        self.recover(
            deadline=deadline, cancelled=cancelled,
            writer_wait_seconds=writer_wait_seconds,
        )
        self._require_operation_active(deadline, cancelled)
        existing = self._existing_bound_record(operation_id, request_hash)
        if existing is not None:
            return existing
        return self._prepare_new_transaction(
            normalized,
            persisted_preconditions,
            request_hash,
            operation_id=operation_id,
            validators=validators,
            content_guard=content_guard,
            project_reservation=project_reservation,
            parent_transaction_id=_parent_transaction_id,
            deadline=deadline,
            cancelled=cancelled,
        )

    def _bind_project_reservation(
        self,
        database: sqlite3.Connection,
        reservation: ProjectCheckpointReservation,
        transaction_id: str,
        preconditions: Mapping[str, object],
    ) -> None:
        lease = preconditions["project_lease"]
        assert isinstance(lease, Mapping)
        self._check_project_lease(database, lease)
        self._check_project_head(database, reservation.project, reservation.sequence)
        cursor = database.execute(
            "UPDATE project_checkpoints SET transaction_id = ?, state = 'prepared', "
            "lease_token = ?, fencing_epoch = ? "
            "WHERE project = ? AND sequence = ? AND operation_id = ? "
            "AND state IN ('reserved', 'prepared')",
            (
                transaction_id,
                lease["lease_token"],
                lease["fencing_epoch"],
                reservation.project,
                reservation.sequence,
                reservation.operation_id,
            ),
        )
        if cursor.rowcount != 1:
            raise TransactionFailure(
                "project checkpoint reservation is no longer available",
                "precondition_failed",
                "quarantined",
            )
        attempt = database.execute(
            "UPDATE project_checkpoint_attempts SET transaction_id = ?, "
            "state = 'prepared' WHERE operation_id = ? AND state = 'reserved'",
            (transaction_id, reservation.operation_id),
        )
        if attempt.rowcount != 1:
            raise TransactionFailure(
                "project checkpoint attempt is no longer available",
                "precondition_failed",
                "quarantined",
            )

    def refresh_project_lease_precondition(
        self,
        transaction_id: str,
        lease: Mapping[str, object],
    ) -> TransactionRecord:
        """Refresh a prepared transaction's lease fence without changing its plan."""
        refreshed = self._validate_preconditions({"project_lease": lease})[
            "project_lease"
        ]
        assert isinstance(refreshed, Mapping)
        with self._connect() as database, begin_immediate(
            database, before_commit=self._require_current_operation_active
        ):
            preconditions = self._prepared_preconditions(
                database, transaction_id, refreshed
            )
            self._check_project_lease(database, refreshed)
            preconditions["project_lease"] = dict(refreshed)
            self._store_refreshed_preconditions(
                database, transaction_id, preconditions
            )
        return self._record(transaction_id)

    def _prepared_preconditions(
        self,
        database: sqlite3.Connection,
        transaction_id: str,
        refreshed: Mapping[str, object],
    ) -> dict[str, object]:
        row = database.execute(
            'SELECT state, preconditions_json FROM "transaction" WHERE id = ?',
            (transaction_id,),
        ).fetchone()
        if row is None or row["state"] != "prepared":
            raise TransactionFailure(
                "project lease refresh requires a prepared transaction",
                "precondition_failed",
                "quarantined",
            )
        preconditions = json.loads(row["preconditions_json"])
        _require_same_lease_fence(preconditions.get("project_lease"), refreshed)
        return preconditions

    def _store_refreshed_preconditions(
        self,
        database: sqlite3.Connection,
        transaction_id: str,
        preconditions: Mapping[str, object],
    ) -> None:
        updated = database.execute(
            'UPDATE "transaction" SET preconditions_json = ?, updated_at = ? '
            "WHERE id = ? AND state = 'prepared'",
            (
                canonical_json_bytes(preconditions).decode("utf-8"),
                markdown_store.now(),
                transaction_id,
            ),
        )
        if updated.rowcount != 1:
            raise TransactionFailure(
                "project transaction changed during lease refresh",
                "precondition_failed",
                "quarantined",
            )

    @staticmethod
    def _check_project_head(
        database: sqlite3.Connection, project: str, sequence: int
    ) -> None:
        prior = database.execute(
            "SELECT sequence FROM project_checkpoints WHERE project = ? "
            "AND sequence < ? AND state != 'committed' ORDER BY sequence LIMIT 1",
            (project, sequence),
        ).fetchone()
        if prior is not None:
            raise ProjectPendingPriorError(project, sequence, int(prior["sequence"]))

    def _check_project_transaction_head(
        self, database: sqlite3.Connection, transaction_id: str
    ) -> None:
        reservation = database.execute(
            "SELECT project, sequence FROM project_checkpoints WHERE transaction_id = ?",
            (transaction_id,),
        ).fetchone()
        if reservation is not None:
            self._check_project_head(
                database, str(reservation["project"]), int(reservation["sequence"])
            )

    def _request_hash(
        self, changes: Sequence[MarkdownChange], preconditions: Mapping[str, object]
    ) -> str:
        request = {
            "changes": [
                {
                    "kind": change.kind,
                    "path": change.path,
                    "content_hash": ABSENT
                    if change.content is None
                    else sha256_bytes(change.content),
                }
                for change in changes
            ],
            "preconditions": dict(preconditions),
        }
        return sha256_bytes(canonical_json_bytes(request))

    def _staged_states(
        self, roots: _ArtifactRoots, position: int, before: bytes | VerifiedByteRange | None, after: bytes | None
    ) -> tuple[object, object]:
        """Both images, or for an append only the added bytes and the before length.

        An append stored the whole file twice; on the live vault 7 026 appends of one
        day held 2.58 GB of images for 9.67 MB they added (2026-09-27).
        """
        if not _is_append(before, after):
            return self._stage_state(roots.before, position, before), self._stage_state(
                roots.after, position, after
            )
        return self._stage_append(roots.after, position, before, after)

    def _stage_append(
        self, root: Path, position: int, before: bytes | VerifiedByteRange, after: bytes
    ) -> tuple[object, object]:
        suffix = memoryview(after)[len(before) :]
        name = f"{position:06d}.bin"
        self._write_compressed_image(root / name, suffix)
        before_state = {"sha256": content_digest_text(before), "length": len(before)}
        after_state = {
            "sha256": sha256_bytes(after),
            "base_length": len(before),
            "suffix": f"{root.name}/{name}",
            "suffix_sha256": sha256_bytes(suffix),
        }
        return before_state, after_state

    def _stage_state(self, root: Path, position: int, content: bytes | VerifiedByteRange | None) -> object:
        if content is None:
            return ABSENT
        name = f"{position:06d}.bin"
        self._write_compressed_image(root / name, content)
        return {"sha256": content_digest_text(content), "artifact": f"{root.name}/{name}"}

    def _verify_plan_artifacts(self, plan: Mapping[str, object], artifact_root: Path) -> None:
        operations = plan["operations"]
        assert isinstance(operations, list)
        for operation in operations:
            assert isinstance(operation, dict)
            self._verify_operation_artifacts(operation, artifact_root)

    def _verify_operation_artifacts(
        self, operation: Mapping[str, object], artifact_root: Path
    ) -> None:
        for state_name in ("before", "after"):
            self._verify_artifact_state(
                operation[state_name], state_name, artifact_root
            )

    @staticmethod
    def _verify_artifact_state(
        state: object, state_name: str, artifact_root: Path
    ) -> None:
        if state == ABSENT:
            return
        assert isinstance(state, dict)
        verify_stored_image(state, state_name, artifact_root)

    def _write_new_file(self, path: Path, content: bytes, *, owner_only: bool = True) -> None:
        with self._new_file_stream(path, owner_only=owner_only) as handle:
            handle.write(content)

    def _write_compressed_image(self, path, content):
        import lzma

        with self._new_file_stream(path) as handle, lzma.LZMAFile(
            handle, "wb", preset=IMAGE_COMPRESSION_PRESET
        ) as encoded:
            for chunk in _content_chunks(content):
                encoded.write(chunk)

    @contextlib.contextmanager
    def _new_file_stream(self, path, *, owner_only=True):
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            yield handle
            handle.flush()
        if owner_only:
            if os.name != "nt":
                harden_owner_only(path, 0o600)
        fsync_file(path)
