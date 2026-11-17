"""Adopted-queue purge: selection, archive packages, receipts and restore."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from llm_wiki.queue_artifacts import (
    cleanup_export_staging,
    harden_owner_only,
    is_owner_only,
    read_stable_owner_file,
    remove_export_staging,
    write_durable_file,
)
from llm_wiki.queue_contracts import MAX_RESULT_BYTES
from llm_wiki.queue_evidence_records import (
    purge_receipt_record,
)
from llm_wiki.queue_export_proof import (
    MAX_EXPORT_METADATA_BYTES,
    corrupt_package_trustworthy,
)
from llm_wiki.queue_task_records import (
    ExportedRows,
    exported_task_record,
)
from llm_wiki.queue_v3_candidate import (
    ordinary_purge_authorization_digest,
)
from llm_wiki.queue_validation import (
    require_lower_sha256,
    require_repair_owner,
    require_task_identifier,
)

if TYPE_CHECKING:
    from operational_ownership import OwnerLease
import compile_progress
from llm_wiki import queue_store
from llm_wiki.formats.canonical import canonical_json_bytes, sha256_bytes
from llm_wiki.formats.queue_payload import (
    MAX_QUEUE_PAYLOAD_BYTES,
    validate_payload_blob,
)
from llm_wiki.formats.schema import validate_schema
from llm_wiki.queue_contracts import (
    CaptureTaskBinding,
    CorruptPurgeProgress,
    PurgeReceipt,
    QueueOperationError,
)
from llm_wiki.queue_store import TERMINAL_STATES, TaskFence, require_active
from llm_wiki.queue_timing import (
    as_utc,
    parse_timestamp,
    timestamp,
)
from llm_wiki.reliability_defaults import DEFAULTS
from llm_wiki.restricted_paths import restricted_relative_path
from llm_wiki.sqlite_transactions import begin_immediate
from reliable_memory import (
    fsync_directory,
    read_runtime_bytes,
)

SCHEMA_DIRECTORY = Path(__file__).resolve().parents[1] / "schemas"


def _ordinary_purge_operation_id(manifest_sha256: str) -> str:
    return f"ordinary-purge:{manifest_sha256}"


def capture_purge_archive_path(task_id: str, source_path: str) -> str:
    key = sha256_bytes(
        canonical_json_bytes({"source_path": source_path, "task_id": task_id})
    )
    return f"capture-artifacts/{key}.artifact"


def _cleanable_result_reference(
    record: Mapping[str, object], retained_terminals: set[str]
) -> str | None:
    """The result reference this record may drop, or None when it keeps it."""
    result = record["result"]
    if not isinstance(result, dict):
        return None
    reference = str(result["reference"])
    return None if reference in retained_terminals else reference


def _ordinary_purge_result_archive_path(task_id: str) -> str:
    value = f"results/{task_id}.result"
    try:
        restricted_relative_path(value, ("results",))
    except ValueError as exc:
        raise QueueOperationError("export_verification_failed") from exc
    return value


def _in_redrive_family(database: sqlite3.Connection, task_id: str) -> bool:
    """The task was redriven, or is itself a redrive."""
    row = database.execute(
        "SELECT 1 FROM tasks WHERE (id=? AND redrive_of IS NOT NULL) OR redrive_of=? LIMIT 1",
        (task_id, task_id),
    ).fetchone()
    return row is not None


@dataclass(frozen=True)
class CapturePurgeArtifact:
    source_path: str
    archive_path: str
    sha256: str


@dataclass(frozen=True)
class _CapturePurgeEvidence:
    task_id: str
    intent_id: str
    intent: CapturePurgeArtifact
    decisions: tuple[CapturePurgeArtifact, ...]
    terminal_path: str
    sources: tuple[CapturePurgeArtifact, ...] = ()


@dataclass(frozen=True)
class _OrdinaryPurgePlan:
    cutoff: str
    export: Path
    task_ids: tuple[str, ...]
    records: tuple[dict[str, object], ...]
    records_bytes: bytes
    capture_evidence: tuple[_CapturePurgeEvidence, ...]
    manifest_bytes: bytes | None
    retained: tuple[str, ...] = ()


def _purge_progress(
    task_id: str,
    operation_id: str,
    pages_written: int,
    links_deleted: int,
    *,
    state: str,
    code: str | None = None,
) -> CorruptPurgeProgress:
    return CorruptPurgeProgress(
        task_id=task_id,
        operation_id=operation_id,
        state=state,
        pages_written=pages_written,
        links_deleted=links_deleted,
        complete=False,
        code=code,
    )


def _blocked_or_pending(blocker_code: str | None) -> str:
    if blocker_code is None:
        return "purge_pending"
    return "blocked"


# Receipt field, and the disposition column it has to agree with.
_RECEIPT_DISPOSITION_FIELDS = (
    ("package_key", "disposition_key"),
    ("manifest_sha256", "manifest_sha256"),
    ("disposition_sha256", "disposition_sha256"),
    ("original_frozen_root", "original_frozen_root"),
)


def _receipt_matches_disposition(
    receipt: dict,
    disposition: sqlite3.Row,
    operation: sqlite3.Row,
    operation_id: str,
    task_id: str,
) -> bool:
    if (receipt["operation_id"], receipt["task_id"]) != (operation_id, task_id):
        return False
    if any(
        receipt[field] != disposition[column]
        for field, column in _RECEIPT_DISPOSITION_FIELDS
    ):
        return False
    return _receipt_matches_operation(receipt, operation)


def _receipt_matches_operation(receipt: dict, operation: sqlite3.Row) -> bool:
    if receipt["purge_page_count"] != operation["page_count"]:
        return False
    if receipt["final_rolling_root"] != operation["rolling_root"]:
        return False
    return receipt["final_generation"] == operation["expected_generation"]


def _parent_state_ready_for_delete(
    operation: sqlite3.Row, task: sqlite3.Row
) -> bool:
    if operation["state"] != "receipt-published":
        return False
    if task["state"] != "purge_pending":
        return False
    return int(task["lineage_generation"]) == int(operation["expected_generation"])


def _exported_bytes_match(
    raw: bytes,
    history: bytes,
    current_history: bytes,
    task: sqlite3.Row,
    disposition: sqlite3.Row,
) -> bool:
    return (
        sha256_bytes(raw) == disposition["raw_sha256"]
        and sha256_bytes(bytes(task["payload_blob"])) == disposition["raw_sha256"]
        and sha256_bytes(history) == disposition["history_sha256"]
        and sha256_bytes(current_history) == disposition["history_sha256"]
    )


def _require_matching_purge_operation(
    existing_operation: sqlite3.Row, task: sqlite3.Row, operation_id: str
) -> None:
    if existing_operation["operation_id"] != operation_id:
        raise QueueOperationError("corrupt_purge_conflict")
    if task["state"] != "purge_pending":
        raise QueueOperationError("corrupt_purge_conflict")


def _blocked_purge(task_id: str, code: str) -> CorruptPurgeProgress:
    return _purge_progress(task_id, "", 0, 0, state="blocked", code=code)


def _corrupt_child_leaf_blocker(
    queue: QueuePurgeOperations, database: sqlite3.Connection, child: sqlite3.Row, now: datetime
) -> str | None:
    """A child something else was redriven from is not a leaf."""
    row = database.execute(
        "SELECT 1 FROM tasks WHERE redrive_of=? LIMIT 1", (str(child["id"]),)
    ).fetchone()
    return "corrupt_child_not_leaf" if row is not None else None


def _corrupt_child_state_blocker(
    queue: QueuePurgeOperations, database: sqlite3.Connection, child: sqlite3.Row, now: datetime
) -> str | None:
    """A child still running, or one whose kind is kept forever."""
    if child["state"] not in TERMINAL_STATES:
        return "corrupt_child_nonterminal"
    if child["state"] == "dead" and DEFAULTS.dead_task_retention_days is None:
        return "corrupt_child_retained"
    return None


def _child_retention_days(child: sqlite3.Row) -> int | None:
    if child["state"] == "dead":
        return DEFAULTS.dead_task_retention_days
    return DEFAULTS.queue_result_retention_days


def _corrupt_child_retention_blocker(
    queue: QueuePurgeOperations, database: sqlite3.Connection, child: sqlite3.Row, now: datetime
) -> str | None:
    """A child whose retention window has not run out yet."""
    updated_at = parse_timestamp(str(child["updated_at"]))
    retention_days = _child_retention_days(child)
    if updated_at is None or retention_days is None:
        return "corrupt_child_retention_active"
    if updated_at + timedelta(days=retention_days) > now:
        return "corrupt_child_retention_active"
    return None


def _corrupt_child_fence_blocker(
    queue: QueuePurgeOperations, database: sqlite3.Connection, child: sqlite3.Row, now: datetime
) -> str | None:
    row = database.execute(
        "SELECT 1 FROM task_fences WHERE task_id=? LIMIT 1", (str(child["id"]),)
    ).fetchone()
    return "corrupt_child_fenced" if row is not None else None


def _active_binding_or_block(
    queue: QueuePurgeOperations, database: sqlite3.Connection, child_id: str
) -> tuple[object | None, str | None]:
    """The child's active capture binding, or why it cannot be read."""
    link_exists = (
        database.execute(
            "SELECT 1 FROM capture_task_links WHERE task_id=?", (child_id,)
        ).fetchone()
        is not None
    )
    try:
        return queue.active_capture_binding(database, child_id), None
    except QueueOperationError:
        if link_exists:
            return None, "corrupt_child_intent_unresolved"
        return None, None


def _corrupt_child_intent_blocker(
    queue: QueuePurgeOperations, database: sqlite3.Connection, child: sqlite3.Row, now: datetime
) -> str | None:
    """A child whose capture intent has no terminal record yet."""
    child_id = str(child["id"])
    binding, blocker = _active_binding_or_block(queue, database, child_id)
    if blocker is not None:
        return blocker
    return _unresolved_child_intent(queue, database, child_id, binding)


def _unresolved_child_intent(
    queue: QueuePurgeOperations, database: sqlite3.Connection, child_id: str, binding
) -> str | None:
    """Whether this child's capture intent is still waiting for a terminal record."""
    if binding is None or binding.intent_id is None:
        return None
    if queue._capture_terminal_blocker(database, child_id, binding) is None:
        return None
    return "corrupt_child_intent_unresolved"


def _corrupt_child_decision_blocker(
    queue: QueuePurgeOperations, database: sqlite3.Connection, child: sqlite3.Row, now: datetime
) -> str | None:
    row = database.execute(
        """SELECT 1 FROM capture_task_link_seals
           WHERE task_id=? AND consumer_kind='semantic-decision' LIMIT 1""",
        (str(child["id"]),),
    ).fetchone()
    return "corrupt_child_decision_retained" if row is not None else None


def _corrupt_child_result_blocker(
    queue: QueuePurgeOperations, database: sqlite3.Connection, child: sqlite3.Row, now: datetime
) -> str | None:
    fields = ("result_reference", "result_sha256", "result_operation_id")
    if any(child[field] is not None for field in fields):
        return "corrupt_child_result_retained"
    return None


_CORRUPT_CHILD_CHECKS = (
    _corrupt_child_leaf_blocker,
    _corrupt_child_state_blocker,
    _corrupt_child_retention_blocker,
    _corrupt_child_fence_blocker,
    _corrupt_child_intent_blocker,
    _corrupt_child_decision_blocker,
    _corrupt_child_result_blocker,
)


def _read_purge_receipt(package: Path) -> dict[str, object]:
    """The schema-valid purge receipt the package holds."""
    try:
        receipt_bytes = read_stable_owner_file(
            package / "purge-receipt.json", 64 * 1024
        )
        receipt = json.loads(receipt_bytes.decode("utf-8", errors="strict"))
        validate_schema(
            receipt,
            SCHEMA_DIRECTORY / "corrupt-purge-v1.json",
        )
    except (OSError, PermissionError, ValueError, json.JSONDecodeError) as exc:
        raise QueueOperationError("corrupt_purge_completion_invalid") from exc
    return receipt


