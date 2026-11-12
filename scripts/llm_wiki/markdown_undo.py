"""Transaction undo, history pruning and deletion blockers."""

from __future__ import annotations

import sqlite3
import time
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from llm_wiki import markdown_store
from llm_wiki.coordinator_schema import (
    COORDINATOR_V3_CONTRACT,
    ensure_coordinator_v3_indexes,
)
from llm_wiki.formats.canonical import sha256_bytes
from llm_wiki.markdown_records import (
    MarkdownChange,
    RefusedArgument,
    RefusedOperation,
    TransactionRecord,
)
from llm_wiki.markdown_store import (
    ABSENT,
    UNAVAILABLE_STATE,
    UNDO_RETENTION_DAYS,
    lstat_or_none,
    optional_before_state,
    parse_stored_timestamp,
    stored_before_state,
    stored_timestamp_text,
)
from llm_wiki.sqlite_transactions import begin_immediate
from transaction_lineage import quarantine_witnesses, resolved_quarantines

# How long a settled transaction's row and a committed checkpoint's attempts are
# kept after their images are gone: the hot window the archive contract already
# uses. Checkpoint rows and quarantined transactions are never removed here. See
# `docs/research/2026-09-24-every-store-has-a-bound.md`.
HISTORY_RETENTION_DAYS = 90


_PRUNE_COMMITTED_ATTEMPTS = (
    "DELETE FROM project_checkpoint_attempts WHERE created_at < ? AND EXISTS ("
    "SELECT 1 FROM project_checkpoints AS c WHERE c.project = project_checkpoint_attempts.project "
    "AND c.sequence = project_checkpoint_attempts.sequence AND c.state = 'committed')"
)


# The operation families whose settled rows are authority for something that
# outlives them, and so are never pruned: compile receipts and quarantine records,
# daily archive attestations, capture terminal records and episode consolidation.
# Every other family goes once settled, pruned of its images and past the window:
# its content lives in the Markdown it wrote, and the only thing the row still
# answers is a replay of the same operation, whose sources (hook replays within
# seconds, queue tasks kept 30 days) are gone long before 90 days. Kept by name,
# pruned by default, so a new family is bounded without being listed. See
# `docs/research/2026-09-25-history-prune-keeps-every-authority-row.md` and
# `docs/research/2026-09-26-a-new-operation-family-is-bounded-by-default.md`.
KEPT_OPERATION_FAMILIES = ("compile", "compile-quarantine", "archive-remove", "capture-markdown", "episodes")


# A reservation on a checkpoint another attempt committed will never run (C-11).
_PRUNE_SPENT_RESERVATIONS = (
    "DELETE FROM project_checkpoint_attempts WHERE state = 'reserved' AND EXISTS ("
    "SELECT 1 FROM project_checkpoints AS c WHERE c.project = project_checkpoint_attempts.project "
    "AND c.sequence = project_checkpoint_attempts.sequence AND c.state = 'committed')"
)
_SETTLED_PAST_WINDOW = (
    "state IN ('committed', 'discarded') AND artifacts_pruned_at IS NOT NULL AND updated_at < ?"
)


# A committed checkpoint carries its own event, and `rebuild_journal` reads only
# that; the transaction that appended it is history once settled past the window.
_RELEASE_SETTLED_CHECKPOINT_TRANSACTIONS = (
    "UPDATE project_checkpoints SET transaction_id = NULL WHERE state = 'committed' "
    'AND transaction_id IN (SELECT id FROM "transaction" WHERE ' + _SETTLED_PAST_WINDOW + ")"
)
_PRUNABLE_TRANSACTION = (
    _SETTLED_PAST_WINDOW + " "
    "AND NOT (" + " OR ".join("operation_id LIKE ?" for _ in KEPT_OPERATION_FAMILIES) + ") "
    "AND id NOT IN (SELECT transaction_id FROM project_checkpoints WHERE transaction_id IS NOT NULL) "
    "AND id NOT IN (SELECT transaction_id FROM project_checkpoint_attempts WHERE transaction_id IS NOT NULL)"
)
_PRUNABLE_TRANSACTION_IDS = 'SELECT id FROM "transaction" WHERE ' + _PRUNABLE_TRANSACTION + " ORDER BY updated_at"


