"""Committed write authority reads and transaction record lookup for the Markdown coordinator."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

from llm_wiki import markdown_store
from llm_wiki.formats.canonical import sha256_bytes
from llm_wiki.formats.schema import validate_schema
from llm_wiki.markdown_records import (
    CommittedWriteAuthority,
    MarkdownOperation,
    OperationBoundElsewhereError,
    TransactionImageError,
    TransactionRecord,
)
from llm_wiki.markdown_store import plan_schema
from llm_wiki.reliability_defaults import DEFAULTS
from llm_wiki.sqlite_deadlines import (
    configure_operational_deadline,
)


class TransactionRecordOperations:
    @staticmethod
    def _authority_read_active(deadline: float | None) -> None:
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("transaction authority read deadline reached")

    @staticmethod
    def _authority_read_busy_ms(deadline: float | None) -> int:
        if deadline is None or deadline == float("inf"):
            return DEFAULTS.markdown_busy_ms
        return min(DEFAULTS.markdown_busy_ms, max(0, int((deadline - time.monotonic()) * 1000)))

    @contextlib.contextmanager
    def _authority_read_connection(self, deadline: float | None):
        self._authority_read_active(deadline)
        try:
            with self._connect(busy_ms=self._authority_read_busy_ms(deadline), deadline=deadline) as database:
                self._configure_authority_read(database, deadline)
                yield database
                self._authority_read_active(deadline)
        except (sqlite3.OperationalError, TimeoutError):
            self._authority_read_active(deadline)
            raise

    def _configure_authority_read(self, database, deadline):
        self._authority_read_active(deadline)
        configure_operational_deadline(database, deadline)

    def committed_attempt(self, operation_id: str, *, deadline: float | None = None):
        """The attempt of this operation that committed, ordinals included.

        Evidence written by a compile names the operation identity derived from
        its inputs, not the row that wrote it: the receipt readers recompute
        that identity from the receipt's own fields and refuse anything else.
        When an attempt is refused, the next one carries the same identity with
        the next ordinal, so the committed writer is a sibling of the named row.
        Callers still have to prove the bytes: this only says which attempt to
        ask.
        """
        record = self._record_for_operation_id(operation_id, deadline=deadline)
        if record is not None and record.state == "committed":
            return record
        return self._committed_attempt_by_ordinal(operation_id, deadline=deadline)

    def _committed_attempt_by_ordinal(self, operation_id: str, *, deadline: float | None = None):
        with self._authority_read_connection(deadline) as database:
            row = self._committed_ordinal_row(database, operation_id)
        if row is None:
            return None
        return self._record(row["id"], deadline=deadline)

    @staticmethod
    def _committed_ordinal_row(database, operation_id):
        return database.execute(
            'SELECT id FROM "transaction" WHERE operation_id >= ? AND operation_id < ? '
            "AND state = 'committed' ORDER BY created_at DESC, rowid DESC "
            "LIMIT 1",
            (operation_id + "#", operation_id + "$"),
        ).fetchone()

    def committed_write_authority(
        self, operation_id: str, *, deadline: float | None = None
    ) -> CommittedWriteAuthority | None:
        """Read all write evidence without fetching unused recovery preconditions.

        The normal transaction reader remains complete. This reader retains
        the same exact identity, ordinal selection, connection contract and
        deadline; each caller must still verify its own receipt bytes.
        """
        with self._authority_read_connection(deadline) as database:
            row = self._committed_write_row(database, operation_id)
            if row is None:
                return None
            return self._write_authority(database, row)

    def _committed_write_row(self, database, operation_id):
        row = database.execute(
            'SELECT id,operation_id,state FROM "transaction" WHERE operation_id=?',
            (operation_id,),
        ).fetchone()
        if row is not None and row["state"] == "committed":
            return row
        ordinal = self._committed_ordinal_row(database, operation_id)
        if ordinal is None:
            return None
        return database.execute(
            'SELECT id,operation_id,state FROM "transaction" WHERE id=? AND state=\'committed\'',
            (ordinal["id"],),
        ).fetchone()

    @staticmethod
    def _write_authority(database, row):
        operations = database.execute(
            'SELECT kind,path,before_hash,after_hash FROM "operation" '
            "WHERE transaction_id=? ORDER BY position",
            (row["id"],),
        )
        return CommittedWriteAuthority(
            row["id"], row["operation_id"], row["state"],
            tuple(MarkdownOperation(*operation) for operation in operations),
        )

    def attempt_operation_id(
        self, operation_id: str, *, deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> tuple[str, str | None]:
        """The id this attempt must use, and the quarantined one it follows.

        A key is never reused for a different payload — that refusal stays. What
        changes is that it stops being a dead end: a quarantined attempt wrote
        nothing durable, so the next one is a new operation and takes the next
        ordinal, naming the refused transaction as its parent. Project
        checkpoints in this file have always retried this way.

        An unchanged payload still resolves to the same id, so crash resume and
        ordinary idempotency are untouched.
        Search uses the caller's deadline, or the existing writer-wait budget
        when none is supplied; retained refusals never exhaust a numeric quota.
        """
        selected_deadline = time.monotonic() + markdown_store.WRITER_WAIT_SECONDS if deadline is None else deadline
        candidate = operation_id
        parent: str | None = None
        ordinal = 2
        while True:
            self._require_operation_active(selected_deadline, cancelled)
            record = self._record_for_operation_id(candidate)
            self._require_operation_active(selected_deadline, cancelled)
            if record is None or record.state != "quarantined":
                return candidate, parent
            parent = record.id
            candidate = f"{operation_id}#{ordinal}"
            ordinal += 1

    def _existing_bound_record(
        self, operation_id: str, request_hash: str
    ) -> TransactionRecord | None:
        existing = self._record_for_operation_id(operation_id)
        if existing is None:
            return None
        if self._request_hash_for_operation_id(operation_id) != request_hash:
            raise OperationBoundElsewhereError("operation_id is already bound to a different request")
        return existing

    def _load_verified_plan(self, record: TransactionRecord) -> dict[str, object]:
        plan_path = self.transaction_root / record.id / "plan.json"
        try:
            plan_bytes = plan_path.read_bytes()
            with self._connect() as database:
                expected = database.execute(
                    'SELECT plan_hash FROM "transaction" WHERE id = ?', (record.id,)
                ).fetchone()["plan_hash"]
            if sha256_bytes(plan_bytes) != expected:
                raise TransactionImageError("transaction plan hash mismatch")
            plan = json.loads(plan_bytes)
            validate_schema(plan, plan_schema(plan))
            self._verify_plan_artifacts(plan, self.transaction_root / record.id)
        except (AssertionError, KeyError, OSError, RuntimeError, ValueError) as exc:
            raise TransactionImageError("transaction after-image is corrupt") from exc
        return plan

    def _operation_rows(self, transaction_id: str) -> list[sqlite3.Row]:
        with self._connect() as database:
            return list(
                database.execute(
                    'SELECT * FROM "operation" WHERE transaction_id = ? ORDER BY position',
                    (transaction_id,),
                )
            )

    def transaction_state(self, transaction_id: str) -> str | None:
        """The state of one transaction, or None when it is not recorded."""
        with self._connect() as database:
            row = database.execute(
                'SELECT state FROM "transaction" WHERE id = ?', (str(transaction_id),)
            ).fetchone()
        return None if row is None else str(row["state"])

    def _record(self, transaction_id: str, *, deadline: float | None = None) -> TransactionRecord:
        record = self._record_if_present(transaction_id, deadline=deadline)
        if record is None:
            raise KeyError(f"unknown transaction: {transaction_id}")
        return record

    def _record_if_present(self, transaction_id: str, *, deadline: float | None = None) -> TransactionRecord | None:
        with self._authority_read_connection(deadline) as database:
            row = database.execute(
                'SELECT * FROM "transaction" WHERE id = ?', (transaction_id,)
            ).fetchone()
            if row is None:
                return None
            operation_rows = list(
                database.execute(
                    'SELECT * FROM "operation" WHERE transaction_id = ? ORDER BY position',
                    (transaction_id,),
                )
            )
        return TransactionRecord(
            id=row["id"],
            operation_id=row["operation_id"],
            state=row["state"],
            operations=tuple(
                MarkdownOperation(
                    operation["kind"],
                    operation["path"],
                    operation["before_hash"],
                    operation["after_hash"],
                )
                for operation in operation_rows
            ),
            preconditions=json.loads(row["preconditions_json"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            parent_transaction_id=row["parent_transaction_id"],
            error_code=row["error_code"],
        )

    def _artifacts_pruned(self, transaction_id: str) -> bool:
        """Whether `prune` already removed this transaction's plan and images."""
        with self._connect() as database:
            row = database.execute(
                'SELECT artifacts_pruned_at FROM "transaction" WHERE id = ?',
                (transaction_id,),
            ).fetchone()
        return row is not None and row["artifacts_pruned_at"] is not None

    def operation_id_at(self, sequence: int, *, deadline: float | None = None) -> str | None:
        """The operation a committed transaction ran, by its commit sequence (rowid)."""
        with self._authority_read_connection(deadline) as database:
            row = database.execute(
                'SELECT operation_id FROM "transaction" WHERE rowid=? AND state=\'committed\'',
                (sequence,),
            ).fetchone()
        return None if row is None else str(row["operation_id"])

    def _record_for_operation_id(self, operation_id: str, *, deadline: float | None = None) -> TransactionRecord | None:
        with self._authority_read_connection(deadline) as database:
            row = database.execute(
                'SELECT id, state FROM "transaction" WHERE operation_id = ?',
                (operation_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            return self._record(row["id"], deadline=deadline)
        except KeyError:
            if row["state"] == "preparing":
                return None
            raise

    def _request_hash_for_operation_id(self, operation_id: str) -> str | None:
        with self._connect() as database:
            row = database.execute(
                'SELECT request_hash FROM "transaction" WHERE operation_id = ?', (operation_id,)
            ).fetchone()
        return None if row is None else row["request_hash"]

    def _remove_artifacts(self, root: Path) -> None:
        if not root.exists():
            return
        for path in sorted(root.rglob("*"), reverse=True):
            if path.is_dir():
                path.rmdir()
            else:
                path.unlink()
        root.rmdir()
