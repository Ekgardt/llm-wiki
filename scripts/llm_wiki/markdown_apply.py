"""Transaction application, including project checkpoint commits."""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Callable, Iterator, Mapping, Sequence

from llm_wiki import markdown_store
from llm_wiki.markdown_records import (
    TargetStateMismatch,
    TransactionFailure,
    TransactionImageError,
    TransactionRecord,
    TransactionStateError,
)
from llm_wiki.markdown_store import CONFLICTED_STATE_CODES, is_target_boundary_error
from llm_wiki.sqlite_transactions import begin_immediate
from model_dlp import DLPContentBlocked, DLPPolicyError


def _operation_states(
    rows: Sequence[sqlite3.Row],
) -> dict[str, tuple[str, str]]:
    return {row["path"]: (row["before_hash"], row["after_hash"]) for row in rows}


def _dlp_code(exc: Exception) -> str:
    if isinstance(exc, DLPPolicyError):
        return "dlp_policy_error"
    return "dlp_content_blocked"


def _conflicted_failure(error: BaseException) -> TransactionFailure | None:
    """The two mismatches a caller can act on; anything else is not ours to name."""
    if not isinstance(error, TargetStateMismatch):
        return None
    code = CONFLICTED_STATE_CODES.get(error.state_name)
    if code is None:
        return None
    return TransactionFailure(str(error), code, "conflicted")