# Re-checked per row at delete time, so a row that became referenced since the
# candidates were read is kept.
_PRUNE_ONE_TRANSACTION = 'DELETE FROM "transaction" WHERE id = ? AND ' + _PRUNABLE_TRANSACTION


def _kept_family_patterns() -> tuple[str, ...]:
    return tuple(f"{family}:%" for family in KEPT_OPERATION_FAMILIES)


# How old a transaction directory no row names must be before the prune removes it.
UNNAMED_ARTIFACT_AGE_SECONDS = 3600


def _abandoned_artifact_root(root: Path, named: frozenset[str], cutoff: float) -> bool:
    if root.name.startswith(".") or root.name in named or not root.is_dir():
        return False
    return root.stat().st_mtime < cutoff


def _undoable(
    row: sqlite3.Row,
    before_states: dict[int, object],
    current_hashes: dict[int, str],
) -> bool:
    position = row["position"]
    if not row["applied"] or position not in before_states:
        return False
    return current_hashes[position] == row["after_hash"]


def _deletion_state_blocker(row: sqlite3.Row) -> str | None:
    """None means this transaction's own state does not block deletion."""
    if row["state"] in {"preparing", "prepared", "applying"}:
        return "nonterminal_transaction"
    if row["state"] in {"conflicted", "quarantined"}:
        return row["error_code"] or "transaction_requires_attention"
    return None


def _deletion_blocker_code(row: sqlite3.Row, now: datetime) -> str | None:
    """None means this transaction does not stand in the way of deletion."""
    state_blocker = _deletion_state_blocker(row)
    if state_blocker is not None:
        return state_blocker
    if _within_undo_retention(row, now):
        return "undo_retention"
    return None


def _within_undo_retention(row: sqlite3.Row, now: datetime) -> bool:
    if row["state"] != "committed" or row["artifacts_pruned_at"] is not None:
        return False
    return parse_stored_timestamp(row["updated_at"]) >= now - timedelta(days=UNDO_RETENTION_DAYS)


STAGED_PRUNE_MARK = ".pruning-"


def _staged_prune_owner(name: str) -> str:
    """`.<id>.pruning-<uuid>` names the transaction whose images it holds."""
    return name[1 : name.rindex(STAGED_PRUNE_MARK)]


def _delete_candidates_until(
    database: sqlite3.Connection, candidates: list[str], arguments: tuple[str, ...], stop_at: float
) -> tuple[int, int]:
    processed = deleted = 0
    for identifier in candidates:
        deleted += database.execute(_PRUNE_ONE_TRANSACTION, (identifier, *arguments)).rowcount
        processed += 1
        if time.monotonic() >= stop_at:
            break
    return processed, deleted


def _prune_cutoff(retention_days: int, now: datetime | None) -> datetime:
    # The floor is the undo window itself, not a literal. It was 30 because the
    # contract promised point-undo of any committed write for a month; the owner
    # exchanged that on 2026-09-02 for a daily snapshot and a copy off this
    # machine, which is what every database does with undo data and what no
    # database does is keep it for a month.
    if retention_days < UNDO_RETENTION_DAYS:
        raise RefusedArgument(
            f"retention_days must be at least {UNDO_RETENTION_DAYS}",
            "retention_too_short",
        )
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return current - timedelta(days=retention_days)


