"""Queue record representation; SQL and transaction ownership remain with callers."""

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from llm_wiki.formats.schema import validate_schema
from llm_wiki.queue_contracts import AttemptRecord, QueueTask
from llm_wiki.queue_timing import _parse_timestamp, _timestamp

_TASK_SCHEMA = Path(__file__).parents[1] / "schemas" / "queue-task-v3.json"

def _exported_lease(row: sqlite3.Row) -> dict[str, object] | None:
    """The lease a task holds, or None when it holds none."""
    values = (
        row["lease_owner"],
        row["lease_token"],
        row["lease_expires_at"],
        row["lease_heartbeat_at"],
    )
    if values == (None, None, None, None):
        return None
    return {
        "owner": values[0],
        "token": values[1],
        "expires_at": values[2],
        "heartbeat_at": values[3],
    }


def _exported_result(row: sqlite3.Row) -> dict[str, object] | None:
    """The published result a task points at, or None when it has none."""
    values = (
        row["result_reference"],
        row["result_sha256"],
        row["result_operation_id"],
    )
    if values == (None, None, None):
        return None
    return {
        "reference": values[0],
        "sha256": values[1],
        "operation_id": values[2],
    }


def _exported_capture_binding(
    link: sqlite3.Row | None, seal: sqlite3.Row | None
) -> dict[str, object] | None:
    if link is None:
        return None
    return {
        "intent_id": link["intent_id"],
        "intent_sha256": link["intent_sha256"],
        "handler_version": int(link["handler_version"]),
        "active_digest": str(link["link_digest"]),
        "seal_digest": None if seal is None else seal["seal_digest"],
    }


def _attempt_history_entry(item: Mapping[str, object]) -> dict[str, object]:
    return {
        "attempt": int(item["attempt"]),
        "started_at": str(item["started_at"]),
        "finished_at": str(item["finished_at"]),
        "outcome": str(item["outcome"]),
        "error_code": item["error_code"],
    }



def _exported_attempt_history(history: Sequence[sqlite3.Row]) -> list[dict[str, object]]:
    return [_attempt_history_entry(item) for item in history]


def _exported_source_links(links: Sequence[sqlite3.Row]) -> list[dict[str, object]]:
    return [
        {
            "logical_path": str(item["logical_path"]),
            "source_digest": str(item["source_digest"]),
        }
        for item in links
    ]


def _exported_task_record(
    row: sqlite3.Row,
    payload: object,
    rows: _ExportedRows,
) -> dict[str, object]:
    """The schema-valid export record for one task."""
    record: dict[str, object] = {
        "schema_version": "queue-task/v3",
        "task_id": str(row["id"]),
        "kind": str(row["kind"]),
        "handler_version": int(row["handler_version"]),
        "payload": payload,
        "input_hash": str(row["input_hash"]),
        "dedupe_key": row["dedupe_key"],
        "state": str(row["state"]),
        "priority": int(row["priority"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "available_at": str(row["available_at"]),
        "attempts": int(row["attempts"]),
        "last_attempt_at": row["last_attempt_at"],
        "lease": _exported_lease(row),
        "error_code": row["error_code"],
        "blocked_capability": row["blocked_capability"],
        "result": _exported_result(row),
        "redrive_of": row["redrive_of"],
        "lineage_generation": int(row["lineage_generation"]),
        "attempt_history": _exported_attempt_history(rows.history),
        "source_links": _exported_source_links(rows.source_links),
        "capture_binding": _exported_capture_binding(rows.link, rows.seal),
    }
    validate_schema(
        record, _TASK_SCHEMA
    )
    return record


@dataclass(frozen=True)
class _ExportedRows:
    """Everything an export reads about one task, beside the task row."""

    history: Sequence[sqlite3.Row]
    source_links: Sequence[sqlite3.Row]
    link: sqlite3.Row | None
    seal: sqlite3.Row | None


def _task_record(
    row: sqlite3.Row, history: Sequence[sqlite3.Row], payload: object
) -> QueueTask:
    """One task record, whichever column its already-decoded payload came from."""
    return QueueTask(
        id=str(row["id"]),
        kind=str(row["kind"]),
        handler_version=int(row["handler_version"]),
        payload=payload,
        input_hash=str(row["input_hash"]),
        dedupe_key=row["dedupe_key"],
        state=str(row["state"]),
        priority=int(row["priority"]),
        created_at=_parse_timestamp(row["created_at"]),  # type: ignore[arg-type]
        updated_at=_parse_timestamp(row["updated_at"]),  # type: ignore[arg-type]
        available_at=_parse_timestamp(row["available_at"]),  # type: ignore[arg-type]
        attempts=int(row["attempts"]),
        last_attempt_at=_parse_timestamp(row["last_attempt_at"]),
        lease_owner=row["lease_owner"],
        lease_token=row["lease_token"],
        lease_expires_at=_parse_timestamp(row["lease_expires_at"]),
        lease_heartbeat_at=_parse_timestamp(row["lease_heartbeat_at"]),
        error_code=row["error_code"],
        blocked_capability=row["blocked_capability"],
        result_reference=row["result_reference"],
        result_sha256=row["result_sha256"],
        redrive_of=row["redrive_of"],
        attempt_history=tuple(
            AttemptRecord(
                attempt=int(item["attempt"]),
                started_at=_parse_timestamp(item["started_at"]),  # type: ignore[arg-type]
                finished_at=_parse_timestamp(item["finished_at"]),  # type: ignore[arg-type]
                outcome=str(item["outcome"]),
                error_code=item["error_code"],
            )
            for item in history
        ),
    )


def _export_task_record(task: QueueTask) -> dict[str, object]:
    return {
        "attempt_history": [
            {
                "attempt": item.attempt,
                "error_code": item.error_code,
                "finished_at": _timestamp(item.finished_at),
                "outcome": item.outcome,
                "started_at": _timestamp(item.started_at),
            }
            for item in task.attempt_history
        ],
        "attempts": task.attempts,
        "available_at": _timestamp(task.available_at),
        "blocked_capability": task.blocked_capability,
        "created_at": _timestamp(task.created_at),
        "dedupe_key": task.dedupe_key,
        "error_code": task.error_code,
        "handler_version": task.handler_version,
        "id": task.id,
        "input_hash": task.input_hash,
        "kind": task.kind,
        "last_attempt_at": (
            _timestamp(task.last_attempt_at) if task.last_attempt_at else None
        ),
        "payload": task.payload,
        "priority": task.priority,
        "redrive_of": task.redrive_of,
        "result_reference": task.result_reference,
        "result_sha256": task.result_sha256,
        "state": task.state,
        "updated_at": _timestamp(task.updated_at),
    }


# The queue still constructs this record through its original import alias.
_ExportedRows.__module__ = "memory_queue"
