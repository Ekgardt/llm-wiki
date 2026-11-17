"""Receipt syntax and source identity; validity is not publication authority."""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path

from .canonical import canonical_json_bytes, sha256_bytes
from .schema import validate_schema
from .source import SourceDescriptor

COMPILE_RECEIPT_SCHEMA = Path(__file__).resolve().parents[2] / "schemas" / "compile-receipt-v2.json"


COMPILE_RECEIPT_V3_SCHEMA = Path(__file__).resolve().parents[2] / "schemas" / "compile-receipt-v3.json"


COMPILE_RECEIPT_V4_SCHEMA = Path(__file__).resolve().parents[2] / "schemas" / "compile-receipt-v4.json"


def compile_source_identity(logical_path: str, source_sha256: str) -> str:
    SourceDescriptor(logical_path, 0, source_sha256).canonical()
    return sha256_bytes(canonical_json_bytes([logical_path, source_sha256]))


def corrupt_receipt(reason: BaseException, path: Path | None = None) -> ValueError:
    """Say which receipt failed and why, not merely that one did.

    The bare message was the same for four different causes, so the only way to
    learn what happened was to reproduce it through the reader. Receipt paths
    and these reasons are our own text, never page content.
    """
    named = "" if path is None else f" {path.name}"
    return ValueError(f"compile receipt is corrupt{named}: {reason}")


