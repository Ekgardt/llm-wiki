"""Whether a purge export or a corrupt-task package still proves what it recorded."""
from __future__ import annotations

import json
import sqlite3
import stat
from collections.abc import Mapping
from pathlib import Path

from llm_wiki.formats.canonical import sha256_bytes
from llm_wiki.formats.queue_payload import MAX_QUEUE_PAYLOAD_BYTES
from llm_wiki.formats.schema import validate_schema
from llm_wiki.queue_artifacts import is_owner_only, read_stable_owner_file
from llm_wiki.queue_contracts import MAX_RESULT_BYTES, QueueOperationError
from llm_wiki.queue_evidence_records import corrupt_records_match

# Basis: a read bound, not a sizing; a damaged or hostile export file is refused
# before it is held in memory. The value predates this module and was not measured.
MAX_EXPORT_METADATA_BYTES = 64 * 1024 * 1024


def _owner_only_directory(package: Path) -> bool:
    """A real, owner-only directory — not a link, a reparse point, or a file."""
    metadata = package.lstat()
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return (
        not stat.S_ISLNK(metadata.st_mode)
        and not (getattr(metadata, "st_file_attributes", 0) & reparse_flag)
        and stat.S_ISDIR(metadata.st_mode)
        and is_owner_only(package)
    )


def _validated_corrupt_records(
    package: Path,
) -> tuple[bytes, bytes, dict[str, object], dict[str, object]]:
    """The manifest and disposition bytes, and the objects they parse into."""
    schemas = Path(__file__).parents[1] / "schemas"
    manifest_bytes = read_stable_owner_file(package / "manifest.json", 64 * 1024)
    disposition_bytes = read_stable_owner_file(
        package / "disposition.json", 64 * 1024
    )
    manifest = json.loads(manifest_bytes.decode("utf-8", errors="strict"))
    record = json.loads(disposition_bytes.decode("utf-8", errors="strict"))
    validate_schema(manifest, schemas / "corrupt-task-manifest-v1.json")
    validate_schema(record, schemas / "corrupt-task-disposition-v1.json")
    return manifest_bytes, disposition_bytes, manifest, record


def corrupt_package_trustworthy(package: Path, disposition: sqlite3.Row) -> bool:
    """Whether the exported package still proves what the disposition recorded."""
    if not _owner_only_directory(package):
        return False
    records = _validated_corrupt_records(package)
    return corrupt_records_match(
        *records, disposition
    ) and _corrupt_payload_matches(package, records[2], disposition)


def _corrupt_payload_matches(
    package: Path, manifest: Mapping[str, object], disposition: sqlite3.Row
) -> bool:
    """The exported bytes still hash to what the manifest recorded."""
    raw = read_stable_owner_file(package / "payload.bin", MAX_QUEUE_PAYLOAD_BYTES)
    history = read_stable_owner_file(
        package / "attempt-history.json", MAX_EXPORT_METADATA_BYTES
    )
    metadata = read_stable_owner_file(
        package / "task-metadata.json", MAX_EXPORT_METADATA_BYTES
    )
    digests = (sha256_bytes(raw), sha256_bytes(history), sha256_bytes(metadata))
    expected = (
        manifest["raw_sha256"],
        manifest["history_sha256"],
        manifest["metadata_sha256"],
    )
    if digests != expected:
        return False
    return manifest["rolling_root"] == disposition["original_frozen_root"]


def verify_corrupt_package_bytes(
    package: Path,
    *,
    raw: bytes,
    history: bytes,
    metadata: bytes,
    disposition_bytes: bytes,
) -> None:
    """Every file in the package still holds exactly what we wrote."""
    observed = (
        read_stable_owner_file(package / "payload.bin", MAX_QUEUE_PAYLOAD_BYTES),
        read_stable_owner_file(
            package / "attempt-history.json", MAX_EXPORT_METADATA_BYTES
        ),
        read_stable_owner_file(
            package / "task-metadata.json", MAX_EXPORT_METADATA_BYTES
        ),
        read_stable_owner_file(package / "disposition.json", 64 * 1024),
    )
    if observed != (raw, history, metadata, disposition_bytes):
        raise QueueOperationError("corrupt_export_verification_failed")


def _restore_json_object(raw: bytes) -> dict[str, object]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise QueueOperationError("restore_manifest_invalid", "manifest is not JSON") from None
    if not isinstance(value, dict):
        raise QueueOperationError("restore_manifest_invalid", "manifest is not an object")
    return value


def verified_export_records(export: Path) -> list[dict[str, object]]:
    """Read one purge export only when its manifest and digests all verify."""
    manifest = _restore_json_object(
        read_stable_owner_file(export / "manifest.json", MAX_EXPORT_METADATA_BYTES)
    )
    records_bytes = read_stable_owner_file(
        export / "records.json", MAX_EXPORT_METADATA_BYTES
    )
    if sha256_bytes(records_bytes) != manifest.get("records_sha256"):
        raise QueueOperationError("restore_verification_failed", "records digest mismatch")
    _verify_exported_results(export, manifest)
    records = json.loads(records_bytes.decode("utf-8"))
    _require_record_list(records, manifest)
    return records


def _is_object_list(records: object) -> bool:
    if not isinstance(records, list):
        return False
    return all(isinstance(item, dict) for item in records)


def _manifest_task_ids(manifest: Mapping[str, object]) -> list[str]:
    declared = manifest.get("task_ids") or []
    return [str(item) for item in declared]


def _require_record_list(records: object, manifest: Mapping[str, object]) -> None:
    if not _is_object_list(records):
        raise QueueOperationError(
            "restore_manifest_invalid", "records are not a list of objects"
        )
    exported = [str(item.get("id")) for item in records]
    if exported != _manifest_task_ids(manifest):
        raise QueueOperationError("restore_verification_failed", "task ids do not match")


def _verify_exported_results(export: Path, manifest: Mapping[str, object]) -> None:
    for item in manifest.get("results") or []:
        if not isinstance(item, Mapping):
            raise QueueOperationError(
                "restore_manifest_invalid", "result entry is not an object"
            )
        data = read_stable_owner_file(
            export / "results" / f"{item.get('id')}.result", MAX_RESULT_BYTES
        )
        if sha256_bytes(data) != item.get("sha256"):
            raise QueueOperationError("restore_verification_failed", "result digest mismatch")
