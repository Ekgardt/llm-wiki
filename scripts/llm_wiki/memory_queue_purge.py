"""Purging terminal tasks into a verified export and restoring them."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from llm_wiki.formats.canonical import canonical_json_bytes, sha256_bytes
from llm_wiki.queue_artifacts import (
    cleanup_export_staging,
    harden_owner_only,
    is_owner_only,
    read_stable_owner_file,
    remove_export_staging,
    write_durable_file,
)
from llm_wiki.queue_contracts import (
    MAX_RESULT_BYTES,
    PurgeReceipt,
    QueueOperationError,
    RestoreReceipt,
)
from llm_wiki.queue_export_proof import MAX_EXPORT_METADATA_BYTES
from llm_wiki.queue_store import (
    RETAINING_QUEUE_QUERIES,
    directory_holds_anything,
    require_active,
    restore_from_export,
)
from llm_wiki.queue_task_records import export_task_record
from llm_wiki.queue_timing import as_utc, timestamp
from llm_wiki.reliability_defaults import DEFAULTS
from llm_wiki.sqlite_transactions import begin_immediate
from reliable_memory import fsync_directory


def _purge_states(include_dead: bool) -> tuple[str, ...]:
    if include_dead:
        return ("succeeded", "cancelled", "dead")
    return ("succeeded", "cancelled")


def _create_export_parent(parent: Path) -> None:
    if not parent.exists():
        parent.mkdir(parents=True)
        harden_owner_only(parent, 0o700)


def _require_export_parent(parent: Path) -> None:
    _create_export_parent(parent)
    if parent.is_symlink() or not parent.is_dir() or not is_owner_only(parent):
        raise QueueOperationError("export_parent_permissions_invalid")


def _purge_selection_changed(
    current: list, task_ids: tuple[str, ...], states: tuple[str, ...], cutoff_stamp: str
) -> bool:
    if len(current) != len(task_ids):
        return True
    return any(
        row["state"] not in states or row["updated_at"] >= cutoff_stamp
        for row in current
    )


def _require_exported_results(
    results_export: Path, result_manifest: list[dict[str, str]]
) -> None:
    """Every exported result reads back with the digest its manifest names."""
    for item in result_manifest:
        exported = results_export / f"{item['id']}.result"
        data = read_stable_owner_file(exported, MAX_RESULT_BYTES)
        if sha256_bytes(data) != item["sha256"]:
            raise QueueOperationError("export_verification_failed")


class MemoryQueuePurge:
    def _has_any_retaining_row(self) -> bool:
        """Whether any queue table still holds a row."""
        with self._connect() as connection:
            return any(
                connection.execute(query).fetchone() is not None
                for query in RETAINING_QUEUE_QUERIES
            )

    def retains_run_directory(self) -> bool:
        if self._has_any_retaining_row():
            return True
        if directory_holds_anything(self.run_dir / "queue-quarantine"):
            return True
        return directory_holds_anything(self.results_dir)

    def _purge_selection(
        self, states: tuple[str, ...], cutoff: datetime
    ) -> tuple[list, dict[str, list]]:
        with self._connect() as connection:
            state_places = ",".join("?" for _ in states)
            rows = connection.execute(
                f"""SELECT * FROM tasks
                    WHERE state IN ({state_places}) AND updated_at < ?
                    ORDER BY created_at, rowid""",  # noqa: S608
                (*states, timestamp(cutoff)),
            ).fetchall()
            histories = {
                str(row["id"]): connection.execute(
                    "SELECT * FROM attempt_history WHERE task_id=? ORDER BY sequence",
                    (row["id"],),
                ).fetchall()
                for row in rows
            }
        return rows, histories

    def _export_results(
        self,
        rows: list,
        results_export: Path,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> list[dict[str, str]]:
        result_manifest: list[dict[str, str]] = []
        for row in rows:
            require_active(deadline, cancelled)
            reference = row["result_reference"]
            if reference is None:
                continue
            digest = row["result_sha256"]
            if not isinstance(digest, str):
                raise QueueOperationError("result_verification_failed")
            result = self._read_result_for_export(str(reference), digest)
            write_durable_file(results_export / f"{row['id']}.result", result)
            result_manifest.append({"id": str(row["id"]), "sha256": digest})
        return result_manifest

    def _verify_export(
        self,
        staging: Path,
        results_export: Path,
        records_bytes: bytes,
        manifest_bytes: bytes,
        result_manifest: list[dict[str, str]],
    ) -> None:
        """Read every exported byte back before anything is deleted."""
        records = read_stable_owner_file(
            staging / "records.json", MAX_EXPORT_METADATA_BYTES
        )
        if records != records_bytes:
            raise QueueOperationError("export_verification_failed")
        _require_exported_results(results_export, result_manifest)
        manifest = read_stable_owner_file(
            staging / "manifest.json", MAX_EXPORT_METADATA_BYTES
        )
        if manifest != manifest_bytes:
            raise QueueOperationError("export_verification_failed")

    def _write_export_package(
        self,
        rows: list,
        records_bytes: bytes,
        task_ids: tuple[str, ...],
        staging: Path,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> None:
        results_export = staging / "results"
        results_export.mkdir()
        harden_owner_only(results_export, 0o700)
        result_manifest = self._export_results(
            rows, results_export, deadline, cancelled
        )
        write_durable_file(staging / "records.json", records_bytes)
        manifest_bytes = canonical_json_bytes(
            {
                "records_sha256": sha256_bytes(records_bytes),
                "results": result_manifest,
                "task_ids": list(task_ids),
            }
        )
        write_durable_file(staging / "manifest.json", manifest_bytes)
        fsync_directory(results_export)
        fsync_directory(staging)
        self._verify_export(
            staging, results_export, records_bytes, manifest_bytes, result_manifest
        )

    def _publish_export(
        self,
        rows: list,
        records_bytes: bytes,
        task_ids: tuple[str, ...],
        export: Path,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> None:
        parent = export.parent
        _require_export_parent(parent)
        cleanup_export_staging(parent, export.name)
        staging = parent / f".{export.name}.staging-{uuid.uuid4().hex}"
        staging.mkdir()
        harden_owner_only(staging, 0o700)
        try:
            self._write_export_package(
                rows, records_bytes, task_ids, staging, deadline, cancelled
            )
            require_active(deadline, cancelled)
            staging.replace(export)
            fsync_directory(parent)
        except Exception:
            remove_export_staging(staging)
            raise

    def _delete_purged_tasks(
        self,
        task_ids: tuple[str, ...],
        states: tuple[str, ...],
        cutoff: datetime,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> None:
        require_active(deadline, cancelled)
        placeholders = ",".join("?" for _ in task_ids)
        with self._connect() as connection, begin_immediate(
            connection,
            before_commit=lambda: require_active(deadline, cancelled),
        ):
            current = connection.execute(
                f"""SELECT id, state, updated_at FROM tasks
                    WHERE id IN ({placeholders})""",  # noqa: S608
                task_ids,
            ).fetchall()
            if _purge_selection_changed(
                current, task_ids, states, timestamp(cutoff)
            ):
                raise QueueOperationError("purge_selection_changed")
            require_active(deadline, cancelled)
            connection.execute("DROP TRIGGER attempt_history_immutable_delete")
            connection.execute(
                f"DELETE FROM attempt_history WHERE task_id IN ({placeholders})",  # noqa: S608
                task_ids,
            )
            connection.execute(
                f"DELETE FROM tasks WHERE id IN ({placeholders})",  # noqa: S608
                task_ids,
            )
            connection.execute(
                """CREATE TRIGGER attempt_history_immutable_delete
                   BEFORE DELETE ON attempt_history BEGIN
                   SELECT RAISE(ABORT, 'attempt history is immutable'); END"""
            )

    def _drop_unreferenced_results(
        self, rows: list, deadline: float, cancelled: Callable[[], bool] | None
    ) -> None:
        for row in rows:
            require_active(deadline, cancelled)
            reference = row["result_reference"]
            if reference is None:
                continue
            with self._connect() as connection:
                retained = connection.execute(
                    "SELECT 1 FROM tasks WHERE result_reference=? LIMIT 1",
                    (reference,),
                ).fetchone()
            if retained is None:
                (self.state_root / str(reference)).unlink(missing_ok=True)
        fsync_directory(self.results_dir)

    def _retire_purged_tasks(
        self,
        rows: list,
        task_ids: tuple[str, ...],
        states: tuple[str, ...],
        cutoff: datetime,
        export: Path,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> None:
        try:
            self._delete_purged_tasks(task_ids, states, cutoff, deadline, cancelled)
        except BaseException:
            remove_export_staging(export)
            raise
        self._drop_unreferenced_results(rows, deadline, cancelled)

    def purge(
        self,
        *,
        terminal_before: datetime,
        export_path: Path,
        include_dead: bool = False,
        deadline: float = float("inf"),
        cancelled: Callable[[], bool] | None = None,
    ) -> PurgeReceipt:
        """Export, verify, then delete terminal tasks older than the cutoff.

        `include_dead` extends the selection to attempts-exhausted tasks. It is
        off by default: a dead task is evidence that work never happened, so
        retiring it stays an explicit operator action.
        """
        require_active(deadline, cancelled)
        states = _purge_states(include_dead)
        retention_cutoff = as_utc(self._clock()) - timedelta(
            days=DEFAULTS.queue_result_retention_days
        )
        cutoff = min(as_utc(terminal_before), retention_cutoff)
        export = Path(export_path).absolute()
        if export.exists():
            raise QueueOperationError("export_exists")
        rows, histories = self._purge_selection(states, cutoff)
        task_ids = tuple(str(row["id"]) for row in rows)
        records_bytes = canonical_json_bytes(
            [
                export_task_record(
                    self._task_from_row(row, histories[str(row["id"])])
                )
                for row in rows
            ]
        )
        self._publish_export(
            rows, records_bytes, task_ids, export, deadline, cancelled
        )
        if task_ids:
            self._retire_purged_tasks(
                rows, task_ids, states, cutoff, export, deadline, cancelled
            )
        return PurgeReceipt(len(task_ids), task_ids)

    def restore(
        self,
        *,
        export_path: Path,
        deadline: float = float("inf"),
        cancelled: Callable[[], bool] | None = None,
    ) -> RestoreReceipt:
        """Re-enqueue the work in one verified purge export.

        A purged task leaves the queue with its history, so restoring it means
        the work runs again: each record returns as a new ready task and the
        receipt maps the exported id to the new one. Nothing is restored unless
        the manifest and every digest verify first.
        """
        return restore_from_export(
            self, export_path=export_path, deadline=deadline, cancelled=cancelled
        )