def parse_compile_receipt_v2(raw_bytes: bytes, digest: str) -> dict[str, object]:
    """Validate canonical receipt bytes without requiring live transaction state."""
    try:
        return _parsed_receipt_v2(raw_bytes, digest)
    except (
        IndexError,
        KeyError,
        TypeError,
        ValueError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        raise corrupt_receipt(exc) from exc


def _parsed_receipt_v2(raw_bytes: bytes, digest: str) -> dict[str, object]:
    text = raw_bytes.decode("utf-8", errors="strict")
    frontmatter, body = text.split("---\n", 2)[1:]
    prefix = "\n# Compile Receipt\n\nOne-sentence summary: This immutable receipt proves completion of a snapshot compile.\n\n## Record\n```json\n"
    fields = _receipt_frontmatter(frontmatter)
    _require_v2_frontmatter(fields)
    record = _receipt_record(body, prefix, COMPILE_RECEIPT_SCHEMA)
    _require_v2_agreement(fields, record, digest)
    _require_v2_identity(record, digest)
    _require_v2_evidence_scope(record, digest)
    return record


def _receipt_frontmatter(frontmatter: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in frontmatter.splitlines():
        key, separator, value = line.partition(": ")
        if not separator or key in fields:
            raise ValueError("compile receipt frontmatter is invalid")
        fields[key] = value
    return fields


def _receipt_record(body: str, prefix: str, schema: object) -> dict[str, object]:
    """The one canonical JSON record a receipt body is allowed to carry."""
    if not body.startswith(prefix) or not body.endswith("\n```\n"):
        raise ValueError("compile receipt body is invalid")
    canonical = body[len(prefix) : -5]
    record = json.loads(canonical)
    validate_schema(record, schema)
    if canonical_json_bytes(record).decode() != canonical:
        raise ValueError("compile receipt record is not canonical")
    return record


def _require_v2_frontmatter(fields: Mapping[str, str]) -> None:
    if set(fields) != {
        "type", "source_digest", "action_key", "status", "timestamp",
        "confidence", "source_authority"
    }:
        raise ValueError("compile receipt frontmatter fields are invalid")
    timestamp = datetime.fromisoformat(fields["timestamp"])
    if timestamp.tzinfo is None:
        raise ValueError("compile receipt timestamp must include a timezone")


def _require_v2_agreement(
    fields: Mapping[str, str], record: Mapping[str, object], digest: str
) -> None:
    expected = {
        "type": "compile-receipt",
        "source_digest": digest,
        "action_key": record["action_key"],
        "status": record["state"],
        "timestamp": record["completed_at"],
        "confidence": "high",
        "source_authority": "ai-derived",
    }
    if fields != expected or record["source_digest"] != digest:
        raise ValueError("compile receipt frontmatter and record disagree")


def _require_v2_identity(record: Mapping[str, object], digest: str) -> None:
    input_digests = record["input_digests"]
    if input_digests != sorted(set(input_digests)) or digest not in input_digests:
        raise ValueError("compile receipt input digests are invalid")
    expected_operation_id = "compile:" + sha256_bytes(
        canonical_json_bytes(
            {"action_key": record["action_key"], "source_digests": input_digests}
        )
    )
    if record["operation_id"] != expected_operation_id:
        raise ValueError("compile receipt operation identity is invalid")


def _require_v2_evidence_scope(record: Mapping[str, object], digest: str) -> None:
    operation_paths = [operation["path"] for operation in record["operations"]]
    known = set(operation_paths)
    if len(operation_paths) != len(known):
        raise ValueError("compile receipt operation paths are duplicated")
    for evidence in record["evidence"]:
        _require_v2_evidence_entry(evidence, known, digest)


def _require_v2_evidence_entry(
    evidence: Mapping[str, str], operation_paths: set[str], digest: str
) -> None:
    if (
        evidence["source_digest"] != digest
        or evidence["operation_path"] not in operation_paths
    ):
        raise ValueError("compile receipt evidence scope is invalid")


def compile_operation_id(
    action_key: str,
    batch_manifest_sha256: str,
    dispositions: Sequence[Mapping[str, str]],
) -> str:
    return "compile:" + sha256_bytes(
        canonical_json_bytes(
            {
                "action_key": action_key,
                "batch_manifest_sha256": batch_manifest_sha256,
                "dispositions": list(dispositions),
            }
        )
    )


def compile_context_source_identity(source: Mapping[str, object]) -> str:
    return sha256_bytes(canonical_json_bytes({
        "logical_path": source["logical_path"], "sha256": source["sha256"],
        "original_sha256": source["original_sha256"],
        "original_byte_size": source["original_byte_size"],
        "byte_start": source["byte_start"], "byte_end": source["byte_end"],
    }))


def require_v4_descriptor(source: Mapping[str, object]) -> None:
    start, end = source["byte_start"], source["byte_end"]
    if not (0 <= start <= end <= source["original_byte_size"]):
        raise ValueError("compile source context bounds are invalid")
    if end - start != source["byte_size"]:
        raise ValueError("compile source context size disagrees")


def parse_compile_receipt_v3(
    raw_bytes: bytes, *, logical_path: str, source_sha256: str
) -> dict[str, object]:
    try:
        return _parsed_receipt_v3(raw_bytes, logical_path, source_sha256)
    except (
        IndexError,
        KeyError,
        TypeError,
        ValueError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        raise corrupt_receipt(exc) from exc


def _parsed_receipt_v3(
    raw_bytes: bytes, logical_path: str, source_sha256: str
) -> dict[str, object]:
    source_identity = compile_source_identity(logical_path, source_sha256)
    text = raw_bytes.decode("utf-8", errors="strict")
    frontmatter, body = text.split("---\n", 2)[1:]
    prefix = (
        "\n# Compile Receipt\n\n"
        "One-sentence summary: This immutable receipt proves completion of a snapshot compile.\n\n"
        "## Record\n```json\n"
    )
    _require_v3_frontmatter(_receipt_frontmatter(frontmatter), source_identity)
    record = _receipt_record(body, prefix, COMPILE_RECEIPT_V3_SCHEMA)
    _require_v3_source(record, source_identity, logical_path, source_sha256)
    _require_v3_manifest(record)
    _require_v3_identity(record)
    _require_v3_evidence_scope(record, source_identity, logical_path, source_sha256)
    return record


def _require_v3_frontmatter(fields: Mapping[str, str], source_identity: str) -> None:
    if fields != {
        "type": "compile-receipt",
        "schema_version": "compile-receipt/v3",
        "source_identity": source_identity,
        "status": "completed",
        "confidence": "high",
        "source_authority": "ai-derived",
    }:
        raise ValueError("compile receipt frontmatter fields are invalid")


def _require_v3_source(
    record: Mapping[str, object],
    source_identity: str,
    logical_path: str,
    source_sha256: str,
) -> None:
    source = record["source"]
    if (
        record["source_identity"] != source_identity
        or source["logical_path"] != logical_path
        or source["sha256"] != source_sha256
    ):
        raise ValueError("compile receipt source identity disagrees")


def _require_v3_manifest(record: Mapping[str, object]) -> None:
    manifest = record["batch_manifest"]
    _require_sorted_manifest(manifest)
    if sha256_bytes(canonical_json_bytes(manifest)) != record["batch_manifest_sha256"]:
        raise ValueError("compile receipt manifest digest disagrees")
    _require_complete_dispositions(record, manifest)


def _require_sorted_manifest(manifest: Sequence[Mapping[str, str]]) -> None:
    if manifest != sorted(manifest, key=lambda item: item["logical_path"]):
        raise ValueError("compile receipt manifest is not sorted")


def _require_complete_dispositions(
    record: Mapping[str, object], manifest: Sequence[Mapping[str, str]]
) -> None:
    identities = sorted(
        compile_source_identity(item["logical_path"], item["sha256"])
        for item in manifest
    )
    if [item["source_identity"] for item in record["dispositions"]] != identities:
        raise ValueError("compile receipt dispositions are incomplete")


def _require_v3_identity(record: Mapping[str, object]) -> None:
    if record["operation_id"] != compile_operation_id(
        record["action_key"],
        record["batch_manifest_sha256"],
        record["dispositions"],
    ):
        raise ValueError("compile receipt operation identity is invalid")


def _require_v3_evidence_scope(
    record: Mapping[str, object],
    source_identity: str,
    logical_path: str,
    source_sha256: str,
) -> None:
    operation_paths = {item["path"] for item in record["operations"]}
    if len(operation_paths) != len(record["operations"]):
        raise ValueError("compile receipt operation paths are duplicated")
    for evidence in record["evidence"]:
        _require_v3_evidence_entry(
            evidence, operation_paths, source_identity, logical_path, source_sha256
        )


def _require_v3_evidence_entry(
    evidence: Mapping[str, str],
    operation_paths: set[str],
    source_identity: str,
    logical_path: str,
    source_sha256: str,
) -> None:
    if (
        evidence["source_identity"] != source_identity
        or evidence["source_path"] != logical_path
        or evidence["source_digest"] != source_sha256
        or evidence["operation_path"] not in operation_paths
    ):
        raise ValueError("compile receipt evidence scope is invalid")


def parse_compile_receipt_v4(
    raw_bytes: bytes, *, logical_path: str, source_sha256: str
) -> dict[str, object]:
    try:
        return _parsed_receipt_v4(raw_bytes, logical_path, source_sha256)
    except (IndexError, KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise corrupt_receipt(exc) from exc


def _parsed_receipt_v4(raw_bytes: bytes, logical_path: str, source_sha256: str):
    frontmatter, body = raw_bytes.decode("utf-8", errors="strict").split("---\n", 2)[1:]
    prefix = ("\n# Compile Receipt\n\n"
              "One-sentence summary: This immutable receipt proves completion of a snapshot compile.\n\n"
              "## Record\n```json\n")
    record = _receipt_record(body, prefix, COMPILE_RECEIPT_V4_SCHEMA)
    identity = compile_context_source_identity(record["source"])
    _require_v4_frontmatter(_receipt_frontmatter(frontmatter), identity)
    _require_v3_source(record, identity, logical_path, source_sha256)
    require_v4_manifest(record)
    _require_v3_identity(record)
    _require_v3_evidence_scope(record, identity, logical_path, source_sha256)
    return record


def _require_v4_frontmatter(fields, identity):
    expected = {
        "type": "compile-receipt", "schema_version": "compile-receipt/v4",
        "source_identity": identity, "status": "completed",
        "confidence": "high", "source_authority": "ai-derived",
    }
    if fields != expected:
        raise ValueError("compile receipt frontmatter fields are invalid")


def require_v4_manifest(record):
    manifest = record["batch_manifest"]
    _require_sorted_v4_manifest(manifest)
    _require_v4_manifest_sources(record, manifest)
    if sha256_bytes(canonical_json_bytes(manifest)) != record["batch_manifest_sha256"]:
        raise ValueError("compile receipt manifest digest disagrees")
    identities = sorted(compile_context_source_identity(item) for item in manifest)
    if [item["source_identity"] for item in record["dispositions"]] != identities:
        raise ValueError("compile receipt dispositions are incomplete")


def _require_sorted_v4_manifest(manifest):
    ordered = sorted(manifest, key=lambda item: (item["logical_path"], item["byte_start"]))
    if manifest != ordered:
        raise ValueError("compile receipt manifest is not sorted")


def _require_v4_manifest_sources(record, manifest):
    require_packing_manifest_version(record, manifest)
    identities = [compile_context_source_identity(item) for item in manifest]
    if len(identities) != len(set(identities)) or record["source"] not in manifest:
        raise ValueError("compile receipt source manifest is invalid")
    for source in manifest:
        require_v4_descriptor(source)
    _require_manifest_part_groups(manifest)


def require_packing_manifest_version(record, manifest):
    if record["packing"]["algorithm"] != "compile-complete-items/v1":
        return
    paths = [source["logical_path"] for source in manifest]
    if len(paths) != len(set(paths)):
        raise ValueError("historical packing v1 requires unique source paths")


def _require_manifest_part_groups(manifest):
    grouped = {}
    for source in manifest:
        grouped.setdefault(source["logical_path"], []).append(source)
    for sources in grouped.values():
        _require_manifest_group(sources)


def _require_manifest_group(sources):
    if len(sources) == 1:
        return
    ordered = sorted(sources, key=lambda source: source["byte_start"])
    if len(_manifest_physical_contexts(ordered)) != 1 or _manifest_discontinuous(ordered):
        raise ValueError("compile receipt native source parts overlap, have gaps or disagree")


def _manifest_physical_contexts(sources):
    return {(source["original_sha256"], source["original_byte_size"]) for source in sources}


def _manifest_discontinuous(sources):
    return any(left["byte_end"] != right["byte_start"] for left, right in zip(sources, sources[1:]))


def parse_compile_receipt_version(raw_bytes, *, logical_path, source_sha256):
    try:
        reader = receipt_version_reader(raw_bytes)
        return reader(raw_bytes, logical_path=logical_path, source_sha256=source_sha256)
    except UnsupportedCompileReceiptVersion:
        raise
    except (IndexError, KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise corrupt_receipt(exc) from exc


class UnsupportedCompileReceiptVersion(ValueError):
    """An older reader must preserve evidence written by a newer version."""


def receipt_version_reader(raw_bytes):
    record = json.loads(raw_bytes.split(b"```json\n", 1)[1].split(b"\n```", 1)[0])
    if not isinstance(record, dict):
        raise ValueError("compile receipt record must be an object")
    readers = {"compile-receipt/v3": parse_compile_receipt_v3,
               "compile-receipt/v4": parse_compile_receipt_v4}
    reader = readers.get(record.get("schema_version"))
    if reader is None:
        raise UnsupportedCompileReceiptVersion("unsupported compile receipt version")
    return reader


def receipt_source_fields(raw: bytes) -> tuple[str, str] | None:
    """The source a receipt claims, read from the receipt itself."""
    try:
        payload = json.loads(raw.split(b"```json", 1)[1].split(b"```", 1)[0])
        source = payload["source"]
        return str(source["logical_path"]), str(source["sha256"])
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
