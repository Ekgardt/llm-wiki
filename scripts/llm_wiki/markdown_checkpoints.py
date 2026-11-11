"""Project checkpoint reservation, retry and normalization for the Markdown coordinator."""

from __future__ import annotations

import copy
import json
import sqlite3
import unicodedata
from collections.abc import Mapping, Sequence

from llm_wiki import markdown_store
from llm_wiki.formats.canonical import canonical_json_bytes, sha256_bytes
from llm_wiki.formats.schema import validate_schema
from llm_wiki.markdown_records import MarkdownChange, ProjectCheckpointReservation
from llm_wiki.markdown_store import content_digest_text, encode_parent_identity
from llm_wiki.sqlite_transactions import begin_immediate

_PROJECT_CHECKPOINT_SCHEMA = markdown_store.SCHEMA_DIRECTORY / "project-checkpoint-v1.json"


# Transaction states a reserved checkpoint can never recover from: the write it
# stood for did not happen and no longer can. `preparing`, `prepared`,
# `applying` and `aborting` are absent on purpose — those may still commit, and
# retrying one would put a second writer on the same sequence.
_SPENT_TRANSACTION_STATES = frozenset(
    {"discarded", "aborted", "conflicted", "quarantined"}
)


def _require_checkpoint_key(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"checkpoint {label} must be a non-empty string")
    return value


def _checkpoint_event_json(
    base: Mapping[str, object], project: str, sequence: int, row: sqlite3.Row
) -> str:
    full_event = dict(base)
    full_event.update(
        project=project,
        sequence=sequence,
        last_applied_sequence=int(row["last_applied_sequence"]),
    )
    return canonical_json_bytes(full_event).decode("utf-8")


def _insert_checkpoint(
    database: sqlite3.Connection,
    project: str,
    sequence: int,
    occurrence_id: str,
    idempotency_key: str,
    event_json: str,
    precondition: Mapping[str, object],
    operation_id: str,
) -> None:
    database.execute(
        "INSERT INTO project_checkpoints "
        "(project, sequence, occurrence_id, idempotency_key, event_json, "
        "lease_token, fencing_epoch, operation_id, attempt_number, state) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 'reserved')",
        (
            project,
            sequence,
            occurrence_id,
            idempotency_key,
            event_json,
            precondition["lease_token"],
            precondition["fencing_epoch"],
            operation_id,
        ),
    )
    database.execute(
        "INSERT INTO project_checkpoint_attempts "
        "(project, sequence, attempt_number, operation_id, lease_token, "
        "fencing_epoch, state, created_at) "
        "VALUES (?, ?, 1, ?, ?, ?, 'reserved', ?)",
        (
            project,
            sequence,
            operation_id,
            precondition["lease_token"],
            precondition["fencing_epoch"],
            markdown_store.now(),
        ),
    )