def _require_matching_purge_receipt(
    receipt: Mapping[str, object],
    operation: sqlite3.Row,
    disposition: sqlite3.Row,
    task_id: str,
) -> None:
    """The receipt has to name exactly the purge the rows describe."""
    observed = (
        receipt["operation_id"],
        receipt["task_id"],
        receipt["package_key"],
        receipt["manifest_sha256"],
        receipt["disposition_sha256"],
        receipt["purge_page_count"],
        receipt["final_rolling_root"],
        receipt["final_generation"],
    )
    expected = (
        operation["operation_id"],
        task_id,
        disposition["disposition_key"],
        disposition["manifest_sha256"],
        disposition["disposition_sha256"],
        operation["page_count"],
        operation["rolling_root"],
        operation["expected_generation"],
    )
    if observed != expected:
        raise QueueOperationError("corrupt_purge_completion_invalid")


def _require_package_digests(package: Path, disposition: sqlite3.Row) -> None:
    """The manifest and disposition still hash to what the row recorded."""
    manifest_bytes = read_stable_owner_file(package / "manifest.json", 64 * 1024)
    disposition_bytes = read_stable_owner_file(
        package / "disposition.json", 64 * 1024
    )
    observed = (sha256_bytes(manifest_bytes), sha256_bytes(disposition_bytes))
    expected = (disposition["manifest_sha256"], disposition["disposition_sha256"])
    if observed != expected:
        raise QueueOperationError("corrupt_package_invalid")


def _write_purge_receipt(package: Path, receipt: Mapping[str, object]) -> bytes:
    """Write the receipt and read it back to prove it landed."""
    receipt_bytes = canonical_json_bytes(receipt)
    if len(receipt_bytes) > 64 * 1024:
        raise QueueOperationError("corrupt_purge_receipt_too_large")
    write_durable_file(package / "purge-receipt.json", receipt_bytes)
    written = read_stable_owner_file(package / "purge-receipt.json", 64 * 1024)
    if written != receipt_bytes:
        raise QueueOperationError("corrupt_purge_receipt_invalid")
    return receipt_bytes


_ORDINARY_PURGEABLE_STATES = frozenset({"succeeded", "cancelled", "dead"})


def _ordinary_purge_selection(include_dead: bool) -> str:
    """Which finished rows an ordinary purge takes; one query for both readers.

    Attempts-exhausted work is retained by default and leaves only when asked
    for with `--include-dead`, which the weekly pass does after 30 days; a dead
    capture whose capture has no terminal record stays even then
    (`_rows_with_releasable_evidence`). A row demoted to
    `dead / payload_hash_mismatch` is never taken here whatever the caller asks:
    its payload no longer hashes to its record, so it cannot be exported, and
    `quarantine-corrupt` is the route the product has for it. See
    `docs/research/2026-09-17-the-adopted-queue-settles-its-own-attempts-and-can-let-the-dead-go.md`.
    """
    if not include_dead:
        return "SELECT * FROM tasks WHERE state IN ('succeeded','cancelled') AND updated_at<?"
    return (
        "SELECT * FROM tasks WHERE (state IN ('succeeded','cancelled') "
        "OR (state='dead' AND error_code IS NOT 'payload_hash_mismatch')) "
        "AND updated_at<?"
    )


def _delete_ordinary_purge_authorizations(
    database: sqlite3.Connection, task_ids: Sequence[str], placeholders: str
) -> None:
    deleted = database.execute(
        f"""DELETE FROM task_purge_authorizations
            WHERE task_id IN ({placeholders})""",  # noqa: S608
        task_ids,
    ).rowcount
    if deleted != len(task_ids):
        raise QueueOperationError("purge_authorization_failed")


def _published_purge_operation(
    database: sqlite3.Connection, task_id: str
) -> sqlite3.Row | None:
    """The purge operation of a task that is gone; None while the task is still here."""
    task_exists = database.execute(
        "SELECT 1 FROM tasks WHERE id=?", (task_id,)
    ).fetchone()
    operation = database.execute(
        "SELECT * FROM corrupt_purge_operations WHERE task_id=?",
        (task_id,),
    ).fetchone()
    if task_exists is not None or operation is None:
        return None
    if operation["state"] != "receipt-published":
        raise QueueOperationError("corrupt_purge_completion_invalid")
    return operation


def _read_exported_rows(
    database: sqlite3.Connection, task_id: str
) -> ExportedRows:
    """The history, links and seal an export carries beside the task row."""
    history = database.execute(
        """SELECT attempt,started_at,finished_at,outcome,error_code
           FROM attempt_history WHERE task_id=? ORDER BY sequence""",
        (task_id,),
    ).fetchall()
    source_links = database.execute(
        """SELECT logical_path,source_digest FROM task_source_links
           WHERE task_id=? ORDER BY logical_path,source_digest""",
        (task_id,),
    ).fetchall()
    link = database.execute(
        "SELECT * FROM capture_task_links WHERE task_id=?", (task_id,)
    ).fetchone()
    seal = database.execute(
        "SELECT * FROM capture_task_link_seals WHERE task_id=?", (task_id,)
    ).fetchone()
    return ExportedRows(history, source_links, link, seal)


def _purge_operation_rows(
    database: sqlite3.Connection, task_id: str, operation_id: str
) -> tuple[sqlite3.Row, sqlite3.Row]:
    """The purge operation and its parent task, or a lost refusal."""
    operation = database.execute(
        "SELECT * FROM corrupt_purge_operations WHERE operation_id=?",
        (operation_id,),
    ).fetchone()
    parent = database.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if operation is None or parent is None:
        raise QueueOperationError("corrupt_purge_lost")
    return operation, parent