class TransactionApplication:
    def apply(
        self,
        transaction_id: str,
        *,
        writer_wait_seconds: float | None = None,
        deadline: float = float("inf"),
        cancelled: Callable[[], bool] | None = None,
    ) -> TransactionRecord:
        self._require_operation_active(deadline, cancelled)
        with self.writer_gate(wait_seconds=writer_wait_seconds):
            self._require_operation_active(deadline, cancelled)
            previous = self._enter_recovery_scope(deadline, cancelled)
            try:
                return self._apply_locked(transaction_id)
            except (DLPContentBlocked, DLPPolicyError) as exc:
                self._quarantine_blocked_publication(transaction_id, exc)
            except TransactionFailure as exc:
                self._record_transaction_failure(transaction_id, exc)
                raise
            except (RuntimeError, ValueError) as exc:
                self._translate_apply_error(transaction_id, exc)
            finally:
                self._leave_recovery_scope(previous)

    def _enter_recovery_scope(
        self, deadline: float, cancelled: Callable[[], bool] | None
    ) -> tuple[object, object]:
        """Install this call's deadline, returning the one it displaced."""
        previous = (
            getattr(self._local, "recovery_deadline", None),
            getattr(self._local, "recovery_cancelled", None),
        )
        self._local.recovery_deadline = deadline
        self._local.recovery_cancelled = cancelled
        return previous

    def _leave_recovery_scope(self, previous: tuple[object, object]) -> None:
        self._local.recovery_deadline, self._local.recovery_cancelled = previous

    def _quarantine_blocked_publication(
        self, transaction_id: str, exc: Exception
    ) -> None:
        code = _dlp_code(exc)
        self._rollback_for_quarantine(transaction_id, code)
        raise TransactionFailure(
            "model output publication was blocked", code, "quarantined"
        ) from exc

    def _record_transaction_failure(
        self, transaction_id: str, exc: TransactionFailure
    ) -> None:
        if exc.code == "precondition_failed":
            self._rollback_for_quarantine(transaction_id, exc.code)
            return
        self._set_transaction_state(transaction_id, exc.state, error_code=exc.code)

    def _require_named_target_boundary(
        self, transaction_id: str, exc: Exception
    ) -> None:
        """Quarantine and name a moved target parent; anything else passes through."""
        if not is_target_boundary_error(exc):
            return
        self._set_transaction_state(
            transaction_id, "quarantined", error_code="parent_identity_changed"
        )
        raise TransactionFailure(
            "target parent identity changed",
            "parent_identity_changed",
            "quarantined",
        ) from exc

    def _translate_apply_error(self, transaction_id: str, exc: Exception) -> None:
        """Name what went wrong, or re-raise what this layer cannot name."""
        self._require_named_target_boundary(transaction_id, exc)
        if isinstance(exc, TransactionImageError):
            recovered = self._recover_corrupt_after_image(transaction_id)
            raise TransactionFailure(
                str(exc), "after_image_corrupt", recovered.state
            ) from exc
        failure = _conflicted_failure(exc)
        if failure is None:
            raise
        self._set_transaction_state(
            transaction_id, failure.state, error_code=failure.code
        )
        raise failure from exc

    @staticmethod
    def _require_applicable_state(record: TransactionRecord) -> None:
        """Only a prepared or half-applied transaction may still be applied."""
        if record.state not in {"prepared", "applying"}:
            raise TransactionStateError(record.state)

    def _apply_locked(self, transaction_id: str) -> TransactionRecord:
        record = self._record(transaction_id)
        if record.state == "committed":
            return record
        self._require_applicable_state(record)
        plan = self._load_verified_plan(record)
        content_guard = self._content_guard(record, plan)
        rows = self._operation_rows(transaction_id)
        operation_states = _operation_states(rows)
        if "project_lease" in record.preconditions:
            return self._apply_project_locked(record, plan, rows, operation_states)
        self._check_preconditions(record.preconditions, operation_states)
        self._reconcile_operation_states(transaction_id, rows)
        self._mark_transaction_state(transaction_id, "applying")
        self._killpoint("after_applying", record.parent_transaction_id)
        self._mutate_all(record, plan, operation_states, content_guard)
        self._check_preconditions(record.preconditions, operation_states)
        self._require_all_after_state(transaction_id)
        self._killpoint("before_commit", record.parent_transaction_id)
        self._mark_transaction_state(transaction_id, "committed")
        result = self._record(transaction_id)
        self._killpoint("after_commit", record.parent_transaction_id)
        return result

    def _mutate_all(
        self,
        record: TransactionRecord,
        plan: Mapping[str, object],
        operation_states: Mapping[str, tuple[str, str]],
        content_guard: object,
    ) -> None:
        rows = self._operation_rows(record.id)
        for row, operation_plan in zip(rows, plan["operations"], strict=True):
            self._require_current_operation_active()
            self._check_preconditions(record.preconditions, operation_states)
            self._mutate_one(record, row, operation_plan, content_guard)

    def _mutate_one(
        self,
        record: TransactionRecord,
        row: sqlite3.Row,
        operation_plan: Mapping[str, object],
        content_guard: object,
    ) -> None:
        if row["applied"]:
            self._require_operation_state(row, row["after_hash"], "after")
            return
        self._mutate_and_mark(
            record.id, row, operation_plan, content_guard=content_guard
        )
        self._killpoint("after_each_target", record.parent_transaction_id)

    def _require_all_after_state(self, transaction_id: str) -> None:
        for row in self._operation_rows(transaction_id):
            self._require_operation_state(row, row["after_hash"], "after")

    def _mark_transaction_state(self, transaction_id: str, state: str) -> None:
        self._require_current_operation_active()
        with self._connect() as database, begin_immediate(
            database, before_commit=self._require_current_operation_active
        ):
            database.execute(
                'UPDATE "transaction" SET state = ?, updated_at = ? WHERE id = ?',
                (state, markdown_store.now(), transaction_id),
            )

    def _apply_project_locked(
        self,
        record: TransactionRecord,
        plan: Mapping[str, object],
        rows: Sequence[sqlite3.Row],
        operation_states: Mapping[str, tuple[str, str]],
    ) -> TransactionRecord:
        content_guard = self._content_guard(record, plan)
        self._check_preconditions(record.preconditions, operation_states)
        self._reconcile_operation_states(record.id, rows)
        rows = self._operation_rows(record.id)
        self._mark_project_applying(record)
        self._killpoint("after_applying", record.parent_transaction_id)
        self._commit_project_operations(
            record, plan, rows, operation_states, content_guard
        )
        result = self._record(record.id)
        self._killpoint("after_commit", record.parent_transaction_id)
        return result

    def _mark_project_applying(self, record: TransactionRecord) -> None:
        self._require_current_operation_active()
        with self._connect() as database, begin_immediate(
            database, before_commit=self._require_current_operation_active
        ):
            self._assert_writer_ownership(database)
            database.execute(
                'UPDATE "transaction" SET state = \'applying\', updated_at = ? '
                "WHERE id = ?",
                (markdown_store.now(), record.id),
            )

    def _commit_project_operations(
        self,
        record: TransactionRecord,
        plan: Mapping[str, object],
        rows: Sequence[sqlite3.Row],
        operation_states: Mapping[str, tuple[str, str]],
        content_guard: object,
    ) -> None:
        """Every project target moves inside the database transaction that
        commits its checkpoint, so a failure rewinds both together.

        The restore is outside the lock because the commit itself can fail: the
        caller's deadline lands in `before_commit`, which runs as the lock exits.
        See `docs/research/2026-09-18-the-restore-covers-the-commit-too.md`.
        """
        changed: list[sqlite3.Row] = []
        try:
            self._commit_project_under_lock(
                record, plan, rows, operation_states, content_guard, changed
            )
        except BaseException:
            with self._uncancellable():
                self._restore_inflight_operations(record.id, changed)
            raise

    @contextlib.contextmanager
    def _uncancellable(self) -> Iterator[None]:
        """Compensation runs to the end; the deadline that stopped the write
        must not also stop the undo and leave the vault half-written."""
        previous = self._enter_recovery_scope(float("inf"), None)
        try:
            yield
        finally:
            self._leave_recovery_scope(previous)

    def _commit_project_under_lock(
        self,
        record: TransactionRecord,
        plan: Mapping[str, object],
        rows: Sequence[sqlite3.Row],
        operation_states: Mapping[str, tuple[str, str]],
        content_guard: object,
        changed: list[sqlite3.Row],
    ) -> None:
        with self._connect() as database, begin_immediate(
            database, before_commit=self._require_current_operation_active
        ):
            self._assert_writer_ownership(database)
            self._check_preconditions(
                record.preconditions, operation_states, database=database
            )
            self._check_project_transaction_head(database, record.id)
            self._local.mutation_database = database
            try:
                self._mutate_project_operations(
                    database, record, plan, rows, operation_states,
                    content_guard, changed,
                )
                self._commit_project_checkpoint(
                    database, record, rows, operation_states
                )
            finally:
                self._local.mutation_database = None

    def _mutate_project_operations(
        self,
        database: sqlite3.Connection,
        record: TransactionRecord,
        plan: Mapping[str, object],
        rows: Sequence[sqlite3.Row],
        operation_states: Mapping[str, tuple[str, str]],
        content_guard: object,
        changed: list[sqlite3.Row],
    ) -> None:
        operations = plan["operations"]
        assert isinstance(operations, list)
        for row, operation_plan in zip(rows, operations, strict=True):
            self._require_current_operation_active()
            self._check_preconditions(
                record.preconditions, operation_states, database=database
            )
            if not self._mutate_project_one(
                record, row, operation_plan, content_guard, changed
            ):
                continue
            self._check_preconditions(
                record.preconditions, operation_states, database=database
            )

    def _mutate_project_one(
        self,
        record: TransactionRecord,
        row: sqlite3.Row,
        operation_plan: object,
        content_guard: object,
        changed: list[sqlite3.Row],
    ) -> bool:
        """True when this call moved the target; False when it was already applied."""
        if row["applied"]:
            self._require_operation_state(row, row["after_hash"], "after")
            return False
        assert isinstance(operation_plan, Mapping)
        self._mutate_and_mark(
            record.id, row, operation_plan, content_guard=content_guard
        )
        changed.append(row)
        self._killpoint("after_each_target", record.parent_transaction_id)
        return True

    def _commit_project_checkpoint(
        self,
        database: sqlite3.Connection,
        record: TransactionRecord,
        rows: Sequence[sqlite3.Row],
        operation_states: Mapping[str, tuple[str, str]],
    ) -> None:
        self._check_preconditions(
            record.preconditions, operation_states, database=database
        )
        for row in rows:
            self._require_operation_state(row, row["after_hash"], "after")
        self._killpoint("before_commit", record.parent_transaction_id)
        self._require_current_operation_active()
        self._check_preconditions(
            record.preconditions, operation_states, database=database
        )
        database.execute(
            'UPDATE "transaction" SET state = \'committed\', updated_at = ? '
            "WHERE id = ?",
            (markdown_store.now(), record.id),
        )
        database.execute(
            "UPDATE project_checkpoints SET state = 'committed' "
            "WHERE transaction_id = ? AND state = 'prepared'",
            (record.id,),
        )
        database.execute(
            "UPDATE project_checkpoint_attempts SET state = 'committed' "
            "WHERE transaction_id = ? AND state = 'prepared'",
            (record.id,),
        )