class ProjectCheckpointOperations:
    def reserve_project_checkpoint(
        self,
        project: str,
        event: Mapping[str, object],
        lease: Mapping[str, object],
    ) -> ProjectCheckpointReservation:
        """Atomically fence and reserve one idempotent project sequence."""
        base = self.normalize_project_checkpoint(project, event)
        occurrence_id = _require_checkpoint_key(
            base.get("occurrence_id"), "occurrence_id"
        )
        idempotency_key = _require_checkpoint_key(
            base.get("idempotency_key"), "idempotency_key"
        )
        precondition = self._checkpoint_lease(project, lease)
        with self._connect() as database, begin_immediate(database):
            self._check_project_lease(database, precondition)
            existing = self._existing_checkpoint(
                database, project, base, occurrence_id, idempotency_key
            )
            if existing is not None:
                return self._reserved_or_retried(
                    database, project, existing, precondition
                )
            return self._reserve_new_checkpoint(
                database, project, base, occurrence_id, idempotency_key, precondition
            )

    def _checkpoint_lease(
        self, project: str, lease: Mapping[str, object]
    ) -> Mapping[str, object]:
        precondition = self._validate_preconditions({"project_lease": lease})[
            "project_lease"
        ]
        assert isinstance(precondition, Mapping)
        if precondition["project"] != project:
            raise ValueError("project lease does not match checkpoint project")
        return precondition

    def _existing_checkpoint(
        self,
        database: sqlite3.Connection,
        project: str,
        base: Mapping[str, object],
        occurrence_id: str,
        idempotency_key: str,
    ) -> sqlite3.Row | None:
        """The row this reservation has already been made under, if any."""
        occurrence = database.execute(
            "SELECT * FROM project_checkpoints "
            "WHERE project = ? AND occurrence_id = ?",
            (project, occurrence_id),
        ).fetchone()
        deduplicated = database.execute(
            "SELECT * FROM project_checkpoints "
            "WHERE project = ? AND idempotency_key = ?",
            (project, idempotency_key),
        ).fetchone()
        self._require_consistent_checkpoint(
            occurrence, deduplicated, base, idempotency_key
        )
        return occurrence or deduplicated

    def _require_same_occurrence(
        self,
        occurrence: sqlite3.Row,
        base: Mapping[str, object],
        idempotency_key: str,
        allocated: set[str],
    ) -> None:
        if occurrence["idempotency_key"] != idempotency_key:
            raise ValueError(
                "occurrence_id is already bound to another idempotency key"
            )
        self._require_same_checkpoint_event(occurrence, base, allocated)

    def _require_consistent_checkpoint(
        self,
        occurrence: sqlite3.Row | None,
        deduplicated: sqlite3.Row | None,
        base: Mapping[str, object],
        idempotency_key: str,
    ) -> None:
        allocated = {"project", "sequence", "last_applied_sequence"}
        if occurrence is not None:
            self._require_same_occurrence(occurrence, base, idempotency_key, allocated)
        if deduplicated is not None:
            self._require_same_checkpoint_event(
                deduplicated, base, allocated | {"occurrence_id"}
            )

    def _reserved_or_retried(
        self,
        database: sqlite3.Connection,
        project: str,
        existing: sqlite3.Row,
        precondition: Mapping[str, object],
    ) -> ProjectCheckpointReservation:
        if not self._checkpoint_is_spent(database, existing):
            return self._project_reservation(existing, duplicate=True)
        return self._retry_spent_checkpoint(
            database, project, existing, precondition
        )

    def _checkpoint_is_spent(
        self, database: sqlite3.Connection, existing: sqlite3.Row
    ) -> bool:
        """True when this sequence's write did not happen and never will.

        A quarantined row has always been retried here. A `reserved` row whose
        transaction reached a terminal state other than `committed` is the same
        situation wearing a different label, and it used to be answered with
        `duplicate=True` — a claim that the checkpoint was already written when
        nothing had been. The caller then re-derived the same operation id, the
        transaction table found the recorded plan no longer matched the current
        one, and refused: `operation_id is already bound to a different
        request`, permanently.

        Measured on this vault 2026-08-30: one project's sequence 320 sat
        `reserved` with a NULL transaction while a `discarded` transaction held
        its operation id. That single row had been refusing every checkpoint for
        that project since 08-28 — 366 log lines — and it also stopped the
        backlog drain for every other project behind it.

        In-flight states are deliberately absent: a transaction still preparing
        or applying may yet commit, and retrying it would be a second writer.
        See `docs/research/2026-08-30-a-batch-named-after-one-of-its-members.md`.
        """
        if existing["state"] == "quarantined":
            return True
        if existing["state"] != "reserved":
            return False
        state = self._transaction_state_for_operation(
            database, str(existing["operation_id"])
        )
        return state in _SPENT_TRANSACTION_STATES

    @staticmethod
    def _transaction_state_for_operation(
        database: sqlite3.Connection, operation_id: str
    ) -> str | None:
        row = database.execute(
            'SELECT state FROM "transaction" WHERE operation_id = ?',
            (operation_id,),
        ).fetchone()
        return None if row is None else str(row["state"])

    def retry_unsettled_sequence(
        self,
        project: str,
        sequence: int,
        lease: Mapping[str, object],
        *,
        include_committed: bool = False,
    ) -> ProjectCheckpointReservation | None:
        """Give one unsettled sequence a fresh attempt, or None if it moved on.

        A quarantined or reserved sequence is normally cleared by the original
        request arriving again: the same `occurrence_id` reaches
        `_retry_spent_checkpoint`, the row becomes a new attempt, and both it
        and everything queued behind it append in order.
        `tests/test_project_journal.py` states that contract in seven tests and
        it is not changed here.

        This exposes the same step for the one case where that door is walled
        up: a row whose name no request can produce any more. It has three
        callers: the one-time repair after the 2026-08-30 rename of batch
        identities (`repair_orphaned_checkpoint_names`), the journal-gap repair
        (`repair_journal_gap`), and the project journal's own replay when the
        files moved under a reservation (`_replayed_after_rebinding`). The
        caller must already hold the project lease.

        `include_committed` is for the one repair that needs it: a sequence
        this database calls committed while the journal does not hold it. The
        journal is the authority, so such a row is a false record and its write
        is still owed. Nothing else may pass it — re-attempting a sequence the
        journal already carries would append it twice.

        See `docs/research/2026-08-30-a-sequence-nothing-can-settle.md`.
        """
        precondition = self._checkpoint_lease(project, lease)
        states = ("committed",) if include_committed else ()
        with self._connect() as database, begin_immediate(database):
            self._check_project_lease(database, precondition)
            existing = self._sequence_row(database, project, sequence, states)
            if existing is None:
                return None
            return self._retry_spent_checkpoint(
                database, project, existing, precondition
            )

    @staticmethod
    def _sequence_row(
        database: sqlite3.Connection,
        project: str,
        sequence: int,
        also: Sequence[str],
    ) -> sqlite3.Row | None:
        if also:
            return database.execute(
                "SELECT * FROM project_checkpoints WHERE project = ? AND sequence = ?",
                (project, sequence),
            ).fetchone()
        return database.execute(
            "SELECT * FROM project_checkpoints WHERE project = ? "
            "AND sequence = ? AND state != 'committed'",
            (project, sequence),
        ).fetchone()

    def _retry_spent_checkpoint(
        self,
        database: sqlite3.Connection,
        project: str,
        existing: sqlite3.Row,
        precondition: Mapping[str, object],
    ) -> ProjectCheckpointReservation:
        """A spent sequence is retried as a new attempt, not a new row."""
        attempt_number = int(existing["attempt_number"]) + 1
        parent_operation_id = str(existing["operation_id"])
        operation_id = self._project_attempt_operation_id(
            project,
            int(existing["sequence"]),
            attempt_number,
            int(precondition["fencing_epoch"]),
            str(existing["event_json"]),
        )
        database.execute(
            "UPDATE project_checkpoints SET lease_token = ?, "
            "fencing_epoch = ?, operation_id = ?, attempt_number = ?, "
            "parent_operation_id = ?, transaction_id = NULL, state = 'reserved' "
            "WHERE project = ? AND sequence = ? AND state = ?",
            (
                precondition["lease_token"],
                precondition["fencing_epoch"],
                operation_id,
                attempt_number,
                parent_operation_id,
                project,
                existing["sequence"],
                existing["state"],
            ),
        )
        database.execute(
            "INSERT INTO project_checkpoint_attempts "
            "(project, sequence, attempt_number, operation_id, "
            "parent_operation_id, lease_token, fencing_epoch, state, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'reserved', ?)",
            (
                project,
                existing["sequence"],
                attempt_number,
                operation_id,
                parent_operation_id,
                precondition["lease_token"],
                precondition["fencing_epoch"],
                markdown_store.now(),
            ),
        )
        return self._project_reservation(
            self._checkpoint_row(database, project, int(existing["sequence"]))
        )

    @staticmethod
    def _checkpoint_row(
        database: sqlite3.Connection, project: str, sequence: int
    ) -> sqlite3.Row | None:
        return database.execute(
            "SELECT * FROM project_checkpoints WHERE project = ? AND sequence = ?",
            (project, sequence),
        ).fetchone()

    def _reserve_new_checkpoint(
        self,
        database: sqlite3.Connection,
        project: str,
        base: Mapping[str, object],
        occurrence_id: str,
        idempotency_key: str,
        precondition: Mapping[str, object],
    ) -> ProjectCheckpointReservation:
        row = database.execute(
            "SELECT COALESCE(MAX(sequence), 0) AS last_sequence, "
            "COALESCE(MAX(CASE WHEN state = 'committed' THEN sequence END), 0) "
            "AS last_applied_sequence FROM project_checkpoints WHERE project = ?",
            (project,),
        ).fetchone()
        sequence = int(row["last_sequence"]) + 1
        event_json = _checkpoint_event_json(base, project, sequence, row)
        operation_id = self._project_attempt_operation_id(
            project, sequence, 1, int(precondition["fencing_epoch"]), event_json
        )
        _insert_checkpoint(
            database,
            project,
            sequence,
            occurrence_id,
            idempotency_key,
            event_json,
            precondition,
            operation_id,
        )
        return self._project_reservation(
            self._checkpoint_row(database, project, sequence)
        )

    @staticmethod
    def _project_attempt_operation_id(
        project: str,
        sequence: int,
        attempt_number: int,
        fencing_epoch: int,
        event_json: str,
    ) -> str:
        return (
            f"project:{project}:{sequence}:attempt:{attempt_number}:"
            f"epoch:{fencing_epoch}:{sha256_bytes(event_json.encode('utf-8'))}"
        )

    @staticmethod
    def _require_checkpoint_inputs(project: object, event: object) -> None:
        """The two shape refusals that must precede any canonicalization."""
        if not isinstance(project, str) or not project:
            raise ValueError("project must be a non-empty string")
        if not isinstance(event, Mapping):
            raise TypeError("checkpoint event must be a mapping")

    @staticmethod
    def normalize_project_checkpoint(
        project: str, event: Mapping[str, object]
    ) -> dict[str, object]:
        """Canonicalize and fully validate an event before reserving any state."""
        ProjectCheckpointOperations._require_checkpoint_inputs(project, event)
        allocated = {"project", "sequence", "last_applied_sequence"}
        if allocated.intersection(event):
            raise ValueError("checkpoint event contains coordinator-allocated fields")
        normalized = json.loads(canonical_json_bytes(dict(event)))
        candidate = copy.deepcopy(normalized)
        candidate.update(project=project, sequence=1, last_applied_sequence=0)
        validate_schema(candidate, _PROJECT_CHECKPOINT_SCHEMA)
        return normalized

    @staticmethod
    def _require_same_checkpoint_event(
        row: sqlite3.Row,
        requested: Mapping[str, object],
        ignored: set[str],
    ) -> None:
        stored = json.loads(row["event_json"])
        candidate = dict(requested)
        for field in ignored:
            stored.pop(field, None)
            candidate.pop(field, None)
        if canonical_json_bytes(stored) != canonical_json_bytes(candidate):
            label = "idempotency_key" if "occurrence_id" in ignored else "occurrence_id"
            raise ValueError(f"{label} is already bound to another event")

    @staticmethod
    def _project_reservation(
        row: Mapping[str, object], *, duplicate: bool = False
    ) -> ProjectCheckpointReservation:
        return ProjectCheckpointReservation(
            project=str(row["project"]),
            sequence=int(row["sequence"]),
            occurrence_id=str(row["occurrence_id"]),
            idempotency_key=str(row["idempotency_key"]),
            event_json=str(row["event_json"]),
            operation_id=str(row["operation_id"]),
            attempt_number=int(row["attempt_number"]),
            state=str(row["state"]),
            transaction_id=str(row["transaction_id"])
            if row["transaction_id"]
            else None,
            parent_operation_id=str(row["parent_operation_id"])
            if row["parent_operation_id"]
            else None,
            duplicate=duplicate,
        )

    def _backfill_parent_identities(self, database: sqlite3.Connection) -> None:
        rows = database.execute(
            'SELECT operation.*, "transaction".state AS transaction_state '
            'FROM "operation" JOIN "transaction" '
            'ON "transaction".id = operation.transaction_id '
            "WHERE (operation.parent_device IS NULL OR operation.parent_inode IS NULL) "
            "AND \"transaction\".state IN ('prepared', 'applying')"
        ).fetchall()
        for row in rows:
            target = self._target(row["path"])
            with self._capture_target(target) as (content, identity):
                current_hash = content_digest_text(content)
            expected_hashes = {row["after_hash"]} if row["applied"] else {
                row["before_hash"],
                row["after_hash"],
            }
            if current_hash not in expected_hashes:
                raise RuntimeError(
                    f"cannot migrate parent identity with unknown target bytes: {row['path']}"
                )
            database.execute(
                'UPDATE "operation" SET parent_device = ?, parent_inode = ? '
                "WHERE transaction_id = ? AND position = ?",
                (*encode_parent_identity(identity), row["transaction_id"], row["position"]),
            )

    def _normalized_changes(
        self, changes: Sequence[MarkdownChange]
    ) -> tuple[MarkdownChange, ...]:
        normalized = tuple(self._validate_change(change) for change in changes)
        paths = [
            unicodedata.normalize("NFC", change.path).casefold()
            for change in normalized
        ]
        if len(paths) != len(set(paths)):
            raise ValueError("duplicate transaction target")
        return normalized