class QueuePurgeOperations:
    def _bounded_purge_children(
        self,
        database: sqlite3.Connection,
        candidates: list,
        operation: sqlite3.Row,
        operation_id: str,
        now: datetime,
    ) -> tuple[list[dict[str, object]], str | None]:
        """As many children as one bounded page holds, and what stopped it.

        Bounded by its inputs, never by a clock, so the page a retry rebuilds is
        the page already on disk. See `docs/research/
        2026-09-18-a-page-that-is-rewritten-must-come-out-the-same.md`.
        """
        children: list[dict[str, object]] = []
        for child in candidates[:1000]:
            blocker = self._corrupt_child_purge_blocker(database, child, now=now)
            if blocker is not None:
                return children, blocker
            descriptor = self._corrupt_purge_child_descriptor(database, child)
            prospective = canonical_json_bytes(
                {
                    "operation_id": operation_id,
                    "page_number": int(operation["page_count"]) + 1,
                    "previous_root": str(operation["rolling_root"]),
                    "before_generation": int(operation["expected_generation"]),
                    "after_generation": int(operation["expected_generation"])
                    + len(children)
                    + 1,
                    "children": [*children, descriptor],
                }
            )
            if len(prospective) > 1024 * 1024:
                return children, None
            children.append(descriptor)
        return children, None

    def _publish_purge_page(self, page_path: Path, page_bytes: bytes) -> bool:
        """False when another owner already wrote a different page here."""
        try:
            write_durable_file(page_path, page_bytes)
        except QueueOperationError as exc:
            if exc.code != "durable_file_conflict":
                raise
            return False
        try:
            observed_page = read_stable_owner_file(page_path, 1024 * 1024)
        except (OSError, PermissionError, ValueError) as exc:
            raise QueueOperationError("corrupt_purge_page_invalid") from exc
        if observed_page != page_bytes:
            raise QueueOperationError("corrupt_purge_page_invalid")
        return True

    def _authorize_purge(
        self,
        database: sqlite3.Connection,
        task_id: str,
        operation: sqlite3.Row,
        operation_id: str,
        now: datetime,
    ) -> None:
        inserted = database.execute(
            """INSERT INTO task_purge_authorizations(
                   task_id,mode,operation_id,authorization_digest,created_at
               ) VALUES (?,'corrupt-lineage',?,?,?)""",
            (task_id, operation_id, operation["purge_token"], timestamp(now)),
        ).rowcount
        if inserted != 1:
            raise QueueOperationError("purge_authorization_failed")

    def _clear_purge_authorization(
        self, database: sqlite3.Connection, task_id: str
    ) -> None:
        cleared = database.execute(
            "DELETE FROM task_purge_authorizations WHERE task_id=?",
            (task_id,),
        ).rowcount
        if cleared != 1:
            raise QueueOperationError("purge_authorization_failed")

    def _delete_purged_child(
        self,
        database: sqlite3.Connection,
        child_id: str,
        task_id: str,
        operation: sqlite3.Row,
        operation_id: str,
        now: datetime,
    ) -> None:
        self._authorize_purge(database, child_id, operation, operation_id, now)
        self._delete_task_owned_evidence(database, child_id)
        deleted = database.execute(
            "DELETE FROM tasks WHERE id=? AND redrive_of=?",
            (child_id, task_id),
        ).rowcount
        if deleted != 1:
            raise QueueOperationError("corrupt_child_delete_failed")
        self._clear_purge_authorization(database, child_id)

    def _require_parent_generation(
        self, database: sqlite3.Connection, task_id: str, after_generation: int
    ) -> None:
        parent_generation = database.execute(
            "SELECT lineage_generation FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        if parent_generation is None:
            raise QueueOperationError("corrupt_lineage_generation_changed")
        if int(parent_generation[0]) != after_generation:
            raise QueueOperationError("corrupt_lineage_generation_changed")

    def _advance_purge_operation(
        self,
        database: sqlite3.Connection,
        operation: sqlite3.Row,
        operation_id: str,
        children: list[dict[str, object]],
        before_generation: int,
        after_generation: int,
        rolling_root: str,
    ) -> None:
        updated = database.execute(
            """UPDATE corrupt_purge_operations
               SET expected_generation=?,cursor_task_id=?,page_count=page_count+1,
                   rolling_root=?,updated_at=?
               WHERE operation_id=? AND state='purging'
                 AND expected_generation=? AND cursor_task_id=? AND page_count=?""",
            (
                after_generation,
                children[-1]["task_id"],
                rolling_root,
                timestamp(queue_store.utc_now()),
                operation_id,
                before_generation,
                operation["cursor_task_id"],
                operation["page_count"],
            ),
        ).rowcount
        if updated != 1:
            raise QueueOperationError("corrupt_purge_page_fence_lost")

    def _commit_purge_page(
        self,
        database: sqlite3.Connection,
        task_id: str,
        operation: sqlite3.Row,
        operation_id: str,
        children: list[dict[str, object]],
        package: Path,
        now: datetime,
    ) -> tuple[int, str, bool]:
        """(page number, rolling root, published) for one bounded purge page."""
        page_number = int(operation["page_count"]) + 1
        before_generation = int(operation["expected_generation"])
        after_generation = before_generation + len(children)
        page_bytes = canonical_json_bytes(
            {
                "operation_id": operation_id,
                "page_number": page_number,
                "previous_root": str(operation["rolling_root"]),
                "before_generation": before_generation,
                "after_generation": after_generation,
                "children": children,
            }
        )
        if len(page_bytes) > 1024 * 1024:
            raise QueueOperationError("corrupt_purge_page_too_large")
        page_sha256 = sha256_bytes(page_bytes)
        rolling_root = sha256_bytes(
            bytes.fromhex(str(operation["rolling_root"]))
            + bytes.fromhex(page_sha256)
        )
        page_path = package / f"purge-page-{page_number:08d}.json"
        if not self._publish_purge_page(page_path, page_bytes):
            return page_number, rolling_root, False
        self._insert_corrupt_purge_page(
            database,
            operation_id=operation_id,
            page_number=page_number,
            first_task_id=str(children[0]["task_id"]),
            last_task_id=str(children[-1]["task_id"]),
            deleted_link_count=len(children),
            page_sha256=page_sha256,
            rolling_root=rolling_root,
            expected_generation=after_generation,
        )
        self._authorize_purge(database, task_id, operation, operation_id, now)
        for child in children:
            self._delete_purged_child(
                database, str(child["task_id"]), task_id, operation, operation_id, now
            )
        self._clear_purge_authorization(database, task_id)
        self._require_parent_generation(database, task_id, after_generation)
        self._advance_purge_operation(
            database,
            operation,
            operation_id,
            children,
            before_generation,
            after_generation,
            rolling_root,
        )
        return page_number, rolling_root, True

    def _purge_page_preconditions(
        self,
        database: sqlite3.Connection,
        task_id: str,
        operation_id: str,
        task_fence: TaskFence,
    ) -> tuple[sqlite3.Row, int, CorruptPurgeProgress | None]:
        """The operation row and prior link count, or the progress to return."""
        operation, parent = _purge_operation_rows(database, task_id, operation_id)
        prior_links_deleted = int(
            database.execute(
                """SELECT COALESCE(SUM(deleted_link_count),0)
                   FROM corrupt_purge_pages WHERE operation_id=?""",
                (operation_id,),
            ).fetchone()[0]
        )
        pages = int(operation["page_count"])
        if operation["state"] != "purging" or parent["state"] != "purge_pending":
            return (
                operation,
                prior_links_deleted,
                _purge_progress(
                    task_id,
                    operation_id,
                    pages,
                    prior_links_deleted,
                    state="purge_pending",
                ),
            )
        self._require_corrupt_task_fence(database, task_fence)
        if int(parent["lineage_generation"]) != int(operation["expected_generation"]):
            return (
                operation,
                prior_links_deleted,
                _purge_progress(
                    task_id,
                    operation_id,
                    pages,
                    prior_links_deleted,
                    state="blocked",
                    code="corrupt_lineage_generation_changed",
                ),
            )
        return operation, prior_links_deleted, None

    def _purge_corrupt_lineage_page(
        self,
        task_id: str,
        *,
        operation_id: str,
        package: Path,
        owner: OwnerLease,
        task_fence: TaskFence,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> CorruptPurgeProgress:
        del owner
        require_active(deadline, cancelled)
        with closing(self._connect()) as database, begin_immediate(database):
            operation, prior, early = self._purge_page_preconditions(
                database, task_id, operation_id, task_fence
            )
            if early is not None:
                return early
            return self._purge_one_lineage_page(
                database,
                task_id,
                operation_id=operation_id,
                operation=operation,
                prior=prior,
                package=package,
            )

    def _purge_one_lineage_page(
        self,
        database: sqlite3.Connection,
        task_id: str,
        *,
        operation_id: str,
        operation: sqlite3.Row,
        prior: int,
        package: Path,
    ) -> CorruptPurgeProgress:
        """One bounded page of children, inside the caller's transaction."""
        pages = int(operation["page_count"])
        candidates = database.execute(
            """SELECT * FROM tasks WHERE redrive_of=? AND id>?
               ORDER BY id LIMIT 1001""",
            (task_id, operation["cursor_task_id"]),
        ).fetchall()
        if not candidates:
            return _purge_progress(
                task_id, operation_id, pages, prior, state="purge_pending"
            )
        now = queue_store.utc_now()
        children, blocker_code = self._bounded_purge_children(
            database, candidates, operation, operation_id, now
        )
        if not children:
            return _purge_progress(
                task_id,
                operation_id,
                pages,
                prior,
                state=_blocked_or_pending(blocker_code),
                code=blocker_code,
            )
        return self._commit_purged_children(
            database,
            task_id,
            operation_id=operation_id,
            operation=operation,
            children=children,
            package=package,
            now=now,
            pages=pages,
            prior=prior,
            blocker_code=blocker_code,
        )

    def _commit_purged_children(
        self,
        database: sqlite3.Connection,
        task_id: str,
        *,
        operation_id: str,
        operation: sqlite3.Row,
        children: list,
        package: Path,
        now: datetime,
        pages: int,
        prior: int,
        blocker_code: str | None,
    ) -> CorruptPurgeProgress:
        """Publish this page of children, or report the fence that took it."""
        page_number, _root, published = self._commit_purge_page(
            database, task_id, operation, operation_id, children, package, now
        )
        if not published:
            return _purge_progress(
                task_id,
                operation_id,
                pages,
                prior,
                state="blocked",
                code="orphan_corrupt_purge_page_conflict",
            )
        return _purge_progress(
            task_id,
            operation_id,
            page_number,
            prior + len(children),
            state=_blocked_or_pending(blocker_code),
            code=blocker_code,
        )

    @staticmethod
    def _insert_corrupt_purge_page(
        database: sqlite3.Connection,
        *,
        operation_id: str,
        page_number: int,
        first_task_id: str,
        last_task_id: str,
        deleted_link_count: int,
        page_sha256: str,
        rolling_root: str,
        expected_generation: int,
    ) -> None:
        inserted = database.execute(
            """INSERT INTO corrupt_purge_pages(
                   operation_id,page_number,first_task_id,last_task_id,
                   deleted_link_count,page_sha256,rolling_root,expected_generation
               ) VALUES (?,?,?,?,?,?,?,?)""",
            (
                operation_id,
                page_number,
                first_task_id,
                last_task_id,
                deleted_link_count,
                page_sha256,
                rolling_root,
                expected_generation,
            ),
        ).rowcount
        if inserted != 1:
            raise QueueOperationError("corrupt_purge_page_insert_failed")

    def _corrupt_child_purge_blocker(
        self,
        database: sqlite3.Connection,
        child: sqlite3.Row,
        *,
        now: datetime,
    ) -> str | None:
        """Why this child of a corrupt export cannot be purged yet."""
        for check in _CORRUPT_CHILD_CHECKS:
            code = check(self, database, child, now)
            if code is not None:
                return code
        return None

    def _corrupt_purge_child_descriptor(
        self, database: sqlite3.Connection, child: sqlite3.Row
    ) -> dict[str, object]:
        task_id = str(child["id"])
        history = canonical_json_bytes(
            [
                {
                    "attempt": int(row["attempt"]),
                    "started_at": str(row["started_at"]),
                    "finished_at": str(row["finished_at"]),
                    "outcome": str(row["outcome"]),
                    "error_code": row["error_code"],
                }
                for row in database.execute(
                    """SELECT attempt,started_at,finished_at,outcome,error_code
                       FROM attempt_history WHERE task_id=? ORDER BY sequence""",
                    (task_id,),
                )
            ]
        )
        task_evidence = canonical_json_bytes(
            {
                "task_id": task_id,
                "state": str(child["state"]),
                "input_hash": str(child["input_hash"]),
                "updated_at": str(child["updated_at"]),
                "lineage_generation": int(child["lineage_generation"]),
            }
        )
        return {
            "task_id": task_id,
            "task_sha256": sha256_bytes(task_evidence),
            "history_sha256": sha256_bytes(history),
        }

    @staticmethod
    def _delete_task_owned_evidence(
        database: sqlite3.Connection, task_id: str
    ) -> None:
        for table in (
            "capture_task_link_seals",
            "capture_task_link_resolutions",
            "capture_task_links",
            "attempt_history",
            "task_source_links",
        ):
            database.execute(f'DELETE FROM "{table}" WHERE task_id=?', (task_id,))

    @contextmanager
    def _corrupt_purge_task_fence(
        self, task_id: str, *, owner: OwnerLease
    ) -> Iterator[TaskFence]:
        fence = self.acquire_task_fence(task_id, mode="queue-operator", owner=owner)
        try:
            yield fence
        finally:
            with closing(self._connect()) as database:
                task = database.execute(
                    "SELECT 1 FROM tasks WHERE id=?", (task_id,)
                ).fetchone()
                current = database.execute(
                    """SELECT 1 FROM task_fences WHERE task_id=? AND token=?
                       AND fencing_epoch=?""",
                    (task_id, fence.token, fence.epoch),
                ).fetchone()
            self._release_corrupt_purge_fence(fence, current, task)

    def _release_corrupt_purge_fence(self, fence, current, task) -> None:
        if current is not None:
            self.release_task_fence(fence)
            return
        if task is not None:
            raise QueueOperationError("task_fence_lost")

    def _capture_terminal_file(
        self, intent_id: str
    ) -> tuple[dict[str, object], bytes]:
        path = self.results_dir / f"capture-{intent_id}.json"
        try:
            raw = read_stable_owner_file(path, 64 * 1024)
            value = json.loads(raw.decode("utf-8", errors="strict"))
        except (OSError, PermissionError, ValueError, json.JSONDecodeError) as exc:
            raise QueueOperationError("capture_intent_unresolved") from exc
        if not isinstance(value, dict):
            raise QueueOperationError("capture_terminal_invalid")
        return value, raw

    def _require_capture_terminal_identity(
        self, record: Mapping[str, object], task_id: str, binding: CaptureTaskBinding
    ) -> CaptureTaskBinding:
        disposition = record.get("disposition")
        actual = (
            set(record),
            record.get("schema_version"),
            record.get("intent_id"),
            record.get("intent_sha256"),
            isinstance(record.get("semantic_decisions"), list),
        )
        expected = (
            {
                "schema_version",
                "intent_id",
                "intent_sha256",
                "semantic_decisions",
                "processing_binding",
                "disposition",
            },
            "capture-terminal/v1",
            binding.intent_id,
            binding.intent_sha256,
            True,
        )
        allowed = {"markdown_committed", "no_durable_content", "operator_discard"}
        if actual != expected or task_id != binding.task_id:
            raise QueueOperationError("capture_terminal_invalid")
        if not isinstance(disposition, dict) or disposition.get("kind") not in allowed:
            raise QueueOperationError("capture_terminal_invalid")
        return self._terminal_proof_binding(record, binding)

    @staticmethod
    def _require_capture_terminal_database_binding(
        database: sqlite3.Connection,
        task_id: str,
        binding: CaptureTaskBinding,
        raw: bytes,
    ) -> None:
        intent = database.execute(
            "SELECT intent_sha256 FROM capture_intents WHERE intent_id=?",
            (binding.intent_id,),
        ).fetchone()
        if intent is None or intent["intent_sha256"] != binding.intent_sha256:
            raise QueueOperationError("capture_terminal_invalid")
        task = database.execute(
            """SELECT result_reference,result_sha256,result_operation_id
               FROM tasks WHERE id=?""",
            (task_id,),
        ).fetchone()
        expected = (
            f"run/queue-results/capture-{binding.intent_id}.json",
            sha256_bytes(raw),
            f"capture-terminal:{binding.intent_id}",
        )
        if task is None or tuple(task) != expected:
            raise QueueOperationError("capture_terminal_unbound")

    def _require_capture_terminal_proof(
        self,
        database: sqlite3.Connection,
        task_id: str,
        binding: CaptureTaskBinding,
    ) -> dict[str, object]:
        if binding.intent_id is None or binding.intent_sha256 is None:
            raise QueueOperationError("capture_intent_unresolved")
        record, raw = self._capture_terminal_file(binding.intent_id)
        proven = self._require_capture_terminal_identity(record, task_id, binding)
        self._require_capture_terminal_database_binding(
            database, task_id, binding, raw
        )
        self._require_versioned_capture_proof(proven, record)
        return record

    def _capture_terminal_blocker(
        self,
        database: sqlite3.Connection,
        task_id: str,
        binding: CaptureTaskBinding,
    ) -> str | None:
        try:
            self._require_capture_terminal_proof(database, task_id, binding)
        except QueueOperationError as exc:
            return exc.code
        return None

    @staticmethod
    def _require_capture_purge_unshared(
        database: sqlite3.Connection, task_id: str, intent_id: str
    ) -> None:
        other = database.execute(
            """SELECT task_id FROM capture_task_links
               WHERE intent_id=? AND task_id<>?
               UNION ALL
               SELECT task_id FROM capture_task_link_resolutions
               WHERE selected_intent_id=? AND task_id<>?
               LIMIT 1""",
            (intent_id, task_id, intent_id, task_id),
        ).fetchone()
        if other is not None:
            raise QueueOperationError("capture_link_conflicted")

    def _capture_purge_artifact(
        self,
        task_id: str,
        source_path: str,
        expected_sha256: str,
        *,
        max_bytes: int,
        error_code: str,
    ) -> CapturePurgeArtifact:
        try:
            data = read_runtime_bytes(
                self.state_root / source_path,
                self.state_root,
                max_bytes=max_bytes,
                owner_only=True,
            )
        except (OSError, PermissionError, ValueError) as exc:
            raise QueueOperationError(error_code) from exc
        if sha256_bytes(data) != expected_sha256:
            raise QueueOperationError(error_code)
        archive_path = capture_purge_archive_path(task_id, source_path)
        return CapturePurgeArtifact(source_path, archive_path, expected_sha256)

    def _capture_purge_intent_path(
        self, database: sqlite3.Connection, binding: CaptureTaskBinding
    ) -> CapturePurgeArtifact:
        row = database.execute(
            "SELECT * FROM capture_intents WHERE intent_id=?", (binding.intent_id,)
        ).fetchone()
        if row is None:
            raise QueueOperationError("capture_intent_conflict")
        relative = str(row["relative_path"])
        artifact = self._capture_purge_artifact(
            binding.task_id,
            relative,
            str(binding.intent_sha256),
            max_bytes=MAX_QUEUE_PAYLOAD_BYTES,
            error_code="capture_intent_conflict",
        )
        size = (self.state_root / relative).stat().st_size
        if (size, row["publication_state"]) != (row["byte_size"], "ready"):
            raise QueueOperationError("capture_intent_conflict")
        return artifact

    def _require_capture_purge_decision_file(
        self, task_id: str, row: sqlite3.Row
    ) -> CapturePurgeArtifact:
        return self._capture_purge_artifact(
            task_id,
            str(row["decision_path"]),
            str(row["decision_sha256"]),
            max_bytes=1024 * 1024,
            error_code="semantic_decision_conflict",
        )

    @staticmethod
    def _capture_purge_decisions_match(
        rows: list[sqlite3.Row],
        binding: CaptureTaskBinding,
        record: Mapping[str, object],
    ) -> bool:
        indexed = [
            {
                "stage": str(row["stage"]),
                "decision_path": str(row["decision_path"]),
                "decision_sha256": str(row["decision_sha256"]),
            }
            for row in rows
        ]
        digests_match = all(
            row["active_link_digest"] == binding.active_digest for row in rows
        )
        return indexed == record.get("semantic_decisions") and digests_match

    def _capture_purge_decision_paths(
        self,
        database: sqlite3.Connection,
        binding: CaptureTaskBinding,
        record: Mapping[str, object],
    ) -> tuple[CapturePurgeArtifact, ...]:
        rows = database.execute(
            """SELECT stage,decision_path,decision_sha256,active_link_digest
               FROM semantic_decisions WHERE intent_id=? ORDER BY stage""",
            (binding.intent_id,),
        ).fetchall()
        if not self._capture_purge_decisions_match(rows, binding, record):
            raise QueueOperationError("capture_terminal_invalid")
        return tuple(
            self._require_capture_purge_decision_file(binding.task_id, row)
            for row in rows
        )

    def _capture_purge_evidence(
        self,
        database: sqlite3.Connection,
        task_id: str,
        binding: CaptureTaskBinding,
    ) -> _CapturePurgeEvidence:
        record = self._require_capture_terminal_proof(database, task_id, binding)
        intent_id = str(binding.intent_id)
        self._require_capture_purge_unshared(database, task_id, intent_id)
        intent = self._capture_purge_intent_path(database, binding)
        decisions = self._capture_purge_decision_paths(
            database, binding, record
        )
        return _CapturePurgeEvidence(
            task_id,
            intent_id,
            intent,
            decisions,
            f"run/queue-results/capture-{intent_id}.json",
            self._capture_purge_source_paths(binding),
        )

    def _capture_purge_source_paths(self, binding: CaptureTaskBinding) -> tuple[CapturePurgeArtifact, ...]:
        if binding.handler_version == 1:
            return ()
        from breadcrumb_bundle import (
            load_bundle,
            runtime_source_records,
            stored_manifest,
        )

        manifest = stored_manifest(self.state_root, binding.intent_id)
        bundle = load_bundle(self.state_root, manifest)
        return tuple(
            self._capture_purge_artifact(
                binding.task_id, relative, sha256_bytes(data),
                max_bytes=MAX_QUEUE_PAYLOAD_BYTES, error_code="breadcrumb_source_changed",
            )
            for relative, data in runtime_source_records(self.state_root, bundle)
        )

    @staticmethod
    def _require_corrupt_task_fence(
        database: sqlite3.Connection, task_fence: TaskFence
    ) -> None:
        row = database.execute(
            """SELECT 1 FROM task_fences WHERE task_id=? AND token=?
               AND fencing_epoch=? AND canonical_owner_token=?
               AND canonical_fencing_epoch=? AND expires_at>?""",
            (
                task_fence.task_id,
                task_fence.token,
                task_fence.epoch,
                task_fence.owner.token,
                task_fence.owner.epoch,
                timestamp(queue_store.utc_now()),
            ),
        ).fetchone()
        if row is None:
            raise QueueOperationError("task_fence_lost")

    def _publish_corrupt_purge_receipt(
        self,
        task_id: str,
        *,
        operation_id: str,
        package: Path,
        task_fence: TaskFence,
    ) -> bytes:
        with closing(self._connect()) as database, begin_immediate(database):
            operation = database.execute(
                "SELECT * FROM corrupt_purge_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            task = database.execute(
                "SELECT * FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            disposition = database.execute(
                """SELECT disposition.*,export.disposition_key,
                          export.rolling_root AS original_frozen_root
                   FROM corrupt_dispositions AS disposition
                   JOIN corrupt_export_operations AS export
                     ON export.operation_id=disposition.operation_id
                   WHERE disposition.task_id=?""",
                (task_id,),
            ).fetchone()
            if operation is None or task is None or disposition is None:
                raise QueueOperationError("corrupt_purge_lost")
            self._require_corrupt_task_fence(database, task_fence)
            self._require_receipt_preconditions(database, task_id, task, operation)
            _require_package_digests(package, disposition)
            receipt_bytes = _write_purge_receipt(
                package,
                purge_receipt_record(
                    operation, disposition, operation_id=operation_id, task_id=task_id
                ),
            )
            self._advance_purge_receipt_state(database, operation, operation_id)
            return receipt_bytes

    def _require_receipt_preconditions(
        self,
        database: sqlite3.Connection,
        task_id: str,
        task: sqlite3.Row,
        operation: sqlite3.Row,
    ) -> None:
        """The task is pending purge, on the expected generation, with no children."""
        incoming_count = int(
            database.execute(
                "SELECT COUNT(*) FROM tasks WHERE redrive_of=?", (task_id,)
            ).fetchone()[0]
        )
        observed = (
            task["state"],
            int(task["lineage_generation"]),
            incoming_count,
        )
        if observed != ("purge_pending", int(operation["expected_generation"]), 0):
            raise QueueOperationError("corrupt_purge_receipt_precondition_failed")

    @staticmethod
    def _advance_purge_receipt_state(
        database: sqlite3.Connection, operation: sqlite3.Row, operation_id: str
    ) -> None:
        """Move the operation to receipt-published, under its own fence."""
        if operation["state"] != "purging":
            return
        updated = database.execute(
            """UPDATE corrupt_purge_operations
               SET state='receipt-published',updated_at=?
               WHERE operation_id=? AND state='purging'
                 AND expected_generation=? AND page_count=? AND rolling_root=?""",
            (
                timestamp(queue_store.utc_now()),
                operation_id,
                operation["expected_generation"],
                operation["page_count"],
                operation["rolling_root"],
            ),
        ).rowcount
        if updated != 1:
            raise QueueOperationError("corrupt_purge_receipt_fence_lost")

    def _corrupt_parent_rows(
        self, database: sqlite3.Connection, task_id: str, operation_id: str
    ) -> tuple[sqlite3.Row, sqlite3.Row, sqlite3.Row]:
        operation = database.execute(
            "SELECT * FROM corrupt_purge_operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        task = database.execute(
            "SELECT * FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        disposition = database.execute(
            """SELECT disposition.*,export.raw_sha256,export.history_sha256,
                      export.metadata_sha256,export.disposition_key,
                      export.rolling_root AS original_frozen_root
               FROM corrupt_dispositions AS disposition
               JOIN corrupt_export_operations AS export
                 ON export.operation_id=disposition.operation_id
               WHERE disposition.task_id=?""",
            (task_id,),
        ).fetchone()
        if operation is None or task is None or disposition is None:
            raise QueueOperationError("corrupt_purge_lost")
        return operation, task, disposition

    def _current_attempt_history(
        self, database: sqlite3.Connection, task_id: str
    ) -> bytes:
        return canonical_json_bytes(
            [
                {
                    "attempt": int(row["attempt"]),
                    "started_at": str(row["started_at"]),
                    "finished_at": str(row["finished_at"]),
                    "outcome": str(row["outcome"]),
                    "error_code": row["error_code"],
                }
                for row in database.execute(
                    """SELECT attempt,started_at,finished_at,outcome,error_code
                       FROM attempt_history WHERE task_id=? ORDER BY sequence""",
                    (task_id,),
                )
            ]
        )

    def _require_parent_delete_preconditions(
        self,
        database: sqlite3.Connection,
        task_id: str,
        operation_id: str,
        operation: sqlite3.Row,
        task: sqlite3.Row,
        disposition: sqlite3.Row,
        package: Path,
    ) -> None:
        """Nothing is deleted until the package still proves what was exported."""
        receipt_bytes = read_stable_owner_file(
            package / "purge-receipt.json", 64 * 1024
        )
        receipt = json.loads(receipt_bytes.decode("utf-8", errors="strict"))
        validate_schema(
            receipt, SCHEMA_DIRECTORY / "corrupt-purge-v1.json"
        )
        raw = read_stable_owner_file(
            package / "payload.bin", MAX_QUEUE_PAYLOAD_BYTES
        )
        history = read_stable_owner_file(
            package / "attempt-history.json", MAX_EXPORT_METADATA_BYTES
        )
        current_history = self._current_attempt_history(database, task_id)
        children = database.execute(
            "SELECT 1 FROM tasks WHERE redrive_of=? LIMIT 1", (task_id,)
        ).fetchone()
        if (
            not _parent_state_ready_for_delete(operation, task)
            or children is not None
            or not _exported_bytes_match(
                raw, history, current_history, task, disposition
            )
            or not _receipt_matches_disposition(
                receipt, disposition, operation, operation_id, task_id
            )
        ):
            raise QueueOperationError("corrupt_parent_delete_precondition_failed")

    def _delete_corrupt_parent(
        self,
        task_id: str,
        *,
        operation_id: str,
        package: Path,
        task_fence: TaskFence,
    ) -> None:
        with closing(self._connect()) as database, begin_immediate(database):
            operation, task, disposition = self._corrupt_parent_rows(
                database, task_id, operation_id
            )
            self._require_corrupt_task_fence(database, task_fence)
            self._require_parent_delete_preconditions(
                database,
                task_id,
                operation_id,
                operation,
                task,
                disposition,
                package,
            )
            self._authorize_corrupt_parent_purge(
                database, task_id, operation_id, operation
            )
            self._delete_corrupt_parent_rows(database, task_id, task_fence)
            self._clear_purge_authorization(database, task_id)

    def _authorize_corrupt_parent_purge(
        self,
        database: sqlite3.Connection,
        task_id: str,
        operation_id: str,
        operation: sqlite3.Row,
    ) -> None:
        inserted = database.execute(
            """INSERT INTO task_purge_authorizations(
                   task_id,mode,operation_id,authorization_digest,created_at
               ) VALUES (?,'corrupt-parent',?,?,?)""",
            (
                task_id,
                operation_id,
                operation["purge_token"],
                timestamp(queue_store.utc_now()),
            ),
        ).rowcount
        if inserted != 1:
            raise QueueOperationError("purge_authorization_failed")

    def _delete_corrupt_parent_rows(
        self, database: sqlite3.Connection, task_id: str, task_fence: TaskFence
    ) -> None:
        """Take the fence, then the evidence, then the task -- in that order."""
        deleted_fence = database.execute(
            """DELETE FROM task_fences WHERE task_id=? AND token=?
               AND fencing_epoch=?""",
            (task_id, task_fence.token, task_fence.epoch),
        ).rowcount
        if deleted_fence != 1:
            raise QueueOperationError("task_fence_lost")
        self._delete_task_owned_evidence(database, task_id)
        deleted_task = database.execute(
            "DELETE FROM tasks WHERE id=?", (task_id,)
        ).rowcount
        if deleted_task != 1:
            raise QueueOperationError("corrupt_parent_delete_failed")

    def _corrupt_purge_rows(
        self, database: sqlite3.Connection, task_id: str
    ) -> tuple[sqlite3.Row, sqlite3.Row, sqlite3.Row | None]:
        task = database.execute(
            "SELECT * FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        disposition = database.execute(
            """SELECT disposition.*, operation.disposition_key,
                      operation.rolling_root AS original_frozen_root,
                      operation.lineage_generation
               FROM corrupt_dispositions AS disposition
               JOIN corrupt_export_operations AS operation
                 ON operation.operation_id=disposition.operation_id
               WHERE disposition.task_id=?""",
            (task_id,),
        ).fetchone()
        if task is None or disposition is None:
            raise QueueOperationError("corrupt_purge_state_invalid")
        if task["state"] not in {"quarantined", "purge_pending"}:
            raise QueueOperationError("corrupt_purge_state_invalid")
        existing_operation = database.execute(
            "SELECT * FROM corrupt_purge_operations WHERE task_id=?",
            (task_id,),
        ).fetchone()
        return task, disposition, existing_operation

    def _retention_blocker(
        self,
        disposition: sqlite3.Row,
        existing_operation: sqlite3.Row | None,
        now: datetime,
    ) -> str | None:
        """A disposed task is retained for thirty days before it may be purged."""
        disposed_at = parse_timestamp(str(disposition["disposed_at"]))
        if disposed_at is None:
            raise QueueOperationError("corrupt_disposition_invalid")
        if existing_operation is None and disposed_at + timedelta(days=30) > now:
            return "corrupt_retention_active"
        return None

    def _start_corrupt_purge(
        self,
        database: sqlite3.Connection,
        task_id: str,
        task: sqlite3.Row,
        operation_id: str,
        task_fence: TaskFence,
        now: datetime,
    ) -> str | None:
        """None once the operation exists, or the code that blocks starting it."""
        try:
            binding = self.active_capture_binding(database, task_id)
        except QueueOperationError:
            return "capture_intent_unresolved"
        terminal_code = self._capture_terminal_blocker(database, task_id, binding)
        if terminal_code is not None:
            return terminal_code
        purge_token = sha256_bytes(
            canonical_json_bytes(
                {
                    "operation_id": operation_id,
                    "task_id": task_id,
                    "task_fence_epoch": task_fence.epoch,
                }
            )
        )
        inserted = database.execute(
            """INSERT INTO corrupt_purge_operations(
                   operation_id,task_id,purge_token,expected_generation,
                   cursor_task_id,page_count,rolling_root,state,created_at,updated_at
               ) VALUES (?,?,?,?,'',0,?,'purging',?,?)""",
            (
                operation_id,
                task_id,
                purge_token,
                int(task["lineage_generation"]),
                sha256_bytes(b""),
                timestamp(now),
                timestamp(now),
            ),
        ).rowcount
        changed = database.execute(
            """UPDATE tasks SET state='purge_pending',updated_at=?
               WHERE id=? AND state='quarantined'""",
            (timestamp(now), task_id),
        ).rowcount
        if (inserted, changed) != (1, 1):
            raise QueueOperationError("corrupt_purge_start_failed")
        return None

    def _directed_corrupt_purge(
        self, task_id: str, task_fence: TaskFence, now: datetime
    ) -> tuple[str, Path, str | None]:
        """(operation id, package, blocking code) for this purge attempt."""
        with closing(self._connect()) as database, begin_immediate(database):
            task, disposition, existing_operation = self._corrupt_purge_rows(
                database, task_id
            )
            retention = self._retention_blocker(disposition, existing_operation, now)
            if retention is not None:
                return "", self.results_dir, retention
            package_key = str(disposition["disposition_key"])
            package = self.results_dir / f"corrupt-{package_key}"
            package_code = self._corrupt_package_purge_blocker(package, disposition)
            if package_code is not None:
                return "", package, package_code
            return self._corrupt_purge_operation(
                database,
                task_id,
                task=task,
                disposition=disposition,
                existing_operation=existing_operation,
                task_fence=task_fence,
                now=now,
                package=package,
                package_key=package_key,
            )

    def _corrupt_purge_operation(
        self,
        database: sqlite3.Connection,
        task_id: str,
        *,
        task: sqlite3.Row,
        disposition: sqlite3.Row,
        existing_operation: sqlite3.Row | None,
        task_fence: TaskFence,
        now: datetime,
        package: Path,
        package_key: str,
    ) -> tuple[str, Path, str | None]:
        """Start this purge operation, or re-attach to the one already open."""
        operation_key = sha256_bytes(
            canonical_json_bytes(
                {
                    "disposition_sha256": disposition["disposition_sha256"],
                    "package_key": package_key,
                    "task_id": task_id,
                }
            )
        )
        operation_id = f"corrupt-purge:{operation_key}"
        if existing_operation is None:
            blocker = self._start_corrupt_purge(
                database, task_id, task, operation_id, task_fence, now
            )
            return operation_id, package, blocker
        _require_matching_purge_operation(existing_operation, task, operation_id)
        return operation_id, package, None

    def _finish_corrupt_purge(
        self,
        task_id: str,
        operation_id: str,
        package: Path,
        task_fence: TaskFence,
        owner: OwnerLease,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> CorruptPurgeProgress:
        page_progress = self._purge_corrupt_lineage_page(
            task_id,
            operation_id=operation_id,
            package=package,
            owner=owner,
            task_fence=task_fence,
            deadline=deadline,
            cancelled=cancelled,
        )
        if page_progress.state == "blocked":
            return page_progress
        with closing(self._connect()) as database:
            incoming = database.execute(
                "SELECT 1 FROM tasks WHERE redrive_of=? LIMIT 1", (task_id,)
            ).fetchone()
        if incoming is not None:
            return page_progress
        self._publish_corrupt_purge_receipt(
            task_id,
            operation_id=operation_id,
            package=package,
            task_fence=task_fence,
        )
        self._delete_corrupt_parent(
            task_id,
            operation_id=operation_id,
            package=package,
            task_fence=task_fence,
        )
        return CorruptPurgeProgress(
            task_id=task_id,
            operation_id=operation_id,
            state="purged",
            pages_written=page_progress.pages_written,
            links_deleted=page_progress.links_deleted,
            complete=True,
            code=None,
        )

    def purge_quarantined(
        self,
        task_id: str,
        *,
        owner: OwnerLease,
        deadline: float = float("inf"),
        cancelled: Callable[[], bool] | None = None,
    ) -> CorruptPurgeProgress:
        from operational_ownership import OwnerLease

        require_active(deadline, cancelled)
        require_repair_owner(owner, OwnerLease, "corrupt purge requires a repair owner")
        require_task_identifier(task_id)
        registry = self.ownership_registry()
        with closing(registry.connect()) as coordinator_database:
            registry.require(coordinator_database, owner)
        completed = self._completed_corrupt_purge_progress(task_id)
        if completed is not None:
            return completed
        with self.queue_owner(
            role="queue-operator",
            scope=f"task:{sha256_bytes(task_id.encode('utf-8'))}",
            parent=owner,
        ), self._corrupt_purge_task_fence(task_id, owner=owner) as task_fence:
            operation_id, package, blocker = self._directed_corrupt_purge(
                task_id, task_fence, queue_store.utc_now()
            )
            if blocker is not None:
                return _blocked_purge(task_id, blocker)
            return self._finish_corrupt_purge(
                task_id, operation_id, package, task_fence, owner, deadline, cancelled
            )

    def _completed_corrupt_purge_progress(
        self, task_id: str
    ) -> CorruptPurgeProgress | None:
        completed = self._completed_purge_rows(task_id)
        if completed is None:
            return None
        operation, disposition, links_deleted = completed
        package = self.results_dir / f"corrupt-{disposition['disposition_key']}"
        receipt = _read_purge_receipt(package)
        _require_matching_purge_receipt(receipt, operation, disposition, task_id)
        return CorruptPurgeProgress(
            task_id=task_id,
            operation_id=str(operation["operation_id"]),
            state="purged",
            pages_written=int(operation["page_count"]),
            links_deleted=links_deleted,
            complete=True,
            code=None,
        )

    def _completed_purge_rows(
        self, task_id: str
    ) -> tuple[sqlite3.Row, sqlite3.Row, int] | None:
        """The purge rows for a task that is gone, or None while it is still here."""
        with closing(self._connect()) as database:
            operation = _published_purge_operation(database, task_id)
            if operation is None:
                return None
            disposition = database.execute(
                """SELECT disposition.*,export.disposition_key
                   FROM corrupt_dispositions AS disposition
                   JOIN corrupt_export_operations AS export
                     ON export.operation_id=disposition.operation_id
                   WHERE disposition.task_id=?""",
                (task_id,),
            ).fetchone()
            links_deleted = int(
                database.execute(
                    """SELECT COALESCE(SUM(deleted_link_count),0)
                       FROM corrupt_purge_pages WHERE operation_id=?""",
                    (operation["operation_id"],),
                ).fetchone()[0]
            )
        if disposition is None:
            raise QueueOperationError("corrupt_purge_completion_invalid")
        return operation, disposition, links_deleted

    def _corrupt_package_purge_blocker(
        self, package: Path, disposition: sqlite3.Row
    ) -> str | None:
        """Why the exported package cannot be trusted as the task's evidence."""
        try:
            if not corrupt_package_trustworthy(package, disposition):
                return "corrupt_package_invalid"
        except (OSError, PermissionError, ValueError, json.JSONDecodeError):
            return "corrupt_package_invalid"
        return None

    def purge(
        self,
        *,
        terminal_before: datetime,
        export_path: Path,
        include_dead: bool = False,
        deadline: float = float("inf"),
        cancelled: Callable[[], bool] | None = None,
    ) -> PurgeReceipt:
        require_active(deadline, cancelled)
        plan = self._ordinary_purge_plan(
            terminal_before, export_path, include_dead=include_dead
        )
        manifest_bytes = self._publish_ordinary_purge_export(
            plan, deadline=deadline, cancelled=cancelled
        )
        capture_evidence = self._commit_ordinary_purge(
            plan,
            manifest_bytes,
            deadline=deadline,
            cancelled=cancelled,
        )
        self._cleanup_ordinary_purge_artifacts(plan, capture_evidence)
        self._publish_ordinary_purge_receipt(plan, manifest_bytes)
        self._clear_ordinary_purge_authorizations(plan, manifest_bytes)
        return PurgeReceipt(len(plan.task_ids), plan.task_ids, plan.retained)

    def _ordinary_purge_plan(
        self, terminal_before: datetime, export_path: Path, *, include_dead: bool
    ) -> _OrdinaryPurgePlan:
        retention_cutoff = queue_store.utc_now() - timedelta(
            days=DEFAULTS.queue_result_retention_days
        )
        cutoff = timestamp(min(as_utc(terminal_before), retention_cutoff))
        export = Path(export_path).absolute()
        if export.is_symlink():
            raise QueueOperationError("export_verification_failed")
        if export.exists():
            return self._load_ordinary_purge_plan(cutoff, export)
        return self._new_ordinary_purge_plan(cutoff, export, include_dead)

    def _demote_corrupt_finished_tasks(self, cutoff: str, include_dead: bool) -> None:
        """Move corrupt finished rows out of the purge selection, and commit it.

        One corrupt row used to refuse the whole plan, and nothing else can move
        a finished row. Demoted to `dead / payload_hash_mismatch` it leaves the
        selection and becomes something `quarantine-corrupt` accepts.
        """
        now = queue_store.utc_now()
        with closing(self._connect()) as database, begin_immediate(database):
            rows = database.execute(
                _ordinary_purge_selection(include_dead), (cutoff,)
            ).fetchall()
            for row in rows:
                self._require_valid_task_payload(database, row, now=now, parse=True)

    def _new_ordinary_purge_plan(
        self, cutoff: str, export: Path, include_dead: bool
    ) -> _OrdinaryPurgePlan:
        self._demote_corrupt_finished_tasks(cutoff, include_dead)
        with closing(self._connect()) as database:
            database.execute("BEGIN")
            selected = database.execute(
                f"{_ordinary_purge_selection(include_dead)} ORDER BY created_at,id",
                (cutoff,),
            ).fetchall()
            rows, retained = self._rows_with_releasable_evidence(database, selected)
            records = tuple(
                self._export_task_in_transaction(database, row) for row in rows
            )
            capture_evidence = self._ordinary_capture_evidence_for_rows(
                database, rows
            )
        task_ids = tuple(str(row["id"]) for row in rows)
        records_bytes = canonical_json_bytes(list(records))
        if len(records_bytes) > MAX_EXPORT_METADATA_BYTES:
            raise QueueOperationError("export_metadata_too_large")
        return _OrdinaryPurgePlan(
            cutoff,
            export,
            task_ids,
            records,
            records_bytes,
            capture_evidence,
            None,
            retained,
        )

    def _rows_with_releasable_evidence(
        self, database: sqlite3.Connection, rows: list[sqlite3.Row]
    ) -> tuple[list[sqlite3.Row], tuple[str, ...]]:
        """The rows the purge can prove, and the ids of those it keeps.

        A capture task without a terminal record used to abort the whole plan, so
        one unresolved capture kept every finished row in the queue. It is left
        in place and named instead. See
        `docs/research/2026-09-25-a-dead-capture-gets-its-second-chance-after-a-fix.md`.
        Compile history likewise retains its task; its foreign key must not abort
        deletion of unrelated finished work. Semantic retirement is separate.
        """
        kept: list[sqlite3.Row] = []
        retained: list[str] = []
        for row in rows:
            task_id = str(row["id"])
            if self._ordinary_task_evidence_releasable(database, task_id):
                kept.append(row)
                continue
            retained.append(task_id)
        return kept, tuple(retained)

    def _ordinary_task_evidence_releasable(self, database, task_id: str) -> bool:
        if compile_progress.retains_task(database, task_id):
            return False
        return self._capture_resolved(database, task_id)

    def _capture_resolved(self, database: sqlite3.Connection, task_id: str) -> bool:
        link = database.execute(
            "SELECT 1 FROM capture_task_links WHERE task_id=?", (task_id,)
        ).fetchone()
        if link is None:
            return True
        # A redriven capture's parent and child share one intent, and the terminal
        # record binds only the task that finished it: the parent failed the proof
        # and the whole weekly purge stopped (audit 2026-09-26 A-12). The family is
        # kept, named in `retained`, and never purged half.
        if _in_redrive_family(database, task_id):
            return False
        return self._capture_proof_settled(database, task_id)

    def _capture_proof_settled(self, database: sqlite3.Connection, task_id: str) -> bool:
        binding = self.active_capture_binding(database, task_id)
        blocker = self._capture_terminal_blocker(database, task_id, binding)
        if blocker in (None, "capture_intent_unresolved"):
            return blocker is None
        # A terminal record that exists but does not bind this task is damage,
        # not a capture still waiting: the purge refuses rather than keeping it.
        raise QueueOperationError(blocker)

    def _ordinary_capture_evidence_for_rows(
        self, database: sqlite3.Connection, rows: list[sqlite3.Row]
    ) -> tuple[_CapturePurgeEvidence, ...]:
        evidence: list[_CapturePurgeEvidence] = []
        for row in rows:
            evidence.extend(
                self._ordinary_capture_purge_evidence(database, str(row["id"]))
            )
        return tuple(evidence)

    def _load_ordinary_purge_plan(
        self, cutoff: str, export: Path
    ) -> _OrdinaryPurgePlan:
        self._require_ordinary_purge_export_directory(export)
        try:
            records_bytes = read_stable_owner_file(
                export / "records.json", MAX_EXPORT_METADATA_BYTES
            )
            manifest_bytes = read_stable_owner_file(
                export / "manifest.json", MAX_EXPORT_METADATA_BYTES
            )
            records, manifest = self._decode_ordinary_purge_export(
                records_bytes, manifest_bytes
            )
        except QueueOperationError:
            raise
        except (OSError, PermissionError, UnicodeError, ValueError) as exc:
            raise QueueOperationError("export_verification_failed") from exc
        task_ids = tuple(str(record["task_id"]) for record in records)
        self._require_ordinary_purge_manifest(
            manifest,
            cutoff=cutoff,
            task_ids=task_ids,
            records_bytes=records_bytes,
        )
        capture_evidence = self._capture_purge_evidence_from_manifest(
            manifest["capture_artifacts"], records
        )
        plan = _OrdinaryPurgePlan(
            cutoff,
            export,
            task_ids,
            records,
            records_bytes,
            capture_evidence,
            manifest_bytes,
        )
        self._require_existing_ordinary_purge_files(plan, manifest)
        return plan

    @staticmethod
    def _require_ordinary_purge_export_directory(export: Path) -> None:
        metadata = export.lstat()
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        actual = (
            stat.S_ISDIR(metadata.st_mode),
            stat.S_ISLNK(metadata.st_mode),
            bool(getattr(metadata, "st_file_attributes", 0) & reparse),
            is_owner_only(export),
        )
        if actual != (True, False, False, True):
            raise QueueOperationError("export_verification_failed")

    @staticmethod
    def _decode_ordinary_purge_export(
        records_bytes: bytes, manifest_bytes: bytes
    ) -> tuple[tuple[dict[str, object], ...], dict[str, object]]:
        records_value, manifest = (
            QueuePurgeOperations._decode_ordinary_purge_json(
                records_bytes, manifest_bytes
            )
        )
        records = QueuePurgeOperations._ordinary_purge_records(
            records_value, records_bytes
        )
        return records, manifest

    @staticmethod
    def _decode_ordinary_purge_json(
        records_bytes: bytes, manifest_bytes: bytes
    ) -> tuple[list[object], dict[str, object]]:
        try:
            records_value = json.loads(records_bytes.decode("utf-8", errors="strict"))
            manifest = json.loads(manifest_bytes.decode("utf-8", errors="strict"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise QueueOperationError("export_verification_failed") from exc
        if (isinstance(records_value, list), isinstance(manifest, dict)) != (True, True):
            raise QueueOperationError("export_verification_failed")
        return records_value, manifest

    @staticmethod
    def _ordinary_purge_records(
        records_value: list[object], records_bytes: bytes
    ) -> tuple[dict[str, object], ...]:
        if not all(isinstance(record, dict) for record in records_value):
            raise QueueOperationError("export_verification_failed")
        records = tuple(records_value)
        if canonical_json_bytes(list(records)) != records_bytes:
            raise QueueOperationError("export_verification_failed")
        for record in records:
            validate_schema(
                record, SCHEMA_DIRECTORY / "queue-task-v3.json"
            )
        return records

    @staticmethod
    def _require_ordinary_purge_manifest(
        manifest: Mapping[str, object],
        *,
        cutoff: str,
        task_ids: tuple[str, ...],
        records_bytes: bytes,
    ) -> None:
        identity = (
            set(manifest),
            manifest.get("cutoff"),
            manifest.get("records_sha256"),
            manifest.get("task_ids"),
        )
        expected = (
            {"capture_artifacts", "cutoff", "records_sha256", "results", "task_ids"},
            cutoff,
            sha256_bytes(records_bytes),
            list(task_ids),
        )
        if identity != expected or len(set(task_ids)) != len(task_ids):
            raise QueueOperationError("export_verification_failed")
        if not isinstance(manifest.get("results"), list) or not isinstance(
            manifest.get("capture_artifacts"), list
        ):
            raise QueueOperationError("export_verification_failed")

    @staticmethod
    def _capture_purge_manifest_identity(
        value: object,
    ) -> tuple[str, str, str, str, str, str, str]:
        record = QueuePurgeOperations._capture_purge_manifest_mapping(value)
        QueuePurgeOperations._require_capture_purge_manifest_hashes(record)
        return (
            str(record["task_id"]),
            str(record["intent_id"]),
            str(record["terminal_path"]),
            str(record["kind"]),
            str(record["source_path"]),
            str(record["archive_path"]),
            str(record["sha256"]),
        )

    @staticmethod
    def _capture_purge_manifest_mapping(value: object) -> dict[str, object]:
        fields = {
            "archive_path",
            "intent_id",
            "kind",
            "sha256",
            "source_path",
            "task_id",
            "terminal_path",
        }
        if not isinstance(value, dict):
            raise QueueOperationError("export_verification_failed")
        actual = (set(value), {type(item) for item in value.values()})
        if actual != (fields, {str}):
            raise QueueOperationError("export_verification_failed")
        return value

    @staticmethod
    def _require_capture_purge_manifest_hashes(
        value: Mapping[str, object],
    ) -> None:
        if value["kind"] not in {"intent", "decision", "source"}:
            raise QueueOperationError("export_verification_failed")
        try:
            require_lower_sha256(str(value["intent_id"]), "intent_id")
            require_lower_sha256(str(value["sha256"]), "sha256")
        except ValueError as exc:
            raise QueueOperationError("export_verification_failed") from exc

    @staticmethod
    def _capture_purge_manifest_artifact(
        task_id: str,
        source_path: str,
        archive_path: str,
        digest: str,
    ) -> CapturePurgeArtifact:
        try:
            restricted_relative_path(
                source_path, ("run/capture-intents", "run/queue-results")
            )
            restricted_relative_path(archive_path, ("capture-artifacts",))
        except ValueError as exc:
            raise QueueOperationError("export_verification_failed") from exc
        if archive_path != capture_purge_archive_path(task_id, source_path):
            raise QueueOperationError("export_verification_failed")
        return CapturePurgeArtifact(source_path, archive_path, digest)

    def _capture_purge_evidence_from_manifest(
        self,
        value: object,
        records: tuple[dict[str, object], ...],
    ) -> tuple[_CapturePurgeEvidence, ...]:
        if not isinstance(value, list):
            raise QueueOperationError("export_verification_failed")
        grouped: dict[
            tuple[str, str, str], list[tuple[str, CapturePurgeArtifact]]
        ] = {}
        for item in value:
            task_id, intent_id, terminal, kind, source, archive, digest = (
                self._capture_purge_manifest_identity(item)
            )
            artifact = self._capture_purge_manifest_artifact(
                task_id, source, archive, digest
            )
            grouped.setdefault((task_id, intent_id, terminal), []).append(
                (kind, artifact)
            )
        evidence = tuple(
            self._capture_purge_evidence_group(key, grouped[key])
            for key in sorted(grouped)
        )
        self._require_capture_purge_evidence_records(evidence, records)
        return evidence

    @staticmethod
    def _capture_purge_evidence_group(
        key: tuple[str, str, str],
        items: list[tuple[str, CapturePurgeArtifact]],
    ) -> _CapturePurgeEvidence:
        intents, decisions, sources = QueuePurgeOperations._partition_capture_purge_artifacts(
            items
        )
        QueuePurgeOperations._require_unique_capture_purge_paths(items, intents)
        task_id, intent_id, terminal_path = key
        return _CapturePurgeEvidence(
            task_id, intent_id, intents[0], decisions, terminal_path, sources
        )

    @staticmethod
    def _partition_capture_purge_artifacts(
        items: list[tuple[str, CapturePurgeArtifact]],
    ) -> tuple[tuple[CapturePurgeArtifact, ...], ...]:
        grouped: dict[str, list[CapturePurgeArtifact]] = {"intent": [], "decision": [], "source": []}
        for kind, artifact in items:
            grouped[kind].append(artifact)
        return tuple(tuple(grouped[kind]) for kind in ("intent", "decision", "source"))

    @staticmethod
    def _require_unique_capture_purge_paths(
        items: list[tuple[str, CapturePurgeArtifact]],
        intents: tuple[CapturePurgeArtifact, ...],
    ) -> None:
        paths = {artifact.source_path for _kind, artifact in items}
        if (len(intents), len(paths)) != (1, len(items)):
            raise QueueOperationError("export_verification_failed")

    @staticmethod
    def _ordinary_purge_record_capture_identity(
        record: Mapping[str, object],
    ) -> tuple[str, str, str, str] | None:
        binding = record.get("capture_binding")
        if binding is None:
            return None
        result = record.get("result")
        if (isinstance(binding, dict), isinstance(result, dict)) != (True, True):
            raise QueueOperationError("export_verification_failed")
        return QueuePurgeOperations._ordinary_capture_record_values(
            record, binding, result
        )

    @staticmethod
    def _ordinary_capture_record_values(
        record: Mapping[str, object],
        binding: Mapping[str, object],
        result: Mapping[str, object],
    ) -> tuple[str, str, str, str]:
        task_id = record.get("task_id")
        intent_id = binding.get("intent_id")
        intent_sha256 = binding.get("intent_sha256")
        terminal_path = result.get("reference")
        values = (task_id, intent_id, intent_sha256, terminal_path)
        if tuple(isinstance(item, str) for item in values) != (True,) * len(values):
            raise QueueOperationError("export_verification_failed")
        if terminal_path != f"run/queue-results/capture-{intent_id}.json":
            raise QueueOperationError("export_verification_failed")
        return str(task_id), str(intent_id), str(intent_sha256), str(terminal_path)

    def _require_capture_purge_evidence_records(
        self,
        evidence: tuple[_CapturePurgeEvidence, ...],
        records: tuple[dict[str, object], ...],
    ) -> None:
        expected = self._ordinary_purge_expected_capture_records(records)
        observed = {
            item.task_id: (
                item.intent_id,
                item.intent.sha256,
                item.terminal_path,
            )
            for item in evidence
        }
        if (len(observed), observed) != (len(evidence), expected):
            raise QueueOperationError("export_verification_failed")

    def _ordinary_purge_expected_capture_records(
        self, records: tuple[dict[str, object], ...]
    ) -> dict[str, tuple[str, str, str]]:
        expected: dict[str, tuple[str, str, str]] = {}
        for record in records:
            identity = self._ordinary_purge_record_capture_identity(record)
            if identity is not None:
                task_id, intent_id, intent_sha256, terminal = identity
                expected[task_id] = (intent_id, intent_sha256, terminal)
        return expected

    @staticmethod
    def _ordinary_purge_expected_results(
        records: tuple[dict[str, object], ...],
    ) -> list[dict[str, str]]:
        return [
            {"id": str(record["task_id"]), "sha256": str(record["result"]["sha256"])}
            for record in records
            if isinstance(record.get("result"), dict)
        ]

    @staticmethod
    def _capture_purge_artifacts(
        evidence: tuple[_CapturePurgeEvidence, ...],
    ) -> tuple[CapturePurgeArtifact, ...]:
        return tuple(
            artifact
            for item in evidence
            for artifact in (item.intent, *item.decisions, *item.sources)
        )

    def _require_existing_ordinary_purge_files(
        self, plan: _OrdinaryPurgePlan, manifest: Mapping[str, object]
    ) -> None:
        expected_results = self._ordinary_purge_expected_results(plan.records)
        if manifest["results"] != expected_results:
            raise QueueOperationError("export_verification_failed")
        self._require_existing_ordinary_result_files(plan.export, expected_results)
        self._require_existing_capture_purge_files(plan)

    @staticmethod
    def _require_existing_ordinary_result_files(
        export: Path, expected_results: list[dict[str, str]]
    ) -> None:
        for item in expected_results:
            relative = _ordinary_purge_result_archive_path(item["id"])
            data = read_stable_owner_file(export / relative, MAX_RESULT_BYTES)
            if sha256_bytes(data) != item["sha256"]:
                raise QueueOperationError("export_verification_failed")

    def _require_existing_capture_purge_files(
        self, plan: _OrdinaryPurgePlan
    ) -> None:
        for artifact in self._capture_purge_artifacts(plan.capture_evidence):
            data = read_stable_owner_file(
                plan.export / artifact.archive_path, MAX_QUEUE_PAYLOAD_BYTES
            )
            if sha256_bytes(data) != artifact.sha256:
                raise QueueOperationError("export_verification_failed")

    def _publish_ordinary_purge_export(
        self,
        plan: _OrdinaryPurgePlan,
        *,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> bytes:
        if plan.manifest_bytes is not None:
            return self._existing_ordinary_purge_manifest(plan)
        return self._publish_new_ordinary_purge_export(
            plan, deadline=deadline, cancelled=cancelled
        )

    @staticmethod
    def _existing_ordinary_purge_manifest(plan: _OrdinaryPurgePlan) -> bytes:
        current = read_stable_owner_file(
            plan.export / "manifest.json", MAX_EXPORT_METADATA_BYTES
        )
        if current != plan.manifest_bytes:
            raise QueueOperationError("export_verification_failed")
        return current

    def _publish_new_ordinary_purge_export(
        self,
        plan: _OrdinaryPurgePlan,
        *,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> bytes:
        parent = plan.export.parent
        parent.mkdir(parents=True, exist_ok=True)
        harden_owner_only(parent, 0o700)
        actual = (parent.is_symlink(), parent.is_dir(), is_owner_only(parent))
        if actual != (False, True, True):
            raise QueueOperationError("export_parent_permissions_invalid")
        cleanup_export_staging(parent, plan.export.name)
        staging = parent / f".{plan.export.name}.staging-{uuid.uuid4().hex}"
        staging.mkdir()
        harden_owner_only(staging, 0o700)
        try:
            manifest_bytes = self._write_ordinary_purge_export(
                plan, staging, deadline=deadline, cancelled=cancelled
            )
            require_active(deadline, cancelled)
            staging.replace(plan.export)
            fsync_directory(parent)
            return manifest_bytes
        except BaseException:
            remove_export_staging(staging)
            raise

    def _write_ordinary_purge_export(
        self,
        plan: _OrdinaryPurgePlan,
        staging: Path,
        *,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> bytes:
        results_export = staging / "results"
        results_export.mkdir()
        harden_owner_only(results_export, 0o700)
        results = self._ordinary_purge_result_manifest(
            plan.records,
            results_export,
            deadline=deadline,
            cancelled=cancelled,
        )
        capture_artifacts = self._ordinary_purge_capture_manifest(
            plan,
            staging,
            deadline=deadline,
            cancelled=cancelled,
        )
        manifest_bytes = canonical_json_bytes(
            {
                "capture_artifacts": capture_artifacts,
                "cutoff": plan.cutoff,
                "records_sha256": sha256_bytes(plan.records_bytes),
                "results": results,
                "task_ids": list(plan.task_ids),
            }
        )
        records_path = staging / "records.json"
        manifest_path = staging / "manifest.json"
        write_durable_file(records_path, plan.records_bytes)
        write_durable_file(manifest_path, manifest_bytes)
        self._require_ordinary_purge_export(
            records_path, plan.records_bytes, manifest_path, manifest_bytes
        )
        fsync_directory(results_export)
        fsync_directory(staging)
        return manifest_bytes

    def _ordinary_purge_capture_manifest(
        self,
        plan: _OrdinaryPurgePlan,
        staging: Path,
        *,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> list[dict[str, str]]:
        manifest: list[dict[str, str]] = []
        for evidence in plan.capture_evidence:
            require_active(deadline, cancelled)
            for kind, artifact in self._tagged_capture_purge_artifacts(evidence):
                manifest.append(
                    self._publish_capture_purge_artifact(
                        staging, evidence, artifact, kind=kind
                    )
                )
        capture_dir = staging / "capture-artifacts"
        if capture_dir.exists():
            fsync_directory(capture_dir)
        return manifest

    @staticmethod
    def _tagged_capture_purge_artifacts(evidence: _CapturePurgeEvidence):
        yield "intent", evidence.intent
        for artifact in evidence.decisions:
            yield "decision", artifact
        for artifact in evidence.sources:
            yield "source", artifact

    def _publish_capture_purge_artifact(
        self,
        staging: Path,
        evidence: _CapturePurgeEvidence,
        artifact: CapturePurgeArtifact,
        *,
        kind: Literal["intent", "decision", "source"],
    ) -> dict[str, str]:
        data = read_runtime_bytes(
            self.state_root / artifact.source_path,
            self.state_root,
            max_bytes=MAX_QUEUE_PAYLOAD_BYTES,
            owner_only=True,
        )
        if sha256_bytes(data) != artifact.sha256:
            raise QueueOperationError("capture_purge_changed")
        target = staging / artifact.archive_path
        target.parent.mkdir(parents=True, exist_ok=True)
        harden_owner_only(target.parent, 0o700)
        write_durable_file(target, data)
        return {
            "archive_path": artifact.archive_path,
            "intent_id": evidence.intent_id,
            "kind": kind,
            "sha256": artifact.sha256,
            "source_path": artifact.source_path,
            "task_id": evidence.task_id,
            "terminal_path": evidence.terminal_path,
        }

    def _ordinary_purge_result_manifest(
        self,
        records: tuple[dict[str, object], ...],
        results_export: Path,
        *,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> list[dict[str, str]]:
        result_manifest: list[dict[str, str]] = []
        for record in records:
            require_active(deadline, cancelled)
            result = record["result"]
            if not isinstance(result, dict):
                continue
            reference = str(result["reference"])
            digest = str(result["sha256"])
            data = read_stable_owner_file(
                self.state_root / reference, MAX_RESULT_BYTES
            )
            if sha256_bytes(data) != digest:
                raise QueueOperationError("result_verification_failed")
            archive = _ordinary_purge_result_archive_path(str(record["task_id"]))
            write_durable_file(results_export.parent / archive, data)
            result_manifest.append({"id": str(record["task_id"]), "sha256": digest})
        return result_manifest

    @staticmethod
    def _require_ordinary_purge_export(
        records_path: Path,
        records_bytes: bytes,
        manifest_path: Path,
        manifest_bytes: bytes,
    ) -> None:
        actual = (
            read_stable_owner_file(records_path, MAX_EXPORT_METADATA_BYTES),
            read_stable_owner_file(manifest_path, MAX_EXPORT_METADATA_BYTES),
        )
        if actual != (records_bytes, manifest_bytes):
            raise QueueOperationError("export_verification_failed")

    def _commit_ordinary_purge(
        self,
        plan: _OrdinaryPurgePlan,
        manifest_bytes: bytes,
        *,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> tuple[_CapturePurgeEvidence, ...]:
        if not plan.task_ids:
            return ()
        manifest_sha256 = sha256_bytes(manifest_bytes)
        operation_id = _ordinary_purge_operation_id(manifest_sha256)
        receipt_published = self._ordinary_purge_receipt_matches(
            plan, manifest_bytes
        )
        expected = {item.task_id: item for item in plan.capture_evidence}
        with closing(self._connect()) as database, begin_immediate(
            database,
            before_commit=lambda: require_active(deadline, cancelled),
        ):
            state = self._ordinary_purge_commit_state(
                database,
                plan,
                operation_id=operation_id,
                manifest_sha256=manifest_sha256,
                receipt_published=receipt_published,
            )
            if state == "pending":
                self._purge_ordinary_tasks(
                    database,
                    plan,
                    expected,
                    operation_id=operation_id,
                    manifest_sha256=manifest_sha256,
                    deadline=deadline,
                    cancelled=cancelled,
                )
        return plan.capture_evidence

    def _purge_ordinary_tasks(
        self,
        database: sqlite3.Connection,
        plan: _OrdinaryPurgePlan,
        expected: Mapping[str, _CapturePurgeEvidence],
        *,
        operation_id: str,
        manifest_sha256: str,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> None:
        for task_id, expected_record in zip(plan.task_ids, plan.records):
            require_active(deadline, cancelled)
            self._purge_ordinary_task(
                database,
                task_id,
                expected_record,
                expected_capture=expected.get(task_id),
                cutoff=plan.cutoff,
                operation_id=operation_id,
                manifest_sha256=manifest_sha256,
            )

    def _ordinary_purge_commit_state(
        self,
        database: sqlite3.Connection,
        plan: _OrdinaryPurgePlan,
        *,
        operation_id: str,
        manifest_sha256: str,
        receipt_published: bool,
    ) -> str:
        placeholders = ",".join("?" for _ in plan.task_ids)
        present = database.execute(
            f"SELECT id FROM tasks WHERE id IN ({placeholders})",  # noqa: S608
            plan.task_ids,
        ).fetchall()
        authorizations = database.execute(
            f"""SELECT * FROM task_purge_authorizations
                WHERE task_id IN ({placeholders}) ORDER BY task_id""",  # noqa: S608
            plan.task_ids,
        ).fetchall()
        counts = (len(present), len(authorizations))
        if counts == (len(plan.task_ids), 0):
            return "pending"
        return self._settled_ordinary_purge_state(
            counts,
            authorizations,
            plan,
            operation_id=operation_id,
            manifest_sha256=manifest_sha256,
            receipt_published=receipt_published,
        )

    def _settled_ordinary_purge_state(
        self,
        counts: tuple[int, int],
        authorizations: list,
        plan: _OrdinaryPurgePlan,
        *,
        operation_id: str,
        manifest_sha256: str,
        receipt_published: bool,
    ) -> str:
        """Where a resumed purge stands once its tasks are already gone."""
        if counts == (0, len(plan.task_ids)):
            self._require_ordinary_purge_authorizations(
                authorizations, plan.task_ids, operation_id, manifest_sha256
            )
            return "cleanup"
        if (counts, receipt_published) == ((0, 0), True):
            return "complete"
        raise QueueOperationError("purge_resume_conflict")

    @staticmethod
    def _require_ordinary_purge_authorizations(
        rows: list[sqlite3.Row],
        task_ids: tuple[str, ...],
        operation_id: str,
        manifest_sha256: str,
    ) -> None:
        expected = sorted(
            (
                task_id,
                "ordinary",
                operation_id,
                ordinary_purge_authorization_digest(
                    task_id, operation_id, manifest_sha256
                ),
            )
            for task_id in task_ids
        )
        actual = sorted(
            (
                str(row["task_id"]),
                str(row["mode"]),
                str(row["operation_id"]),
                str(row["authorization_digest"]),
            )
            for row in rows
        )
        if actual != expected:
            raise QueueOperationError("purge_authorization_failed")

    def _purge_ordinary_task(
        self,
        database: sqlite3.Connection,
        task_id: str,
        expected_record: Mapping[str, object],
        *,
        expected_capture: _CapturePurgeEvidence | None,
        cutoff: str,
        operation_id: str,
        manifest_sha256: str,
    ) -> None:
        row = database.execute(
            "SELECT * FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        self._require_ordinary_purge_task(row, cutoff)
        self._require_ordinary_purge_payload(database, row)
        self._require_ordinary_purge_record(database, row, expected_record)
        evidence = self._ordinary_capture_purge_evidence(database, task_id)
        expected_evidence = () if expected_capture is None else (expected_capture,)
        if evidence != expected_evidence:
            raise QueueOperationError("capture_purge_changed")
        self._insert_ordinary_purge_authorization(
            database, task_id, operation_id, manifest_sha256
        )
        self._delete_capture_purge_rows_before_task(database, evidence)
        self._delete_task_owned_evidence(database, task_id)
        self._delete_ordinary_task(database, task_id)
        self._delete_capture_purge_rows_after_task(database, evidence)

    @staticmethod
    def _require_ordinary_purge_task(row: sqlite3.Row | None, cutoff: str) -> None:
        """Still a finished row an ordinary purge may take, still older than the cutoff.

        Which of those states this particular purge planned for is pinned by the
        exported record, compared field for field a moment later, so this guard
        names the states the operation can take at all and no narrower.
        """
        if (
            row is None
            or row["state"] not in _ORDINARY_PURGEABLE_STATES
            or row["updated_at"] >= cutoff
        ):
            raise QueueOperationError("purge_selection_changed")

    def _require_ordinary_purge_payload(
        self, database: sqlite3.Connection, row: sqlite3.Row
    ) -> None:
        validation = validate_payload_blob(
            bytes(row["payload_blob"]), row["input_hash"], parse=True
        )
        if validation.code is not None:
            self._demote_payload_mismatch(database, row, now=queue_store.utc_now())
            raise QueueOperationError("payload_hash_mismatch")

    def _require_ordinary_purge_record(
        self,
        database: sqlite3.Connection,
        row: sqlite3.Row,
        expected_record: Mapping[str, object],
    ) -> None:
        current = self._export_task_in_transaction(database, row)
        if canonical_json_bytes(current) != canonical_json_bytes(expected_record):
            raise QueueOperationError("purge_selection_changed")

    def _ordinary_capture_purge_evidence(
        self, database: sqlite3.Connection, task_id: str
    ) -> tuple[_CapturePurgeEvidence, ...]:
        link = database.execute(
            "SELECT 1 FROM capture_task_links WHERE task_id=?", (task_id,)
        ).fetchone()
        if link is None:
            return ()
        binding = self.active_capture_binding(database, task_id)
        return (self._capture_purge_evidence(database, task_id, binding),)

    @staticmethod
    def _insert_ordinary_purge_authorization(
        database: sqlite3.Connection,
        task_id: str,
        operation_id: str,
        manifest_sha256: str,
    ) -> None:
        authorization_digest = ordinary_purge_authorization_digest(
            task_id, operation_id, manifest_sha256
        )
        inserted = database.execute(
            """INSERT INTO task_purge_authorizations(
                   task_id,mode,operation_id,authorization_digest,created_at
               ) VALUES (?,'ordinary',?,?,?)""",
            (task_id, operation_id, authorization_digest, timestamp(queue_store.utc_now())),
        ).rowcount
        if inserted != 1:
            raise QueueOperationError("purge_authorization_failed")

    @staticmethod
    def _delete_capture_purge_rows_before_task(
        database: sqlite3.Connection,
        evidence: tuple[_CapturePurgeEvidence, ...],
    ) -> None:
        for item in evidence:
            deleted = database.execute(
                "DELETE FROM semantic_decisions WHERE intent_id=?", (item.intent_id,)
            ).rowcount
            if deleted != len(item.decisions):
                raise QueueOperationError("capture_purge_changed")

    @staticmethod
    def _delete_ordinary_task(
        database: sqlite3.Connection, task_id: str
    ) -> None:
        deleted = database.execute(
            "DELETE FROM tasks WHERE id=?", (task_id,)
        ).rowcount
        if deleted != 1:
            raise QueueOperationError("purge_delete_failed")

    @staticmethod
    def _delete_capture_purge_rows_after_task(
        database: sqlite3.Connection,
        evidence: tuple[_CapturePurgeEvidence, ...],
    ) -> None:
        for item in evidence:
            deleted = database.execute(
                "DELETE FROM capture_intents WHERE intent_id=?", (item.intent_id,)
            ).rowcount
            if deleted != 1:
                raise QueueOperationError("capture_purge_changed")

    def _cleanup_ordinary_purge_artifacts(
        self,
        plan: _OrdinaryPurgePlan,
        evidence: tuple[_CapturePurgeEvidence, ...],
    ) -> None:
        retained_terminals = {item.terminal_path for item in evidence}
        for item in evidence:
            self._unlink_capture_purge_artifacts(plan.export, item)
        for record in plan.records:
            self._cleanup_ordinary_result(record, retained_terminals)

    def _cleanup_ordinary_result(
        self, record: Mapping[str, object], retained_terminals: set[str]
    ) -> None:
        """Drop this export's result file unless something still names it."""
        reference = _cleanable_result_reference(record, retained_terminals)
        if reference is None:
            return
        with closing(self._connect()) as database, begin_immediate(database):
            if self._result_reference_retained(database, reference):
                return
            path = self.state_root / reference
            path.unlink(missing_ok=True)
            fsync_directory(path.parent)

    @staticmethod
    def _result_reference_retained(database, reference: str) -> bool:
        retained = database.execute(
            "SELECT 1 FROM tasks WHERE result_reference=? LIMIT 1", (reference,)
        ).fetchone()
        return retained is not None or compile_progress.retains_artifact(database, reference)

    def _unlink_capture_purge_artifacts(
        self, export: Path, evidence: _CapturePurgeEvidence
    ) -> None:
        for artifact in (evidence.intent, *evidence.decisions, *evidence.sources):
            self._unlink_capture_purge_artifact(export, evidence.task_id, artifact)

    def _unlink_capture_purge_artifact(
        self,
        export: Path,
        task_id: str,
        artifact: CapturePurgeArtifact,
    ) -> None:
        self._capture_purge_manifest_artifact(
            task_id, artifact.source_path, artifact.archive_path, artifact.sha256
        )
        archived = read_stable_owner_file(
            export / artifact.archive_path, MAX_QUEUE_PAYLOAD_BYTES
        )
        if sha256_bytes(archived) != artifact.sha256:
            raise QueueOperationError("export_verification_failed")
        source = self.state_root / artifact.source_path
        try:
            before = source.lstat()
        except FileNotFoundError:
            return
        with closing(self._connect()) as database, begin_immediate(database):
            if self._result_reference_retained(database, artifact.source_path):
                return
            self._unlink_verified_source(source, before, artifact)

    def _unlink_verified_source(
        self, source: Path, before: os.stat_result, artifact: CapturePurgeArtifact
    ) -> None:
        """Remove the source only while it is still what the export recorded."""
        current = read_runtime_bytes(
            source,
            self.state_root,
            max_bytes=MAX_QUEUE_PAYLOAD_BYTES,
            owner_only=True,
        )
        if sha256_bytes(current) != artifact.sha256 or not os.path.samestat(
            before, source.lstat()
        ):
            raise QueueOperationError("capture_purge_changed")
        source.unlink()
        fsync_directory(source.parent)

    @staticmethod
    def _ordinary_purge_receipt_bytes(
        plan: _OrdinaryPurgePlan, manifest_bytes: bytes
    ) -> bytes:
        manifest_sha256 = sha256_bytes(manifest_bytes)
        return canonical_json_bytes(
            {
                "schema_version": "ordinary-purge-receipt/v1",
                "operation_id": _ordinary_purge_operation_id(manifest_sha256),
                "manifest_sha256": manifest_sha256,
                "task_ids": list(plan.task_ids),
            }
        )

    def _ordinary_purge_receipt_matches(
        self, plan: _OrdinaryPurgePlan, manifest_bytes: bytes
    ) -> bool:
        path = plan.export / "purge-receipt.json"
        try:
            current = read_stable_owner_file(path, 64 * 1024)
        except FileNotFoundError:
            return False
        if current != self._ordinary_purge_receipt_bytes(plan, manifest_bytes):
            raise QueueOperationError("purge_receipt_invalid")
        return True

    def _publish_ordinary_purge_receipt(
        self, plan: _OrdinaryPurgePlan, manifest_bytes: bytes
    ) -> None:
        receipt_bytes = self._ordinary_purge_receipt_bytes(plan, manifest_bytes)
        write_durable_file(plan.export / "purge-receipt.json", receipt_bytes)
        if not self._ordinary_purge_receipt_matches(plan, manifest_bytes):
            raise QueueOperationError("purge_receipt_invalid")

    def _clear_ordinary_purge_authorizations(
        self, plan: _OrdinaryPurgePlan, manifest_bytes: bytes
    ) -> None:
        if not plan.task_ids:
            return
        manifest_sha256 = sha256_bytes(manifest_bytes)
        operation_id = _ordinary_purge_operation_id(manifest_sha256)
        placeholders = ",".join("?" for _ in plan.task_ids)
        with closing(self._connect()) as database, begin_immediate(database):
            rows = database.execute(
                f"""SELECT * FROM task_purge_authorizations
                    WHERE task_id IN ({placeholders}) ORDER BY task_id""",  # noqa: S608
                plan.task_ids,
            ).fetchall()
            if not rows:
                return
            self._require_ordinary_purge_authorizations(
                rows, plan.task_ids, operation_id, manifest_sha256
            )
            _delete_ordinary_purge_authorizations(database, plan.task_ids, placeholders)

    def _export_task_in_transaction(
        self, database: sqlite3.Connection, row: sqlite3.Row
    ) -> dict[str, object]:
        """The export record for one task, read inside an open transaction."""
        task_id = str(row["id"])
        validation = validate_payload_blob(
            bytes(row["payload_blob"]), row["input_hash"], parse=True
        )
        if validation.code is not None:
            raise QueueOperationError("payload_hash_mismatch")
        rows = _read_exported_rows(database, task_id)
        return exported_task_record(row, validation.payload, rows)
