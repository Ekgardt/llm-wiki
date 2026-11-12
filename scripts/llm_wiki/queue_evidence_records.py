"""Pure queue evidence representation; callers retain all authority and persistence."""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from llm_wiki.formats.canonical import canonical_json_bytes, sha256_bytes
from llm_wiki.formats.schema import validate_schema
from llm_wiki.queue_contracts import QueueOperationError
from llm_wiki.queue_timing import timestamp


@dataclass(frozen=True)
class CorruptEvidence:
    """The immutable bytes and digests one corrupt export is derived from."""

    raw: bytes
    history: bytes
    metadata: bytes
    raw_sha256: str
    history_sha256: str
    metadata_sha256: str
    disposition_key: str
    lineage_generation: int


def corrupt_metadata(task_id: str, row: Mapping[str, object]) -> bytes:
    return canonical_json_bytes(
        {
            "task_id": task_id,
            "kind": str(row["kind"]),
            "handler_version": int(row["handler_version"]),
            "input_hash": str(row["input_hash"]),
            "state": "dead",
            "error_code": row["error_code"],
            "lineage_generation": int(row["lineage_generation"]),
        }
    )


def corrupt_disposition_key(
    *,
    actor: str,
    history_sha256: str,
    metadata_sha256: str,
    raw_sha256: str,
    reason: str,
    task_id: str,
) -> str:
    """One key for exactly this task, these bytes, this actor and this reason."""
    return sha256_bytes(
        canonical_json_bytes(
            {
                "actor_identity": actor,
                "history_sha256": history_sha256,
                "metadata_sha256": metadata_sha256,
                "raw_sha256": raw_sha256,
                "reason": reason,
                "task_id": task_id,
            }
        )
    )


def corrupt_records_match(
    manifest_bytes: bytes,
    disposition_bytes: bytes,
    manifest: Mapping[str, object],
    record: Mapping[str, object],
    disposition: sqlite3.Row,
) -> bool:
    """The package's own records agree with the row that points at them."""
    if sha256_bytes(manifest_bytes) != disposition["manifest_sha256"]:
        return False
    if sha256_bytes(disposition_bytes) != disposition["disposition_sha256"]:
        return False
    task_id = disposition["task_id"]
    package_path = disposition["package_path"]
    named = (
        manifest["task_id"],
        record["task_id"],
        manifest["package_path"],
        record["package_path"],
    )
    return named == (task_id, task_id, package_path, package_path)


def corrupt_manifest(
    operation: sqlite3.Row,
    *,
    operation_id: str,
    task_id: str,
    disposition_key: str,
) -> dict[str, object]:
    """The manifest describing everything the export produced."""
    manifest = {
        "schema_version": "corrupt-task-manifest/v1",
        "operation_id": operation_id,
        "task_id": task_id,
        "package_key": disposition_key,
        "package_path": f"run/queue-results/corrupt-{disposition_key}",
        "raw_sha256": str(operation["raw_sha256"]),
        "history_sha256": str(operation["history_sha256"]),
        "metadata_sha256": str(operation["metadata_sha256"]),
        "lineage_generation": int(operation["lineage_generation"]),
        "link_count": int(operation["link_count"]),
        "page_count": int(operation["page_count"]),
        "rolling_root": str(operation["rolling_root"]),
    }
    validate_schema(
        manifest,
        (Path(__file__).parents[1] / "schemas") / "corrupt-task-manifest-v1.json",
    )
    return manifest


def corrupt_disposition_record(
    *,
    operation_id: str,
    task_id: str,
    disposition_key: str,
    manifest_sha256: str,
    active_link_digest: str,
    actor: str,
    reason: str,
    disposed_at: str,
) -> dict[str, object]:
    """The record that retires the task and points at its evidence."""
    record = {
        "schema_version": "corrupt-task-disposition/v1",
        "operation_id": operation_id,
        "task_id": task_id,
        "package_key": disposition_key,
        "package_path": f"run/queue-results/corrupt-{disposition_key}",
        "manifest_path": (
            f"run/queue-results/corrupt-{disposition_key}/manifest.json"
        ),
        "manifest_sha256": manifest_sha256,
        "active_link_digest": active_link_digest,
        "actor_identity": actor,
        "reason": reason,
        "disposed_at": disposed_at,
    }
    validate_schema(
        record,
        (Path(__file__).parents[1] / "schemas") / "corrupt-task-disposition-v1.json",
    )
    return record


def capture_resolution_record(
    *,
    task_id: str,
    supersedes_digest: str | None,
    observed: Mapping[str, object],
    selected_intent: Mapping[str, object] | None,
    actor: str,
    reason: str,
    now: datetime,
) -> dict[str, object]:
    """The schema-valid record one link resolution appends."""
    record = {
        "schema_version": "capture-task-link-resolution/v1",
        "task_id": task_id,
        "supersedes_digest": supersedes_digest,
        "observed": dict(observed),
        "selected_intent": (
            None if selected_intent is None else dict(selected_intent)
        ),
        "actor_identity": actor,
        "reason": reason,
        "created_at": timestamp(now),
    }
    validate_schema(
        record,
        (Path(__file__).parents[1] / "schemas")
        / "capture-task-link-resolution-v1.json",
    )
    return record


def purge_receipt_record(
    operation: sqlite3.Row,
    disposition: sqlite3.Row,
    *,
    operation_id: str,
    task_id: str,
) -> dict[str, object]:
    """The schema-valid receipt that closes one purge."""
    receipt = {
        "schema_version": "corrupt-purge/v1",
        "operation_id": operation_id,
        "task_id": task_id,
        "package_key": str(disposition["disposition_key"]),
        "package_path": str(disposition["package_path"]),
        "manifest_sha256": str(disposition["manifest_sha256"]),
        "disposition_sha256": str(disposition["disposition_sha256"]),
        "original_frozen_root": str(disposition["original_frozen_root"]),
        "purge_page_count": int(operation["page_count"]),
        "final_rolling_root": str(operation["rolling_root"]),
        "final_generation": int(operation["expected_generation"]),
        "observed_incoming_link_count": 0,
    }
    validate_schema(
        receipt,
        (Path(__file__).parents[1] / "schemas") / "corrupt-purge-v1.json",
    )
    return receipt


def capture_seal_digest(
    *,
    task_id: str,
    consumer_kind: str,
    consumer_id: str,
    active_link_digest: str,
) -> str:
    """One digest for exactly this consumer sealing exactly this link."""
    return sha256_bytes(
        canonical_json_bytes(
            {
                "active_digest": active_link_digest,
                "consumer_id": consumer_id,
                "consumer_kind": consumer_kind,
                "task_id": task_id,
            }
        )
    )


def require_identical_seal(
    existing: sqlite3.Row,
    *,
    consumer_kind: str,
    consumer_id: str,
    active_link_digest: str,
    seal_digest: str,
) -> None:
    """A second seal is a no-op only when it is the very same seal."""
    stored = (
        existing["active_digest"],
        existing["consumer_kind"],
        existing["consumer_id"],
        existing["seal_digest"],
    )
    if stored != (active_link_digest, consumer_kind, consumer_id, seal_digest):
        raise QueueOperationError("capture_link_sealed")


# Existing queue callers and serialized values retain their original lookup.
CorruptEvidence.__module__ = "memory_queue"