class TransactionUndoAndPrune:
    def _undo_images(
        self, transaction_id: str, rows: Sequence[sqlite3.Row]
    ) -> tuple[dict[int, object], dict[int, str], bool]:
        before_states: dict[int, object] = {}
        current_hashes: dict[int, str] = {}
        ambiguous = False
        for row in rows:
            current = self._boundary_checked_hash(row)
            current_hashes[row["position"]] = current
            if not self._collect_undo_image(
                transaction_id, row, current, before_states
            ):
                ambiguous = True
        return before_states, current_hashes, ambiguous

    def _collect_undo_image(
        self,
        transaction_id: str,
        row: sqlite3.Row,
        current: str,
        before_states: dict[int, object],
    ) -> bool:
        """False marks an operation whose current state cannot be explained."""
        if current not in {row["before_hash"], row["after_hash"]}:
            return False
        if not row["applied"]:
            return current != row["after_hash"]
        return self._collect_applied_undo_image(
            transaction_id, row, current, before_states
        )

    def _collect_applied_undo_image(
        self,
        transaction_id: str,
        row: sqlite3.Row,
        current: str,
        before_states: dict[int, object],
    ) -> bool:
        """An applied target already rolled back needs no image; the rest need one."""
        if current == row["before_hash"]:
            return True
        state = optional_before_state(self.transaction_root, transaction_id, row)
        if state is UNAVAILABLE_STATE:
            return False
        before_states[row["position"]] = state
        return True

    def _undo_applied_operations(
        self,
        transaction_id: str,
        rows: Sequence[sqlite3.Row],
        before_states: dict[int, object],
        current_hashes: dict[int, str],
        ambiguous: bool,
    ) -> bool:
        for row in rows:
            if not _undoable(row, before_states, current_hashes):
                continue
            if not self._try_apply_inverse(
                transaction_id, row, before_states[row["position"]]
            ):
                ambiguous = True
        return ambiguous

    def undo(
        self,
        transaction_id: str,
        *,
        deadline: float = float("inf"),
        cancelled: Callable[[], bool] | None = None,
    ) -> TransactionRecord:
        self._require_operation_active(deadline, cancelled)
        with self.writer_gate():
            self._require_operation_active(deadline, cancelled)
            return self._prepare_undo(
                transaction_id, deadline=deadline, cancelled=cancelled
            )

    def _prepare_undo(
        self,
        transaction_id: str,
        *,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> TransactionRecord:
        self._require_undoable(transaction_id)
        rows = self._operation_rows(transaction_id)
        self._require_targets_unchanged(rows, deadline, cancelled)
        changes, preconditions = self._undo_changes(
            transaction_id, rows, deadline, cancelled
        )
        return self.prepare(
            changes,
            operation_id=f"undo:{transaction_id}:{uuid.uuid4().hex}",
            preconditions=preconditions,
            _parent_transaction_id=transaction_id,
            deadline=deadline,
            cancelled=cancelled,
        )

    def prune(
        self,
        *,
        retention_days: int = UNDO_RETENTION_DAYS,
        now: datetime | None = None,
        deadline: float = float("inf"),
        cancelled: Callable[[], bool] | None = None,
    ) -> int:
        self._require_operation_active(deadline, cancelled)
        cutoff = _prune_cutoff(retention_days, now)
        with self.writer_gate():
            self._recover_interrupted_prunes()
            self._remove_unnamed_artifact_roots()
            return self._prune_rows_older_than(cutoff, deadline, cancelled)

    def _prune_rows_older_than(
        self, cutoff: datetime, deadline: float, cancelled: Callable[[], bool] | None
    ) -> int:
        pruned = 0
        for row in self._prunable_rows():
            self._require_operation_active(deadline, cancelled)
            if parse_stored_timestamp(row["updated_at"]) < cutoff:
                pruned += self._prune_one(row, deadline, cancelled)
        return pruned

    def prune_history(
        self,
        *,
        retention_days: int = HISTORY_RETENTION_DAYS,
        now: datetime | None = None,
        deadline: float = float("inf"),
        slice_seconds: float = float("inf"),
    ) -> dict[str, int | bool]:
        """Drop settled history past its window: committed attempts, then settled rows.

        Attempts first, so the transactions they named are no longer named, then a
        committed checkpoint lets go of its settled transaction. Every family but
        the authority ones (`KEPT_OPERATION_FAMILIES`) goes; a transaction an
        unsettled checkpoint names stays, and so does every quarantined one and
        every row that ties a quarantine to its resolution
        (`transaction_lineage.quarantine_witnesses`); operations go with their
        transaction (`ON DELETE CASCADE`). Rows go in slices of at most
        `slice_seconds` under the gate, each committed, until `deadline`: a
        killed or late prune keeps what it already did and says it is unfinished
        (audit 2026-09-27 A-6, B-1,
        docs/research/2026-09-27-a-prune-keeps-what-resolves-a-quarantine.md).
        """
        cutoff = stored_timestamp_text(_prune_cutoff(retention_days, now))
        attempts = self._prune_attempts(cutoff)
        candidates = self._prunable_history(cutoff)
        processed, removed = self._prune_in_slices(candidates, cutoff, deadline, slice_seconds)
        return {"attempts": attempts, "transactions": removed, "unfinished": processed < len(candidates)}

    def _prune_attempts(self, cutoff: str) -> int:
        with self.writer_gate(), self._connect() as database, begin_immediate(database):
            if getattr(self, "_database_contract", None) == COORDINATOR_V3_CONTRACT:
                ensure_coordinator_v3_indexes(database)
            spent = database.execute(_PRUNE_SPENT_RESERVATIONS).rowcount
            attempts = spent + database.execute(_PRUNE_COMMITTED_ATTEMPTS, (cutoff,)).rowcount
            database.execute(_RELEASE_SETTLED_CHECKPOINT_TRANSACTIONS, (cutoff,))
        return attempts

    def _prunable_history(self, cutoff: str) -> list[str]:
        """Candidate rows, oldest first, less every witness of a quarantine."""
        with self._connect() as database:
            witnesses = quarantine_witnesses(database)
            rows = database.execute(_PRUNABLE_TRANSACTION_IDS, (cutoff, *_kept_family_patterns()))
            return [str(row[0]) for row in rows if row[0] not in witnesses]

    def _prune_in_slices(
        self, candidates: list[str], cutoff: str, deadline: float, slice_seconds: float
    ) -> tuple[int, int]:
        """(candidates processed, rows deleted)."""
        processed = removed = 0
        while processed < len(candidates) and time.monotonic() < deadline:
            stop_at = min(deadline, time.monotonic() + slice_seconds)
            step, deleted = self._prune_slice(candidates[processed:], cutoff, stop_at)
            processed, removed = processed + step, removed + deleted
        return processed, removed

    def _prune_slice(self, candidates: list[str], cutoff: str, stop_at: float) -> tuple[int, int]:
        """Delete candidates in one committed transaction until `stop_at`; at least one."""
        arguments = (cutoff, *_kept_family_patterns())
        with self.writer_gate(), self._connect() as database, begin_immediate(database):
            return _delete_candidates_until(database, candidates, arguments, stop_at)

    def _recover_interrupted_prunes(self) -> None:
        """Put back the images of every prune that died between rename and mark.

        The writer gate is held, so no live prune can be mid-rename: a staged
        directory here is the debris of a process that is gone, and nothing else
        ever looks at it again. See
        `docs/research/2026-09-18-nothing-half-written-is-left-behind.md`.
        """
        if not self.transaction_root.is_dir():
            return
        for staged in sorted(self.transaction_root.glob(".*.pruning-*")):
            self._restore_staged_prune(staged)

    def _remove_unnamed_artifact_roots(self) -> None:
        """A directory no row names, an hour old, is the debris of a failed prepare.

        A prepare inserts its row seconds after making the directory, outside the
        writer gate, so only an old one is certainly abandoned. See
        `docs/research/2026-09-25-a-transaction-and-its-images-agree.md`.
        """
        if not self.transaction_root.is_dir():
            return
        named = self._transaction_ids()
        cutoff = time.time() - UNNAMED_ARTIFACT_AGE_SECONDS
        for root in self.transaction_root.iterdir():
            if _abandoned_artifact_root(root, named, cutoff):
                self._remove_artifacts(root)

    def _transaction_ids(self) -> frozenset[str]:
        with self._connect() as database:
            return frozenset(str(row[0]) for row in database.execute('SELECT id FROM "transaction"'))

    def _restore_staged_prune(self, staged: Path) -> None:
        """The images go back only where the row still says they are.

        A row marked pruned (or gone) already disowned them: the crash came after
        the mark, so the prune is finished and the images go (audit C-10,
        docs/research/2026-09-25-a-finished-prune-is-not-undone.md).
        """
        owner = _staged_prune_owner(staged.name)
        artifact_root = self.transaction_root / owner
        if artifact_root.exists() or not self._images_still_owned(owner):
            self._remove_artifacts(staged)
            return
        staged.replace(artifact_root)

    def _images_still_owned(self, transaction_id: str) -> bool:
        with self._connect() as database:
            row = database.execute(
                'SELECT artifacts_pruned_at FROM "transaction" WHERE id = ?', (transaction_id,)
            ).fetchone()
        return row is not None and row["artifacts_pruned_at"] is None

    def _prunable_rows(self) -> list[sqlite3.Row]:
        """Settled rows with images, and quarantined ones the rows show resolved.

        A resolved quarantine's images proved nothing any more: the retry that
        committed, or the commit that created its files, is the evidence, and
        the row itself is kept. Before this, 117 quarantined rows held 60.8 MB of
        images on this vault for good (audit 2026-09-27 B-2,
        docs/research/2026-09-27-a-resolved-quarantine-lets-go-of-its-images.md).
        """
        with self._connect() as database:
            resolved = resolved_quarantines(database)
            rows = database.execute(
                'SELECT id, updated_at, state FROM "transaction" '
                "WHERE state IN ('committed', 'discarded', 'quarantined') "
                "AND artifacts_pruned_at IS NULL"
            )
            return [row for row in rows if row["state"] != "quarantined" or row["id"] in resolved]

    def _prune_one(
        self,
        row: sqlite3.Row,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> int:
        """The images are staged aside first, so a failure can put them back."""
        artifact_root = self.transaction_root / row["id"]
        if not artifact_root.exists():
            # Nothing left to keep; marked, so the history prune can take the row (C-9).
            self._mark_artifacts_pruned(row["id"], deadline, cancelled)
            return 0
        staged_root = self.transaction_root / (
            f".{row['id']}{STAGED_PRUNE_MARK}{uuid.uuid4().hex}"
        )
        artifact_root.replace(staged_root)
        try:
            self._require_operation_active(deadline, cancelled)
            self._mark_artifacts_pruned(row["id"], deadline, cancelled)
        except BaseException:
            staged_root.replace(artifact_root)
            raise
        self._remove_artifacts(staged_root)
        return 1

    def _mark_artifacts_pruned(
        self,
        transaction_id: str,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> None:
        with self._connect() as database, begin_immediate(
            database,
            before_commit=lambda: self._require_operation_active(deadline, cancelled),
        ):
            database.execute(
                'UPDATE "transaction" SET artifacts_pruned_at = ? WHERE id = ?',
                (markdown_store.now(), transaction_id),
            )

    def deletion_blockers(self) -> list[dict[str, str]]:
        now = datetime.now(UTC)
        with self._connect() as database:
            rows = list(
                database.execute(
                    'SELECT id, state, error_code, updated_at, artifacts_pruned_at '
                    'FROM "transaction" ORDER BY created_at, id'
                )
            )
        coded = ((row, _deletion_blocker_code(row, now)) for row in rows)
        return [
            {"transaction_id": row["id"], "state": row["state"], "code": code}
            for row, code in coded
            if code is not None
        ]

    def _require_undoable(self, transaction_id: str) -> None:
        original = self._record(transaction_id)
        if original.state != "committed":
            raise RefusedOperation(
                "only a committed transaction can be undone",
                "transaction_not_committed",
            )
        if parse_stored_timestamp(original.updated_at) < datetime.now(
            UTC
        ) - timedelta(days=UNDO_RETENTION_DAYS):
            raise RefusedOperation(
                f"transaction is outside the {UNDO_RETENTION_DAYS}-day undo window",
                "undo_window_expired",
            )
        self._require_retained_undo_images(transaction_id)

    def _require_retained_undo_images(self, transaction_id: str) -> None:
        """Undo needs the before-images; a pruned transaction cannot be undone.

        Images a killed prune staged aside are still owned by their row, so they
        are put back first — under the gate undo holds — instead of refusing an
        undo the window still allows (audit 2026-09-26 C-12,
        docs/research/2026-09-26-a-killed-prune-neither-blocks-undo-nor-outruns-its-step.md).
        """
        if not (self.transaction_root / transaction_id).is_dir():
            self._recover_interrupted_prunes()
        if not (self.transaction_root / transaction_id).is_dir():
            raise RuntimeError("transaction undo images are no longer retained")

    def _require_targets_unchanged(
        self,
        rows: Sequence[sqlite3.Row],
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> None:
        for row in rows:
            self._require_operation_active(deadline, cancelled)
            if self._operation_hash(row) != row["after_hash"]:
                raise RefusedOperation(
                    "undo precondition failed: current target changed",
                    "undo_precondition_failed",
                )

    def _undo_changes(
        self,
        transaction_id: str,
        rows: Sequence[sqlite3.Row],
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> tuple[list[MarkdownChange], dict[str, object]]:
        changes: list[MarkdownChange] = []
        preconditions: dict[str, object] = {}
        for row in rows:
            self._require_operation_active(deadline, cancelled)
            preconditions[row["path"]] = row["after_hash"]
            changes.append(self._undo_change(transaction_id, row))
        return changes, preconditions

    def _undo_change(self, transaction_id: str, row: sqlite3.Row) -> MarkdownChange:
        """The inverse of one committed operation, read back from its before-image."""
        if row["before_hash"] == ABSENT:
            return MarkdownChange.delete(row["path"])
        before = self._undo_bytes(transaction_id, row)
        return (
            MarkdownChange.create(row["path"], before)
            if row["after_hash"] == ABSENT
            else MarkdownChange.replace(row["path"], before)
        )

    def _undo_bytes(self, transaction_id: str, row: sqlite3.Row) -> bytes:
        """The before-bytes: its image, or for an append the target cut back to its length."""
        state = stored_before_state(self.transaction_root, transaction_id, row)
        before = self._materialized_state(dict(row, transaction_id=transaction_id), state)
        if sha256_bytes(before) != row["before_hash"]:
            raise RefusedOperation(
                "transaction before-image is corrupt", "before_image_corrupt"
            )
        return before

    def _operation_hash(self, row: sqlite3.Row) -> str:
        with self._stable_parent(row) as (target, parent_descriptor):
            return self._hash_operation_target(target, parent_descriptor)

    def _current_hash(self, path: str) -> str:
        return self._hash_bounded_target(self._target(path))

    def _target_present(self, path: str) -> bool:
        """Whether a name is bound to a file, by `lstat` alone: no open, no read.

        Presence is asked outside any lock about files other writers are growing;
        a hash of a moving file refuses, presence of it must not. See
        `docs/research/2026-09-23-a-presence-check-does-not-hash.md`.
        """
        return lstat_or_none(self._target(path)) is not None
