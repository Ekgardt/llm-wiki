"""Adopted-queue quarantine of corrupt rows and their export."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator, Mapping
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from llm_wiki.queue_artifacts import (
    harden_owner_only,
    read_stable_owner_file,
    write_durable_file,
)
from llm_wiki.queue_evidence_records import (
    CorruptEvidence,
    capture_seal_digest,
    corrupt_disposition_key,
    corrupt_disposition_record,
    corrupt_manifest,
    corrupt_metadata,
    require_identical_seal,
)
from llm_wiki.queue_export_proof import (
    verify_corrupt_package_bytes,
)
from llm_wiki.queue_task_records import (
    attempt_history_entry,
)
from llm_wiki.queue_validation import (
    check_consumer_id,
    check_consumer_kind,
    check_quarantine_names,
)

if TYPE_CHECKING:
    from operational_ownership import OwnerLease
from llm_wiki import queue_store
from llm_wiki.formats.canonical import canonical_json_bytes, sha256_bytes
from llm_wiki.formats.queue_payload import (
    validate_payload_blob,
)
from llm_wiki.queue_contracts import (
    CaptureTaskBinding,
    CorruptExportProgress,
    QueueOperationError,
)
from llm_wiki.queue_store import require_active
from llm_wiki.queue_timing import timestamp
from llm_wiki.sqlite_transactions import begin_immediate


def _require_disposable_rows(
    current_task: sqlite3.Row, current_operation: sqlite3.Row, incoming_count: int
) -> None:
    """The task and its operation are in the exact states disposal needs."""
    observed = (
        current_task["state"],
        current_operation["state"],
        current_operation["lineage_generation"],
        current_operation["link_count"],
    )
    expected = (
        "quarantine_pending",
        "manifested",
        current_task["lineage_generation"],
        incoming_count,
    )
    if observed != expected:
        raise QueueOperationError("corrupt_export_verification_failed")


def _blocked_corrupt_progress(
    operation: sqlite3.Row, *, task_id: str, operation_id: str
) -> CorruptExportProgress:
    return CorruptExportProgress(
        task_id=task_id,
        operation_id=operation_id,
        state="blocked",
        pages_written=int(operation["page_count"]),
        links_exported=int(operation["link_count"]),
        complete=False,
        code="capture_link_conflicted",
    )


def _quarantined_corrupt_progress(
    operation: sqlite3.Row, *, task_id: str, operation_id: str
) -> CorruptExportProgress:
    return CorruptExportProgress(
        task_id=task_id,
        operation_id=operation_id,
        state="quarantined",
        pages_written=int(operation["page_count"]),
        links_exported=int(operation["link_count"]),
        complete=True,
        code=None,
    )


def _record_capture_seal(
    database: sqlite3.Connection,
    *,
    task_id: str,
    consumer_kind: str,
    consumer_id: str,
    active_link_digest: str,
    seal_digest: str,
    now: datetime,
) -> None:
    """Seal the binding, accepting a repeat of the identical seal."""
    existing = database.execute(
        "SELECT * FROM capture_task_link_seals WHERE task_id=?", (task_id,)
    ).fetchone()
    if existing is not None:
        require_identical_seal(
            existing,
            consumer_kind=consumer_kind,
            consumer_id=consumer_id,
            active_link_digest=active_link_digest,
            seal_digest=seal_digest,
        )
        return
    inserted = database.execute(
        """INSERT INTO capture_task_link_seals(
               task_id,active_digest,consumer_kind,consumer_id,
               seal_digest,sealed_at
           ) VALUES (?,?,?,?,?,?)""",
        (
            task_id,
            active_link_digest,
            consumer_kind,
            consumer_id,
            seal_digest,
            timestamp(now),
        ),
    ).rowcount
    if inserted != 1:
        raise QueueOperationError("capture_link_seal_failed")


class QueueQuarantineOperations:
    def _intent_coordinator(self):
        """The coordinator that pairs with this queue: adopted, or the candidate."""
        from markdown_transaction import MarkdownCoordinator, active_markdown_coordinator

        path = self.coordinator_path
        if path is None:
            path = self.state_root / "run" / "markdown-transactions-v3.candidate.sqlite3"
        if self.vault is not None and path == self.state_root / "run" / "markdown-transactions-v3.sqlite3":
            return active_markdown_coordinator(self.vault, self.state_root)
        return MarkdownCoordinator.from_v3_candidate(path, state_root=self.state_root)

    @contextmanager
    def _corrupt_intent_fence(
        self, task_id: str, *, owner: OwnerLease
    ) -> Iterator[tuple[CaptureTaskBinding | None, object | None]]:
        try:
            binding = self.active_capture_binding(None, task_id)
        except QueueOperationError:
            yield None, None
            return
        if binding.intent_id is None:
            yield binding, None
            return
        coordinator = self._intent_coordinator()
        fence = coordinator.acquire_intent_fence(
            binding.intent_id, mode="operator", owner=owner
        )
        try:
            yield binding, fence
        finally:
            coordinator.release_intent_fence(fence)

    def seal_capture_binding(
        self,
        task_id: str,
        *,
        consumer_kind: Literal["transaction", "terminal", "corrupt-disposition"],
        consumer_id: str,
        active_link_digest: str,
        before_side_effect: Callable[[], None] | None = None,
    ) -> CaptureTaskBinding:
        check_consumer_kind(consumer_kind)
        check_consumer_id(consumer_id)
        now = queue_store.utc_now()
        seal_digest = capture_seal_digest(
            task_id=task_id,
            consumer_kind=consumer_kind,
            consumer_id=consumer_id,
            active_link_digest=active_link_digest,
        )
        with closing(self._connect()) as database, begin_immediate(database):
            active = self.active_capture_binding(database, task_id)
            if active.active_digest != active_link_digest:
                raise QueueOperationError("capture_link_conflicted")
            _record_capture_seal(
                database,
                task_id=task_id,
                consumer_kind=consumer_kind,
                consumer_id=consumer_id,
                active_link_digest=active_link_digest,
                seal_digest=seal_digest,
                now=now,
            )
        if before_side_effect is not None:
            before_side_effect()
        return CaptureTaskBinding(
            task_id=active.task_id,
            intent_id=active.intent_id,
            intent_sha256=active.intent_sha256,
            handler_version=active.handler_version,
            active_digest=active.active_digest,
            seal_digest=seal_digest,
        )

    @staticmethod
    def _publish_corrupt_fixed_files(
        package: Path,
        *,
        payload: bytes,
        history: bytes,
        metadata: bytes,
    ) -> None:
        package.parent.mkdir(parents=True, exist_ok=True)
        harden_owner_only(package.parent, 0o700)
        if not package.exists():
            package.mkdir()
            harden_owner_only(package, 0o700)
        for name, data in (
            ("payload.bin", payload),
            ("attempt-history.json", history),
            ("task-metadata.json", metadata),
        ):
            write_durable_file(package / name, data)

    @staticmethod
    def _insert_corrupt_disposition(
        database: sqlite3.Connection,
        *,
        task_id: str,
        operation_id: str,
        package_path: str,
        manifest_sha256: str,
        disposition_sha256: str,
        active_link_digest: str | None,
        disposed_at: str,
    ) -> None:
        inserted = database.execute(
            """INSERT INTO corrupt_dispositions(
                   task_id,operation_id,package_path,manifest_sha256,
                   disposition_sha256,active_link_digest,disposed_at
               ) VALUES (?,?,?,?,?,?,?)""",
            (
                task_id,
                operation_id,
                package_path,
                manifest_sha256,
                disposition_sha256,
                active_link_digest,
                disposed_at,
            ),
        ).rowcount
        if inserted != 1:
            raise QueueOperationError("corrupt_disposition_failed")

    @staticmethod
    def _corrupt_task_row(
        database: sqlite3.Connection, task_id: str
    ) -> sqlite3.Row:
        """The task being quarantined, in a state that allows it."""
        row = database.execute(
            "SELECT * FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        if row is None:
            raise KeyError(task_id)
        if row["state"] not in {"dead", "quarantine_pending"}:
            raise QueueOperationError("corrupt_quarantine_state_invalid")
        return row

    @staticmethod
    def _corrupt_attempt_history(
        database: sqlite3.Connection, task_id: str
    ) -> bytes:
        rows = database.execute(
            """SELECT attempt,started_at,finished_at,outcome,error_code
               FROM attempt_history WHERE task_id=? ORDER BY sequence""",
            (task_id,),
        ).fetchall()
        return canonical_json_bytes([attempt_history_entry(item) for item in rows])

    def _corrupt_evidence(
        self,
        database: sqlite3.Connection,
        task_id: str,
        reason: str,
        actor: str,
    ) -> CorruptEvidence:
        """Everything the export is derived from, read under one transaction."""
        row = self._corrupt_task_row(database, task_id)
        raw = bytes(row["payload_blob"])
        raw_sha256 = validate_payload_blob(
            raw, row["input_hash"], parse=False
        ).input_hash
        history = self._corrupt_attempt_history(database, task_id)
        metadata = corrupt_metadata(task_id, row)
        history_sha256 = sha256_bytes(history)
        metadata_sha256 = sha256_bytes(metadata)
        return CorruptEvidence(
            raw=raw,
            history=history,
            metadata=metadata,
            raw_sha256=raw_sha256,
            history_sha256=history_sha256,
            metadata_sha256=metadata_sha256,
            disposition_key=corrupt_disposition_key(
                actor=actor,
                history_sha256=history_sha256,
                metadata_sha256=metadata_sha256,
                raw_sha256=raw_sha256,
                reason=reason,
                task_id=task_id,
            ),
            lineage_generation=int(row["lineage_generation"]),
        )

    def _begin_corrupt_export(
        self,
        database: sqlite3.Connection,
        *,
        task_id: str,
        evidence: CorruptEvidence,
        operation_id: str,
        task_fence: object,
        actor: str,
        reason: str,
        now: datetime,
    ) -> None:
        """Record a new export operation and move the task into quarantine."""
        inserted = database.execute(
            """INSERT INTO corrupt_export_operations(
                   operation_id,task_id,disposition_key,
                   task_fence_token_digest,task_fence_epoch,
                   intent_fence_digest,raw_sha256,history_sha256,
                   metadata_sha256,lineage_generation,cursor_task_id,
                   link_count,page_count,rolling_root,state,
                   actor_identity,reason,created_at,updated_at
               ) VALUES (?,?,?,?,?,NULL,?,?,?,?, '',0,0,?,'exporting',?,?,?,?)""",
            (
                operation_id,
                task_id,
                evidence.disposition_key,
                sha256_bytes(task_fence.token.encode("utf-8")),
                task_fence.epoch,
                evidence.raw_sha256,
                evidence.history_sha256,
                evidence.metadata_sha256,
                evidence.lineage_generation,
                sha256_bytes(b""),
                actor,
                reason,
                timestamp(now),
                timestamp(now),
            ),
        ).rowcount
        if inserted != 1:
            raise QueueOperationError("corrupt_export_start_failed")
        changed = database.execute(
            """UPDATE tasks SET state='quarantine_pending',updated_at=?
               WHERE id=? AND state='dead'""",
            (timestamp(now), task_id),
        ).rowcount
        if changed != 1:
            raise QueueOperationError("corrupt_quarantine_start_failed")

    @staticmethod
    def _corrupt_export_conflict(
        database: sqlite3.Connection, existing: sqlite3.Row, task_id: str
    ) -> CorruptExportProgress:
        """A different export already ran; a disposed one is reported as done."""
        if existing["state"] == "disposed":
            disposition = database.execute(
                "SELECT * FROM corrupt_dispositions WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if disposition is not None:
                return CorruptExportProgress(
                    task_id=task_id,
                    operation_id=str(existing["operation_id"]),
                    state="quarantined",
                    pages_written=int(existing["page_count"]),
                    links_exported=int(existing["link_count"]),
                    complete=True,
                    code=None,
                )
        raise QueueOperationError("corrupt_export_conflict")

    def _open_corrupt_export(
        self,
        database: sqlite3.Connection,
        *,
        task_id: str,
        evidence: CorruptEvidence,
        task_fence: object,
        actor: str,
        reason: str,
        now: datetime,
    ) -> tuple[str, str] | CorruptExportProgress:
        """The operation to continue, or the progress of one already finished."""
        operation_id = f"corrupt-export:{evidence.disposition_key}"
        existing = database.execute(
            "SELECT * FROM corrupt_export_operations WHERE task_id=?",
            (task_id,),
        ).fetchone()
        if existing is None:
            self._begin_corrupt_export(
                database,
                task_id=task_id,
                evidence=evidence,
                operation_id=operation_id,
                task_fence=task_fence,
                actor=actor,
                reason=reason,
                now=now,
            )
            return operation_id, evidence.disposition_key
        if (
            existing["operation_id"] == operation_id
            and existing["disposition_key"] == evidence.disposition_key
        ):
            return str(existing["operation_id"]), str(existing["disposition_key"])
        return self._corrupt_export_conflict(database, existing, task_id)

    def _advance_corrupt_export_once(
        self, task_id: str, operation_id: str, package: Path
    ) -> None:
        """Move an in-progress export one page forward, under its own lock."""
        with closing(self._connect()) as database, begin_immediate(database):
            operation = database.execute(
                "SELECT * FROM corrupt_export_operations WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if operation is None:
                raise QueueOperationError("corrupt_export_lost")
            if operation["state"] == "exporting":
                self._advance_corrupt_export(
                    database, operation, operation_id, task_id, package
                )

    def _corrupt_export_operation(self, task_id: str) -> sqlite3.Row:
        with closing(self._connect()) as database:
            operation = database.execute(
                "SELECT * FROM corrupt_export_operations WHERE task_id=?",
                (task_id,),
            ).fetchone()
        if operation is None:
            raise QueueOperationError("corrupt_export_lost")
        return operation

    def _quarantine_corrupt_owned(
        self,
        task_id: str,
        *,
        reason: str,
        actor: str,
        task_fence: object,
        capture_binding: object,
        intent_fence: object,
    ) -> CorruptExportProgress:
        """Export and dispose one corrupt task while holding every fence."""
        now = queue_store.utc_now()
        with closing(self._connect()) as database, begin_immediate(database):
            evidence = self._corrupt_evidence(database, task_id, reason, actor)
            opened = self._open_corrupt_export(
                database,
                task_id=task_id,
                evidence=evidence,
                task_fence=task_fence,
                actor=actor,
                reason=reason,
                now=now,
            )
        if isinstance(opened, CorruptExportProgress):
            return opened
        operation_id, disposition_key = opened
        package = self.results_dir / f"corrupt-{disposition_key}"
        self._publish_corrupt_fixed_files(
            package,
            payload=evidence.raw,
            history=evidence.history,
            metadata=evidence.metadata,
        )
        self._advance_corrupt_export_once(task_id, operation_id, package)
        operation = self._corrupt_export_operation(task_id)
        if operation["state"] != "manifested":
            return CorruptExportProgress(
                task_id=task_id,
                operation_id=operation_id,
                state="quarantine_pending",
                pages_written=int(operation["page_count"]),
                links_exported=int(operation["link_count"]),
                complete=False,
                code=None,
            )
        return self._dispose_corrupt_export(
            operation,
            operation_id=operation_id,
            task_id=task_id,
            disposition_key=disposition_key,
            package=package,
            raw=evidence.raw,
            history=evidence.history,
            metadata=evidence.metadata,
            actor=actor,
            reason=reason,
            task_fence=task_fence,
            capture_binding=capture_binding,
            intent_fence=intent_fence,
        )

    def quarantine_corrupt(
        self,
        task_id: str,
        *,
        reason: str,
        owner: OwnerLease,
        deadline: float = float("inf"),
        cancelled: Callable[[], bool] | None = None,
    ) -> CorruptExportProgress:
        from operational_ownership import OwnerLease, current_actor_identity

        require_active(deadline, cancelled)
        if not isinstance(owner, OwnerLease) or owner.role != "repair":
            raise ValueError("corrupt quarantine requires a repair owner")
        check_quarantine_names(task_id, reason)
        registry = self.ownership_registry()
        actor = current_actor_identity()
        with closing(registry.connect()) as coordinator_database:
            registry.require(coordinator_database, owner)
        with self.queue_owner(
            role="queue-operator",
            scope=f"task:{sha256_bytes(task_id.encode('utf-8'))}",
            parent=owner,
        ), self.task_fence(
            task_id, mode="queue-operator", owner=owner
        ) as task_fence, self._corrupt_intent_fence(
            task_id, owner=owner
        ) as (capture_binding, intent_fence):
            return self._quarantine_corrupt_owned(
                task_id,
                reason=reason,
                actor=actor,
                task_fence=task_fence,
                capture_binding=capture_binding,
                intent_fence=intent_fence,
            )

    def _dispose_corrupt_export(
        self,
        operation: sqlite3.Row,
        *,
        operation_id: str,
        task_id: str,
        disposition_key: str,
        package: Path,
        raw: bytes,
        history: bytes,
        metadata: bytes,
        actor: str,
        reason: str,
        task_fence: object,
        capture_binding: object,
        intent_fence: object,
    ) -> CorruptExportProgress:
        """Publish the manifest and disposition, then retire the task."""
        manifest = corrupt_manifest(
            operation,
            operation_id=operation_id,
            task_id=task_id,
            disposition_key=disposition_key,
        )
        write_durable_file(package / "manifest.json", canonical_json_bytes(manifest))
        if capture_binding is None or intent_fence is None:
            return _blocked_corrupt_progress(
                operation, task_id=task_id, operation_id=operation_id
            )
        sealed = self.seal_capture_binding(
            task_id,
            consumer_kind="corrupt-disposition",
            consumer_id=operation_id,
            active_link_digest=capture_binding.active_digest,
        )
        manifest_sha256 = sha256_bytes(
            read_stable_owner_file(package / "manifest.json", 64 * 1024)
        )
        disposed_at = str(operation["created_at"])
        disposition_record = corrupt_disposition_record(
            operation_id=operation_id,
            task_id=task_id,
            disposition_key=disposition_key,
            manifest_sha256=manifest_sha256,
            active_link_digest=sealed.active_digest,
            actor=actor,
            reason=reason,
            disposed_at=disposed_at,
        )
        disposition_bytes = canonical_json_bytes(disposition_record)
        write_durable_file(package / "disposition.json", disposition_bytes)
        verify_corrupt_package_bytes(
            package,
            raw=raw,
            history=history,
            metadata=metadata,
            disposition_bytes=disposition_bytes,
        )
        self._commit_corrupt_disposition(
            task_id=task_id,
            operation_id=operation_id,
            sealed=sealed,
            task_fence=task_fence,
            disposition_record=disposition_record,
            manifest_sha256=manifest_sha256,
            disposition_sha256=sha256_bytes(disposition_bytes),
            disposed_at=disposed_at,
        )
        return _quarantined_corrupt_progress(
            operation, task_id=task_id, operation_id=operation_id
        )

    def _commit_corrupt_disposition(
        self,
        *,
        task_id: str,
        operation_id: str,
        sealed: object,
        task_fence: object,
        disposition_record: Mapping[str, object],
        manifest_sha256: str,
        disposition_sha256: str,
        disposed_at: str,
    ) -> None:
        """Retire the task, once everything it depends on still holds."""
        with closing(self._connect()) as database, begin_immediate(database):
            self._require_disposable_corrupt_state(
                database,
                task_id=task_id,
                operation_id=operation_id,
                sealed=sealed,
                task_fence=task_fence,
            )
            self._insert_corrupt_disposition(
                database,
                task_id=task_id,
                operation_id=operation_id,
                package_path=str(disposition_record["package_path"]),
                manifest_sha256=manifest_sha256,
                disposition_sha256=disposition_sha256,
                active_link_digest=sealed.active_digest,
                disposed_at=disposed_at,
            )
            self._retire_quarantined_task(database, task_id, operation_id)

    @staticmethod
    def _require_disposable_corrupt_state(
        database: sqlite3.Connection,
        *,
        task_id: str,
        operation_id: str,
        sealed: object,
        task_fence: object,
    ) -> None:
        """The task, the operation, the fence and the seal all still agree."""
        current_task = database.execute(
            "SELECT * FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        current_operation = database.execute(
            "SELECT * FROM corrupt_export_operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        current_fence = database.execute(
            """SELECT 1 FROM task_fences WHERE task_id=? AND token=?
               AND fencing_epoch=? AND expires_at>?""",
            (task_id, task_fence.token, task_fence.epoch, timestamp(queue_store.utc_now())),
        ).fetchone()
        current_seal = database.execute(
            """SELECT 1 FROM capture_task_link_seals
               WHERE task_id=? AND active_digest=? AND seal_digest=?
                 AND consumer_kind='corrupt-disposition' AND consumer_id=?""",
            (task_id, sealed.active_digest, sealed.seal_digest, operation_id),
        ).fetchone()
        incoming_count = database.execute(
            "SELECT COUNT(*) FROM tasks WHERE redrive_of=?", (task_id,)
        ).fetchone()[0]
        if current_task is None or current_operation is None:
            raise QueueOperationError("corrupt_export_verification_failed")
        if current_fence is None or current_seal is None:
            raise QueueOperationError("corrupt_export_verification_failed")
        _require_disposable_rows(current_task, current_operation, incoming_count)

    @staticmethod
    def _retire_quarantined_task(
        database: sqlite3.Connection, task_id: str, operation_id: str
    ) -> None:
        """Both rows move together, or neither does."""
        changed = database.execute(
            """UPDATE tasks SET state='quarantined',updated_at=?
               WHERE id=? AND state='quarantine_pending'""",
            (timestamp(queue_store.utc_now()), task_id),
        ).rowcount
        advanced = database.execute(
            """UPDATE corrupt_export_operations
               SET state='disposed',updated_at=?
               WHERE operation_id=? AND state='manifested'""",
            (timestamp(queue_store.utc_now()), operation_id),
        ).rowcount
        if (changed, advanced) != (1, 1):
            raise QueueOperationError("corrupt_disposition_failed")

    def _bounded_lineage_links(
        self,
        candidates: list,
        operation: sqlite3.Row,
        operation_id: str,
    ) -> list[dict[str, object]]:
        """As many lineage links as one bounded page holds.

        The bounds are inputs — at most 1000 candidate rows and at most 1 MiB of
        page bytes — so a page rebuilt after a rollback is byte-identical and
        `write_durable_file` recognises its own work. See `docs/research/
        2026-09-18-a-page-that-is-rewritten-must-come-out-the-same.md`.
        """
        links: list[dict[str, object]] = []
        for child in candidates[:1000]:
            candidate = {
                "created_at": str(child["created_at"]),
                "input_hash": str(child["input_hash"]),
                "state": str(child["state"]),
                "task_id": str(child["id"]),
                "updated_at": str(child["updated_at"]),
            }
            prospective = canonical_json_bytes(
                {
                    "operation_id": operation_id,
                    "page_number": int(operation["page_count"]) + 1,
                    "previous_root": str(operation["rolling_root"]),
                    "links": [*links, candidate],
                }
            )
            if len(prospective) > 1024 * 1024:
                break
            links.append(candidate)
        return links

    def _publish_lineage_page(
        self, package: Path, page_number: int, page_bytes: bytes
    ) -> None:
        """Write the page and prove the bytes on disk are the bytes we meant."""
        page_path = package / f"lineage-page-{page_number:08d}.json"
        write_durable_file(page_path, page_bytes)
        if read_stable_owner_file(page_path, 1024 * 1024) != page_bytes:
            raise QueueOperationError("corrupt_page_verification_failed")

    def _write_lineage_page(
        self,
        database: sqlite3.Connection,
        operation: sqlite3.Row,
        operation_id: str,
        package: Path,
        links: list[dict[str, object]],
        candidates: list,
    ) -> None:
        page_number = int(operation["page_count"]) + 1
        page_bytes = canonical_json_bytes(
            {
                "operation_id": operation_id,
                "page_number": page_number,
                "previous_root": str(operation["rolling_root"]),
                "links": links,
            }
        )
        page_sha256 = sha256_bytes(page_bytes)
        rolling_root = sha256_bytes(
            bytes.fromhex(str(operation["rolling_root"]))
            + bytes.fromhex(page_sha256)
        )
        self._publish_lineage_page(package, page_number, page_bytes)
        inserted = database.execute(
            """INSERT INTO corrupt_export_pages(
                   operation_id,page_number,first_task_id,last_task_id,
                   link_count,page_sha256,rolling_root
               ) VALUES (?,?,?,?,?,?,?)""",
            (
                operation_id,
                page_number,
                links[0]["task_id"],
                links[-1]["task_id"],
                len(links),
                page_sha256,
                rolling_root,
            ),
        ).rowcount
        if inserted != 1:
            raise QueueOperationError("corrupt_page_insert_failed")
        state = "manifested" if len(candidates) <= len(links) else "exporting"
        updated = database.execute(
            """UPDATE corrupt_export_operations
               SET cursor_task_id=?,link_count=link_count+?,
                   page_count=page_count+1,rolling_root=?,state=?,updated_at=?
               WHERE operation_id=? AND state='exporting'
                 AND cursor_task_id=? AND page_count=?""",
            (
                links[-1]["task_id"],
                len(links),
                rolling_root,
                state,
                timestamp(queue_store.utc_now()),
                operation_id,
                operation["cursor_task_id"],
                operation["page_count"],
            ),
        ).rowcount
        if updated != 1:
            raise QueueOperationError("corrupt_page_fence_lost")

    def _finish_corrupt_export(
        self, database: sqlite3.Connection, operation_id: str
    ) -> None:
        updated = database.execute(
            """UPDATE corrupt_export_operations
               SET state='manifested',updated_at=?
               WHERE operation_id=? AND state='exporting'""",
            (timestamp(queue_store.utc_now()), operation_id),
        ).rowcount
        if updated != 1:
            raise QueueOperationError("corrupt_manifest_fence_lost")

    def _advance_corrupt_export(
        self,
        database: sqlite3.Connection,
        operation: sqlite3.Row,
        operation_id: str,
        task_id: str,
        package: Path,
    ) -> None:
        """One bounded step of the resumable lineage export."""
        candidates = database.execute(
            """SELECT id,state,created_at,updated_at,input_hash
               FROM tasks WHERE redrive_of=? AND id>?
               ORDER BY id LIMIT 1001""",
            (task_id, operation["cursor_task_id"]),
        ).fetchall()
        links = self._bounded_lineage_links(candidates, operation, operation_id)
        if links:
            self._write_lineage_page(
                database, operation, operation_id, package, links, candidates
            )
            return
        if not candidates:
            self._finish_corrupt_export(database, operation_id)
