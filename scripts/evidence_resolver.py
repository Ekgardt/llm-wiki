"""Resolve content-addressed daily evidence from flat files or sealed bags."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from bounded_io import read_stable_bytes
from reliable_memory import canonical_json_bytes, sha256_bytes, validate_schema

MAX_DAILY_BYTES = 16 * 1024 * 1024
MAX_GROUNDED_SOURCE_BYTES = 8 * 1024 * 1024
MAX_ARCHIVE_MANIFEST_BYTES = 1024 * 1024
MAX_TAG_FILE_BYTES = 1024 * 1024
MAX_BAGS_PER_MONTH = 10_000
MAX_DIRECTORY_ENTRIES = 10_000
ARCHIVE_SCHEMA = Path(__file__).with_name("schemas") / "archive-manifest-v1.json"
_DAILY_ID_PATTERN = r"\d{4}-\d{2}-\d{2}"
_SHA256_PATTERN = r"[0-9a-f]{64}"
_BLOCK_ID_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}"
_BLOCK_ID_RE = re.compile(_BLOCK_ID_PATTERN)
_REF_RE = re.compile(
    rf"daily:(?P<daily>{_DAILY_ID_PATTERN}) "
    rf"sha256:(?P<sha>{_SHA256_PATTERN}) "
    rf"block:(?P<block>{_BLOCK_ID_PATTERN}) "
    r"bytes:(?P<start>0|[1-9]\d*)-(?P<end>0|[1-9]\d*)"
)
_HEADER_RE = re.compile(rb"(?m)^## \[([^\]\r\n]+)\][^\r\n]*(?:\r?\n|$)")
_MARKER_RE = re.compile(
    rb"(?m)^<!-- llm-wiki-operation:[0-9a-f]+ -->[^\r\n]*(?:\r?\n|$)"
)
_MARKER_ID_RE = re.compile(
    r"^(?:[-+*]\s+)?`?\[((?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d)\]"
)
_HASH_LINE_RE = re.compile(r"([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._/-]*)\n")


class EvidenceResolutionError(ValueError):
    """Evidence could not be proven from one unambiguous immutable source."""


def _require_daily_id(daily_id: object) -> None:
    if not isinstance(daily_id, str) or re.fullmatch(_DAILY_ID_PATTERN, daily_id) is None:
        raise ValueError("evidence daily ID is invalid")
    try:
        if date.fromisoformat(daily_id).isoformat() != daily_id:
            raise ValueError
    except ValueError as exc:
        raise ValueError("evidence daily ID is invalid") from exc


def _require_source_digest(digest: object) -> None:
    if not isinstance(digest, str) or re.fullmatch(_SHA256_PATTERN, digest) is None:
        raise ValueError("evidence SHA-256 is invalid")


def _require_block_identifier(block_id: object) -> None:
    if not isinstance(block_id, str) or _BLOCK_ID_RE.fullmatch(block_id) is None:
        raise ValueError("evidence block ID is invalid")


def _is_plain_int(value: object) -> bool:
    """A bool is an int in Python, and never a byte offset here."""
    return isinstance(value, int) and not isinstance(value, bool)


def _require_byte_span(start: object, end: object) -> None:
    if not _is_plain_int(start) or not _is_plain_int(end):
        raise ValueError("evidence byte span must be non-empty and half-open")
    if start < 0 or start >= end:
        raise ValueError("evidence byte span must be non-empty and half-open")


@dataclass(frozen=True)
class EvidenceRef:
    daily_id: str
    source_sha256: str
    block_id: str
    byte_start: int
    byte_end: int

    def __post_init__(self) -> None:
        _require_daily_id(self.daily_id)
        _require_source_digest(self.source_sha256)
        _require_block_identifier(self.block_id)
        _require_byte_span(self.byte_start, self.byte_end)

    @classmethod
    def parse(cls, value: str) -> EvidenceRef:
        if not isinstance(value, str):
            raise TypeError("evidence reference must be a string")
        match = _REF_RE.fullmatch(value)
        if match is None:
            raise ValueError("evidence reference is not canonical")
        return cls(
            match["daily"],
            match["sha"],
            match["block"],
            int(match["start"]),
            int(match["end"]),
        )

    def __str__(self) -> str:
        return (
            f"daily:{self.daily_id} sha256:{self.source_sha256} "
            f"block:{self.block_id} bytes:{self.byte_start}-{self.byte_end}"
        )


@dataclass(frozen=True)
class ResolvedEvidence:
    reference: EvidenceRef
    bytes: bytes
    sha256: str
    source_sha256: str
    block_sha256: str
    byte_start: int
    byte_end: int
    line_start: int
    line_end: int
    location: str
    source_path: Path


@dataclass(frozen=True)
class ValidatedBag:
    path: Path
    manifest: dict[str, object]
    payload_path: Path
    payload: bytes


def _regular_directory(path: Path, *, label: str) -> None:
    info = path.lstat()
    if (
        path.is_symlink()
        or getattr(info, "st_file_attributes", 0) & 0x400
        or not stat.S_ISDIR(info.st_mode)
    ):
        raise PermissionError(f"{label} must be a regular non-symlink directory")


def bounded_directory_entries(
    path: Path, max_entries: int, *, label: str
) -> list[Path]:
    entries: list[Path] = []
    for entry in Path(path).iterdir():
        entries.append(entry)
        if len(entries) > max_entries:
            raise EvidenceResolutionError(f"{label} exceeds the entry scan limit")
    return entries


def _archive_path_is_read_only(path: Path) -> bool:
    if os.name == "nt":
        return _windows_path_is_read_only(path)
    expected = 0o500 if path.is_dir() else 0o400
    try:
        return stat.S_IMODE(path.stat(follow_symlinks=False).st_mode) == expected
    except OSError:
        return False


def _windows_path_is_read_only(path: Path) -> bool:
    from markdown_transaction import (
        _acl_output_text,
        _run_acl_command,
        _windows_acl_identity,
    )

    verified = _run_acl_command(["icacls", str(path)])
    if verified.returncode != 0:
        return False
    return _acl_is_owner_read_only(
        _acl_output_text(verified.stdout), _windows_acl_identity()
    )


def _acl_is_owner_read_only(acl: str, identity: str) -> bool:
    lines = _acl_entry_lines(acl)
    if not lines or _acl_grants_write(acl):
        return False
    return all(identity.casefold() in line.casefold() for line in lines)


def _acl_entry_lines(acl: str) -> list[str]:
    return [line.strip() for line in acl.splitlines() if ":(" in line]


def _acl_grants_write(acl: str) -> bool:
    return any(marker in acl for marker in ("(F)", "(M)", "(W)"))


def _parse_hash_file(raw: bytes, *, label: str) -> dict[str, str]:
    text = _decoded_tag_file(raw, label=label)
    position = 0
    result: dict[str, str] = {}
    while position < len(text):
        match = _HASH_LINE_RE.match(text, position)
        _require_hash_line(match, result, label=label)
        assert match is not None
        result[match[2]] = match[1]
        position = match.end()
    if not result:
        raise EvidenceResolutionError(f"{label} is empty")
    return result


def _decoded_tag_file(raw: bytes, *, label: str) -> str:
    try:
        return raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise EvidenceResolutionError(f"{label} is not UTF-8") from exc


def _require_hash_line(
    match: re.Match[str] | None, result: dict[str, str], *, label: str
) -> None:
    if match is None or match[2] in result or ".." in Path(match[2]).parts:
        raise EvidenceResolutionError(f"{label} is not canonical")


def daily_entries(content: bytes) -> list[tuple[str, int, int]]:
    """Every entry in a daily log, as (block id, start, end).

    An entry starts at a `## [id]` heading or at an `<!-- llm-wiki-operation: -->`
    marker, and ends where the next entry starts. A heading declares its id in
    the heading; a captured entry declares it at the head of its first content
    line, which is where the capture writers put the time. An entry that
    declares no usable id is not citable and is left out.

    This is the one definition of an entry: the evidence resolver, the archive
    packager and the claim pipeline all read it from here. See
    `knowledge/notes/daily-entry-boundary-decision.md`.
    """
    starts = _entry_starts(content)
    ends = [item[0] for item in starts[1:]] + [len(content)]
    entries = [
        (_entry_id(content[start:end], declared), start, end)
        for (start, declared), end in zip(starts, ends)
    ]
    return [(item[0], item[1], item[2]) for item in entries if item[0] is not None]


def _entry_starts(content: bytes) -> list[tuple[int, str | None]]:
    """Where each entry begins, with the id its own delimiter declares."""
    headings = [
        (match.start(), _heading_block_id(match[1]))
        for match in _HEADER_RE.finditer(content)
    ]
    markers = [(match.start(), None) for match in _MARKER_RE.finditer(content)]
    return sorted(headings + markers, key=lambda item: item[0])


def _heading_block_id(raw: bytes) -> str:
    try:
        block_id = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise EvidenceResolutionError("daily block ID is not UTF-8") from exc
    if _BLOCK_ID_RE.fullmatch(block_id) is None:
        raise EvidenceResolutionError("daily block ID is invalid")
    return block_id


def _entry_id(entry: bytes, declared: str | None) -> str | None:
    if declared is not None:
        return declared
    return _marker_entry_id(entry)


def _marker_entry_id(entry: bytes) -> str | None:
    """A captured entry declares its time at the head of its first content line."""
    for raw in entry.decode("utf-8", errors="replace").splitlines()[1:]:
        line = raw.strip()
        if not line:
            continue
        match = _MARKER_ID_RE.match(line)
        return match.group(1) if match else None
    return None




def _line_span(content: bytes, start: int, end: int) -> tuple[int, int]:
    return content[:start].count(b"\n") + 1, content[: end - 1].count(b"\n") + 2


def _coordinator_record_document(record: object) -> dict[str, object]:
    return {
        "transaction_id": record.id,
        "operation_id": record.operation_id,
        "state": record.state,
        "operations": [
            {
                "kind": item.kind,
                "path": item.path,
                "before_hash": item.before_hash,
                "after_hash": item.after_hash,
            }
            for item in record.operations
        ],
        "preconditions": record.preconditions,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "parent_transaction_id": record.parent_transaction_id,
        "error_code": record.error_code,
    }


def compile_authority_attestation(record: object, sequence: int) -> dict[str, object]:
    if record.state != "committed" or not isinstance(sequence, int) or sequence < 1:
        raise ValueError("compile transaction authority is not committed")
    coordinator_record = _coordinator_record_document(record)
    return {
        "schema": "archive-compile-authority/v1",
        "transaction_id": record.id,
        "state": record.state,
        "committed_at": record.updated_at,
        "commit_sequence": sequence,
        "operation_ids": [record.operation_id],
        "coordinator_record": coordinator_record,
        "coordinator_record_digest": sha256_bytes(
            canonical_json_bytes(coordinator_record)
        ),
    }


def _validate_compile_authority(
    authority: object,
    receipt: dict[str, object],
    coordinator: object | None,
    *,
    receipt_path: str,
    receipt_hash: str,
) -> None:
    _require_authority_fields(authority)
    assert isinstance(authority, dict)
    _require_authority_schema(authority)
    _require_authority_sequence(authority["commit_sequence"])
    _require_authority_identity(authority, receipt)
    _require_authority_record(authority, receipt)
    operations = _authority_operations(authority["coordinator_record"])
    _require_authority_binding(operations, receipt, receipt_path, receipt_hash)
    _require_authority_coordinator(authority, receipt, coordinator)


_AUTHORITY_FIELDS = frozenset(
    {
        "schema",
        "transaction_id",
        "state",
        "committed_at",
        "commit_sequence",
        "operation_ids",
        "coordinator_record",
        "coordinator_record_digest",
    }
)

_COORDINATOR_RECORD_FIELDS = frozenset(
    {
        "transaction_id",
        "operation_id",
        "state",
        "operations",
        "preconditions",
        "created_at",
        "updated_at",
        "parent_transaction_id",
        "error_code",
    }
)


def _require_authority_fields(authority: object) -> None:
    if not isinstance(authority, dict):
        raise EvidenceResolutionError("archive compile authority is missing")
    if set(authority) != _AUTHORITY_FIELDS:
        raise EvidenceResolutionError("archive compile authority fields are invalid")


def _authority_committed_at(raw: object) -> datetime:
    text = str(raw)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise EvidenceResolutionError(
            "archive compile authority time is invalid"
        ) from exc


def _require_authority_schema(authority: Mapping[str, object]) -> None:
    committed_at = _authority_committed_at(authority["committed_at"])
    if (
        authority["schema"] != "archive-compile-authority/v1"
        or authority["state"] != "committed"
        or committed_at.tzinfo is None
    ):
        raise EvidenceResolutionError("archive compile authority is invalid")


def _require_authority_sequence(sequence: object) -> None:
    if not _is_plain_int(sequence) or sequence < 1:
        raise EvidenceResolutionError("archive compile authority is invalid")


def _require_authority_identity(
    authority: Mapping[str, object], receipt: Mapping[str, object]
) -> None:
    if (
        authority["operation_ids"] != [receipt["operation_id"]]
        or not isinstance(authority["coordinator_record"], dict)
        or re.fullmatch(_SHA256_PATTERN, str(authority["coordinator_record_digest"]))
        is None
    ):
        raise EvidenceResolutionError("archive compile authority is invalid")


def _require_authority_record(
    authority: Mapping[str, object], receipt: Mapping[str, object]
) -> None:
    record = authority["coordinator_record"]
    assert isinstance(record, dict)
    if set(record) != _COORDINATOR_RECORD_FIELDS or not isinstance(
        record["operations"], list
    ):
        raise EvidenceResolutionError("archive compile authority record is invalid")
    if _record_disagrees(record, authority, receipt):
        raise EvidenceResolutionError("archive compile authority record is invalid")


def _record_disagrees(
    record: Mapping[str, object],
    authority: Mapping[str, object],
    receipt: Mapping[str, object],
) -> bool:
    return (
        sha256_bytes(canonical_json_bytes(record))
        != authority["coordinator_record_digest"]
        or record["transaction_id"] != authority["transaction_id"]
        or record["operation_id"] != receipt["operation_id"]
        or record["state"] != authority["state"]
        or record["updated_at"] != authority["committed_at"]
    )


def _authority_operations(record: object) -> dict[object, object]:
    assert isinstance(record, dict)
    operations = {
        item.get("path"): item
        for item in record["operations"]
        if isinstance(item, dict)
    }
    if len(operations) != len(record["operations"]):
        raise EvidenceResolutionError("archive compile authority operations are invalid")
    return operations


def _require_authority_binding(
    operations: Mapping[object, object],
    receipt: Mapping[str, object],
    receipt_path: str,
    receipt_hash: str,
) -> None:
    receipt_operation = operations.get(receipt_path)
    if receipt_operation is None or receipt_operation.get("after_hash") != receipt_hash:
        raise EvidenceResolutionError(
            "archive compile authority operation binding failed"
        )
    if any(_operation_unbound(operations, item) for item in receipt["operations"]):
        raise EvidenceResolutionError(
            "archive compile authority operation binding failed"
        )


def _operation_unbound(
    operations: Mapping[object, object], item: Mapping[str, object]
) -> bool:
    recorded = operations.get(item["path"], {})
    return (
        recorded.get("kind") != item["kind"]
        or recorded.get("after_hash") != item["after_sha256"]
    )


def _require_authority_transaction(
    coordinator: object, receipt: Mapping[str, object]
) -> object:
    transaction = coordinator._record_for_operation_id(str(receipt["operation_id"]))
    if transaction is None:
        raise EvidenceResolutionError("archive compile authority transaction is missing")
    return transaction


def _require_authority_coordinator(
    authority: Mapping[str, object],
    receipt: Mapping[str, object],
    coordinator: object | None,
) -> None:
    """A live coordinator is the last word on an attestation a bag carries."""
    if coordinator is None:
        return
    transaction = _require_authority_transaction(coordinator, receipt)
    sequence = _commit_sequence(coordinator, transaction)
    if sequence is None or authority != compile_authority_attestation(
        transaction, sequence
    ):
        raise EvidenceResolutionError(
            "archive compile authority does not match coordinator"
        )


def _commit_sequence(coordinator: object, transaction: object) -> int | None:
    with coordinator._connect() as database:
        row = database.execute(
            'SELECT rowid AS commit_sequence FROM "transaction" WHERE id=?',
            (transaction.id,),
        ).fetchone()
    if row is None:
        return None
    return int(row["commit_sequence"])


def validate_bag(
    path: Path,
    *,
    coordinator: object | None = None,
    vault: Path | None = None,
    allow_build_intent: bool = False,
) -> ValidatedBag:
    """Validate a complete, sealed BagIt daily package without following links."""
    path = Path(path)
    try:
        return _validated_bag(path, coordinator, vault, allow_build_intent)
    except EvidenceResolutionError:
        raise
    except (OSError, ValueError, TypeError, KeyError, UnicodeDecodeError) as exc:
        raise EvidenceResolutionError(f"invalid archive bag: {exc}") from exc


_BAG_MEMBERS = frozenset(
    {
        "archive-manifest.json",
        "bag-info.txt",
        "bagit.txt",
        "data",
        "manifest-sha256.txt",
        "tagmanifest-sha256.txt",
    }
)


def _validated_bag(
    path: Path, coordinator: object | None, vault: Path | None, allow_build_intent: bool
) -> ValidatedBag:
    """The checks in the order they were written; each one refuses on its own."""
    expected = _expected_bag_members(allow_build_intent)
    members = _bag_members(path, expected)
    _require_bagit_tag(path)
    manifest = _bag_manifest(path)
    daily_id = _bag_daily_id(manifest)
    payload_name = f"data/{daily_id}.md"
    payload = _bag_payload(path, daily_id, payload_name, manifest)
    payload_hash = sha256_bytes(payload)
    receipt_path, self_contained = _bag_receipt_path(
        path, manifest, daily_id, payload_hash, coordinator, vault, expected
    )
    _require_bag_members(members, expected)
    receipt = _bag_receipt(
        manifest, receipt_path, daily_id, payload_hash, coordinator, vault, self_contained
    )
    _require_manifest_operations(manifest, receipt)
    _require_bag_tags(path, self_contained)
    _require_bag_info(path, payload, daily_id)
    _require_payload_evidence(manifest, payload)
    _require_bag_immutable(path, payload_name, expected)
    return ValidatedBag(path, manifest, path / payload_name, payload)


def _expected_bag_members(allow_build_intent: bool) -> set[str]:
    expected = set(_BAG_MEMBERS)
    if allow_build_intent:
        expected.add("build-intent.json")
    return expected


def _bag_members(path: Path, expected: set[str]) -> set[str]:
    _regular_directory(path, label="archive bag")
    members = {
        item.name
        for item in bounded_directory_entries(
            path, len(expected) + 1, label="archive bag"
        )
    }
    _regular_directory(path / "data", label="archive payload directory")
    return members


def _require_bagit_tag(path: Path) -> None:
    bagit = read_stable_bytes(path / "bagit.txt", 256, label="bagit tag")
    if bagit != b"BagIt-Version: 1.0\nTag-File-Character-Encoding: UTF-8\n":
        raise EvidenceResolutionError("bagit tag is invalid")


def _bag_manifest(path: Path) -> dict[str, object]:
    raw = read_stable_bytes(
        path / "archive-manifest.json",
        MAX_ARCHIVE_MANIFEST_BYTES,
        label="archive manifest",
    )
    manifest = json.loads(raw.decode("utf-8", errors="strict"))
    validate_schema(manifest, ARCHIVE_SCHEMA)
    if canonical_json_bytes(manifest) != raw:
        raise EvidenceResolutionError("archive manifest is not canonical")
    return manifest


def _bag_daily_id(manifest: Mapping[str, object]) -> str:
    daily_id = str(manifest["logical_daily_id"])
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", daily_id) is None:
        raise EvidenceResolutionError("archive logical ID is invalid")
    if manifest["original_path"] != f"knowledge/daily/{daily_id}.md":
        raise EvidenceResolutionError("archive original path is invalid")
    return daily_id


def _bag_payload(
    path: Path, daily_id: str, payload_name: str, manifest: Mapping[str, object]
) -> bytes:
    entries = bounded_directory_entries(
        path / "data", 1, label="archive payload directory"
    )
    if len(entries) != 1 or entries[0].name != f"{daily_id}.md":
        raise EvidenceResolutionError("archive payload members are not canonical")
    payload = read_stable_bytes(
        path / payload_name, MAX_DAILY_BYTES, label="archive payload"
    )
    _require_payload_hash(path, payload_name, payload, manifest)
    return payload


def _require_payload_hash(
    path: Path, payload_name: str, payload: bytes, manifest: Mapping[str, object]
) -> None:
    hashes = _parse_hash_file(
        read_stable_bytes(
            path / "manifest-sha256.txt", MAX_TAG_FILE_BYTES, label="payload manifest"
        ),
        label="payload manifest",
    )
    payload_hash = sha256_bytes(payload)
    if hashes != {payload_name: payload_hash} or any(
        manifest[field] != payload_hash for field in ("source_hash", "payload_hash")
    ):
        raise EvidenceResolutionError("archive payload hash mismatch")


def _bag_receipt_path(
    path: Path,
    manifest: Mapping[str, object],
    daily_id: str,
    payload_hash: str,
    coordinator: object | None,
    vault: Path | None,
    expected: set[str],
) -> tuple[Path, bool]:
    """Where the receipt lives, and whether the bag carries it itself."""
    receipt_ref = manifest["compile_receipt_ref"]
    _require_receipt_reference(receipt_ref, daily_id, payload_hash)
    authority = manifest.get("compile_authority")
    embedded_path = receipt_ref.get("embedded_path")
    if embedded_path is None and authority is None:
        return _external_receipt_path(receipt_ref, coordinator, vault), False
    if embedded_path != "compile-receipt.md" or authority is None:
        raise EvidenceResolutionError("archive embedded receipt reference is invalid")
    expected.add("compile-receipt.md")
    return path / "compile-receipt.md", True


def _external_receipt_path(
    receipt_ref: Mapping[str, object], coordinator: object | None, vault: Path | None
) -> Path:
    if coordinator is None or vault is None:
        raise EvidenceResolutionError("archive receipt authority is required")
    return Path(vault) / str(receipt_ref["path"])


def _require_receipt_reference(
    receipt_ref: Mapping[str, object], daily_id: str, payload_hash: str
) -> None:
    logical_path = f"knowledge/daily/{daily_id}.md"
    from compile_memory import compile_source_identity

    source_identity = compile_source_identity(logical_path, payload_hash)
    if (
        receipt_ref["path"] != f"knowledge/daily/receipts/v3-{source_identity}.md"
        or receipt_ref["logical_path"] != logical_path
        or receipt_ref["source_digest"] != payload_hash
        or receipt_ref["source_identity"] != source_identity
    ):
        raise EvidenceResolutionError("archive compile receipt reference is invalid")


def _require_bag_members(members: set[str], expected: set[str]) -> None:
    if members != expected:
        raise EvidenceResolutionError("archive bag members are not canonical")


def _bag_receipt(
    manifest: Mapping[str, object],
    receipt_path: Path,
    daily_id: str,
    payload_hash: str,
    coordinator: object | None,
    vault: Path | None,
    self_contained: bool,
) -> dict[str, object] | None:
    receipt_ref = manifest["compile_receipt_ref"]
    receipt_bytes = read_stable_bytes(
        receipt_path, MAX_ARCHIVE_MANIFEST_BYTES, label="archive compile receipt"
    )
    if sha256_bytes(receipt_bytes) != receipt_ref["receipt_file_hash"]:
        raise EvidenceResolutionError("archive compile receipt hash mismatch")
    try:
        return _authoritative_receipt(
            receipt_bytes,
            manifest,
            receipt_path,
            daily_id,
            payload_hash,
            coordinator,
            vault,
            self_contained,
        )
    except EvidenceResolutionError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise EvidenceResolutionError(
            "archive compile receipt is not authoritative"
        ) from exc


def _authoritative_receipt(
    receipt_bytes: bytes,
    manifest: Mapping[str, object],
    receipt_path: Path,
    daily_id: str,
    payload_hash: str,
    coordinator: object | None,
    vault: Path | None,
    self_contained: bool,
) -> dict[str, object] | None:
    logical_path = f"knowledge/daily/{daily_id}.md"
    if not self_contained:
        from compile_memory import read_compile_receipt_v3

        return read_compile_receipt_v3(
            logical_path,
            payload_hash,
            coordinator,  # type: ignore[arg-type]
            path=receipt_path,
            vault=Path(vault),  # type: ignore[arg-type]
        )
    from compile_memory import parse_compile_receipt_v3

    receipt = parse_compile_receipt_v3(
        receipt_bytes, logical_path=logical_path, source_sha256=payload_hash
    )
    receipt_ref = manifest["compile_receipt_ref"]
    _validate_compile_authority(
        manifest.get("compile_authority"),
        receipt,
        coordinator,
        receipt_path=str(receipt_ref["path"]),
        receipt_hash=str(receipt_ref["receipt_file_hash"]),
    )
    return receipt


def _require_manifest_operations(
    manifest: Mapping[str, object], receipt: Mapping[str, object] | None
) -> None:
    if receipt is None or manifest["operations"] != [
        {"operation_id": receipt["operation_id"], "state": "succeeded"}
    ]:
        raise EvidenceResolutionError("archive compile receipt operation mismatch")
    _require_terminal_operations(manifest["operations"])
    _require_manifest_preflight(manifest)


def _require_terminal_operations(operations: object) -> None:
    if not operations or any(
        item["state"] not in {"succeeded", "dead", "cancelled"} for item in operations
    ):
        raise EvidenceResolutionError("archive operations are not terminal")


def _require_manifest_preflight(manifest: Mapping[str, object]) -> None:
    if (
        manifest["queue_preflight"]["passed"] is not True
        or manifest["queue_preflight"]["blocking_task_ids"]
    ):
        raise EvidenceResolutionError("archive queue preflight did not pass")
    if manifest["pins"]:
        raise EvidenceResolutionError("archive manifest contains active pins")


def _require_bag_tags(path: Path, self_contained: bool) -> None:
    expected_tags = {
        name: sha256_bytes(
            read_stable_bytes(
                path / name, MAX_TAG_FILE_BYTES, label=f"archive tag {name}"
            )
        )
        for name in _bag_tag_names(self_contained)
    }
    tag_manifest = read_stable_bytes(
        path / "tagmanifest-sha256.txt", MAX_TAG_FILE_BYTES, label="tag manifest"
    )
    if _parse_hash_file(tag_manifest, label="tag manifest") != expected_tags:
        raise EvidenceResolutionError("archive tag hash mismatch")
    if tag_manifest != _canonical_tag_manifest(expected_tags):
        raise EvidenceResolutionError("archive tag manifest is not canonical")


def _bag_tag_names(self_contained: bool) -> tuple[str, ...]:
    embedded = ("compile-receipt.md",) if self_contained else ()
    return (
        "archive-manifest.json",
        "bag-info.txt",
        "bagit.txt",
        *embedded,
        "manifest-sha256.txt",
    )


def _canonical_tag_manifest(expected_tags: Mapping[str, str]) -> bytes:
    return "".join(
        f"{expected_tags[name]}  {name}\n" for name in sorted(expected_tags)
    ).encode()


def _require_bag_info(path: Path, payload: bytes, daily_id: str) -> None:
    bag_info = read_stable_bytes(path / "bag-info.txt", 4096, label="bag info")
    expected = (
        f"Bagging-Date: {_bagging_date(bag_info)}\n"
        f"Payload-Oxum: {len(payload)}.1\n"
        f"External-Identifier: daily:{daily_id}\n"
    ).encode()
    if bag_info != expected:
        raise EvidenceResolutionError("archive bag info is invalid")


def _bagging_date(bag_info: bytes) -> str:
    try:
        first = bag_info.decode("utf-8", errors="strict").splitlines()[0]
        return date.fromisoformat(first.removeprefix("Bagging-Date: ")).isoformat()
    except (IndexError, UnicodeDecodeError, ValueError) as exc:
        raise EvidenceResolutionError("archive bag info is invalid") from exc


def _require_payload_evidence(manifest: Mapping[str, object], payload: bytes) -> None:
    entries = daily_entries(payload)
    block_map = {block_id: (start, end) for block_id, start, end in entries}
    if len(block_map) != len(entries):
        raise EvidenceResolutionError("archive payload has ambiguous block IDs")
    _require_evidence_covers_blocks(manifest["evidence"], block_map)
    for evidence in manifest["evidence"]:
        _require_evidence_entry(evidence, block_map, payload)


def _require_evidence_covers_blocks(
    evidence: object, block_map: Mapping[str, tuple[int, int]]
) -> None:
    evidence_ids = [str(item["block_id"]) for item in evidence]
    if len(evidence_ids) != len(set(evidence_ids)) or set(evidence_ids) != set(
        block_map
    ):
        raise EvidenceResolutionError(
            "archive evidence does not cover every block exactly once"
        )


def _require_evidence_entry(
    evidence: Mapping[str, object],
    block_map: Mapping[str, tuple[int, int]],
    payload: bytes,
) -> None:
    span = block_map.get(str(evidence["block_id"]))
    if span is None or span != (evidence["byte_start"], evidence["byte_end"]):
        raise EvidenceResolutionError("archive evidence block span mismatch")
    start, end = span
    if evidence["sha256"] != sha256_bytes(payload[start:end]) or (
        evidence["line_start"],
        evidence["line_end"],
    ) != _line_span(payload, start, end):
        raise EvidenceResolutionError("archive evidence hash or line span mismatch")


def _require_bag_immutable(
    path: Path, payload_name: str, expected: set[str]
) -> None:
    immutable = [
        path,
        path / "data",
        path / payload_name,
        *(path / name for name in expected if name != "data"),
    ]
    if any(not _archive_path_is_read_only(item) for item in immutable):
        raise EvidenceResolutionError("archive bag is not immutable")


# How much of one day the compiler takes at a time. A day longer than this is
# split at entry boundaries: a single long session used to fail the whole pass,
# leaving every other day uncompiled with it. The bound is bytes rather than
# tokens so the same file always splits the same way, which is what lets a run
# interrupted halfway resume from the parts it already committed.
#
# The splitter lives here, next to the reader, because the writer and the reader
# must cut a day in exactly the same places. It was in `compile_memory` until
# 2026-08-24, and that is why every page compiled from a split day carried
# evidence no reader could resolve.
MAX_DAILY_PART_BYTES = 16 * 1024

# What separates one captured entry from the next in a daily log.
_DAILY_ENTRY_MARKER = b"<!-- llm-wiki-operation:"

# A day is bounded, but the scan for a historical slice must be bounded too.
MAX_EVIDENCE_SLICE_CANDIDATES = 4096


def _daily_entry_offsets(content: bytes) -> list[int]:
    """Where each entry starts, the first one covering whatever precedes it."""
    offsets = [0]
    position = content.find(_DAILY_ENTRY_MARKER)
    while position != -1:
        if position != 0:
            offsets.append(position)
        position = content.find(_DAILY_ENTRY_MARKER, position + 1)
    return offsets


def _daily_part_bounds(content: bytes) -> list[tuple[int, int]]:
    """The byte ranges this day is compiled in, split only where an entry ends."""
    if len(content) <= MAX_DAILY_PART_BYTES:
        return [(0, len(content))]
    offsets = [*_daily_entry_offsets(content), len(content)]
    bounds: list[tuple[int, int]] = []
    start = 0
    for index in range(1, len(offsets)):
        if offsets[index] - start > MAX_DAILY_PART_BYTES and offsets[index - 1] > start:
            bounds.append((start, offsets[index - 1]))
            start = offsets[index - 1]
    bounds.append((start, len(content)))
    return bounds


def _entry_ends(content: bytes, offset: int) -> range:
    """Where a slice that stops before this entry could have ended.

    An appender writes its own separator, so the newlines between the last
    entry of the old file and the first byte of the new one were added with the
    new entry and were not there before. A slice that ended at the old file's
    end therefore ends somewhere inside that run, not at the entry offset.

    Measured on this vault: `knowledge/daily/2026-09-02.md` was 2294 bytes when
    it was compiled, ending `session close.\n`; the next append wrote `\n`
    before its own block, so the entry that follows starts at 2295. Only 2295
    was ever tried, and four claims on two pages read `evidence_unresolved`
    for one byte of separator.
    """
    cursor = offset
    while cursor > 0 and content[cursor - 1 : cursor] == b"\n":
        cursor -= 1
    return range(cursor, offset + 1)


def _slice_boundaries(content: bytes, start: int) -> list[int]:
    """Where a historical slice beginning at `start` could have ended.

    The end of the file is always a candidate, even when a day carries more
    entries than the scan is allowed to try: the whole tail is the one slice a
    compile part is most likely to have been.

    Sorted and deduplicated because `_slice_from` hashes forward from one
    candidate to the next and needs them ascending.
    """
    ends: set[int] = set()
    for offset in _daily_entry_offsets(content):
        if offset > start:
            ends.update(end for end in _entry_ends(content, offset) if end > start)
    return [*sorted(ends)[: MAX_EVIDENCE_SLICE_CANDIDATES - 1], len(content)]


def _slice_from(content: bytes, start: int, digest: str) -> bytes | None:
    running = hashlib.sha256()
    cursor = start
    for boundary in _slice_boundaries(content, start):
        running.update(content[cursor:boundary])
        cursor = boundary
        if running.hexdigest() == digest:
            return content[start:boundary]
    return None


def compile_part_slice(content: bytes, digest: str) -> bytes | None:
    """The exact bytes one compile part held, in a day that has grown since.

    A page is written from one part of a day, so its evidence names that part's
    digest and offsets inside it — not the whole file. A day also keeps growing
    after it was compiled, so what was the last part then is the head of a
    longer part now. Both are one question: is there an entry-aligned slice,
    starting where a part starts, whose bytes still hash to what the page
    recorded? Nothing weaker is accepted — the historical bytes must still be
    present verbatim and in place, which is the append-only argument a
    transparency log makes with a consistency proof (RFC 6962).
    """
    for start, _end in _daily_part_bounds(content):
        found = _slice_from(content, start, digest)
        if found is not None:
            return found
    return None


class EvidenceResolver:
    def __init__(self, vault: Path, *, state_root: Path | None = None):
        self.vault = Path(vault).resolve(strict=True)
        self.state_root = state_root
        self.daily_root = self.vault / "knowledge" / "daily"
        self.archive_root = self.daily_root / "archive"

    def resolve(self, reference: EvidenceRef | str) -> ResolvedEvidence:
        ref = EvidenceRef.parse(reference) if isinstance(reference, str) else reference
        if not isinstance(ref, EvidenceRef):
            raise TypeError("reference must be EvidenceRef or canonical string")
        flat = self.daily_root / f"{ref.daily_id}.md"
        content = _flat_source(flat)
        if content is None:
            return self._resolve_archive(ref)
        return self._resolve_flat(ref, content, flat)

    def _resolve_flat(self, ref: EvidenceRef, content: bytes, flat: Path):
        if sha256_bytes(content) == ref.source_sha256:
            return self._slice(ref, content, flat, "flat")
        part = compile_part_slice(content, ref.source_sha256)
        if part is None:
            raise EvidenceResolutionError("flat daily source hash mismatch")
        return self._slice(ref, part, flat, "flat-part")

    def resolve_bytes(
        self,
        reference: EvidenceRef | str,
        content: bytes,
        *,
        source_path: Path,
        location: str = "snapshot",
    ) -> ResolvedEvidence:
        """Apply the same hash/block/span checks to an immutable in-memory source."""
        ref = EvidenceRef.parse(reference) if isinstance(reference, str) else reference
        if not isinstance(ref, EvidenceRef) or not isinstance(content, bytes):
            raise TypeError("reference and immutable content have invalid types")
        if sha256_bytes(content) != ref.source_sha256:
            raise EvidenceResolutionError("immutable daily source hash mismatch")
        return self._slice(ref, content, Path(source_path), location)

    def _resolve_archive(self, ref: EvidenceRef) -> ResolvedEvidence:
        month = self.archive_root / ref.daily_id[:7]
        if not month.exists():
            raise EvidenceResolutionError("evidence source was not found")
        matches = self._archive_matches(month, ref)
        if len(matches) != 1:
            reason = "not found" if not matches else "ambiguous"
            raise EvidenceResolutionError(f"archive evidence source is {reason}")
        bag = matches[0]
        return self._slice(ref, bag.payload, bag.payload_path, "archive")

    def _archive_matches(self, month: Path, ref: EvidenceRef) -> list[ValidatedBag]:
        """Every sealed bag in the month that carries the day this reference names."""
        try:
            return [bag for bag in self._validated_bags(month) if _bag_matches(bag, ref)]
        except EvidenceResolutionError:
            raise
        except (OSError, ValueError) as exc:
            raise EvidenceResolutionError(f"invalid archive boundary: {exc}") from exc

    def _validated_bags(self, month: Path) -> list[ValidatedBag]:
        coordinator = self._archive_coordinator()
        return [
            validate_bag(candidate, coordinator=coordinator, vault=self.vault)
            for candidate in self._bag_candidates(month)
        ]

    def _bag_candidates(self, month: Path) -> list[Path]:
        _regular_directory(self.archive_root, label="archive root")
        _regular_directory(month, label="archive month")
        entries = bounded_directory_entries(
            month, MAX_DIRECTORY_ENTRIES, label="archive month"
        )
        candidates = sorted(item for item in entries if item.name.startswith("bag-"))
        if len(candidates) > MAX_BAGS_PER_MONTH:
            raise EvidenceResolutionError("archive month exceeds the bag scan limit")
        return candidates

    def _archive_coordinator(self) -> object | None:
        """None means there is no transaction database to hold a bag to account."""
        if self.state_root is None:
            from memory_state import STATE_ROOT

            self.state_root = Path(STATE_ROOT)
        if not (self.state_root / "run/markdown-transactions.sqlite3").exists():
            return None
        from markdown_transaction import active_or_legacy_coordinator

        return active_or_legacy_coordinator(self.vault, self.state_root)

    @staticmethod
    def _slice(
        ref: EvidenceRef, content: bytes, source_path: Path, location: str
    ) -> ResolvedEvidence:
        _require_utf8(content, "daily source is not UTF-8")
        if ref.byte_end > len(content):
            raise EvidenceResolutionError("evidence byte span exceeds the source")
        selected = content[ref.byte_start : ref.byte_end]
        _require_utf8(selected, "evidence span is not on UTF-8 boundaries")
        block_start, block_end = _sole_block_span(content, ref)
        line_start, line_end = _line_span(content, ref.byte_start, ref.byte_end)
        return ResolvedEvidence(
            reference=ref,
            bytes=selected,
            sha256=sha256_bytes(selected),
            source_sha256=sha256_bytes(content),
            block_sha256=sha256_bytes(content[block_start:block_end]),
            byte_start=ref.byte_start,
            byte_end=ref.byte_end,
            line_start=line_start,
            line_end=line_end,
            location=location,
            source_path=source_path,
        )


def _flat_source(flat: Path) -> bytes | None:
    """None means the day is no longer flat and must come from an archive."""
    try:
        return read_stable_bytes(flat, MAX_DAILY_BYTES, label="daily source")
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise EvidenceResolutionError(str(exc)) from exc


def _require_utf8(content: bytes, message: str) -> None:
    try:
        content.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise EvidenceResolutionError(message) from exc


def _sole_block_span(content: bytes, ref: EvidenceRef) -> tuple[int, int]:
    """The one entry this reference names, and proof the span sits inside it."""
    matching = [item for item in daily_entries(content) if item[0] == ref.block_id]
    if len(matching) != 1:
        raise EvidenceResolutionError("evidence block is ambiguous or missing")
    _block_id, block_start, block_end = matching[0]
    _require_span_inside_block(ref, block_start, block_end)
    return block_start, block_end


def _require_span_inside_block(ref: EvidenceRef, start: int, end: int) -> None:
    if ref.byte_start < start or ref.byte_end > end:
        raise EvidenceResolutionError("evidence span is outside its block")


def _bag_matches(bag: ValidatedBag, ref: EvidenceRef) -> bool:
    if bag.manifest["logical_daily_id"] != ref.daily_id:
        return False
    if bag.manifest["source_hash"] != ref.source_sha256:
        raise EvidenceResolutionError("archive daily source hash mismatch")
    return True


def extract_evidence_references(text: str) -> list[EvidenceRef]:
    """Parse every ``daily:`` candidate instead of skipping malformed references."""
    if not isinstance(text, str):
        raise TypeError("evidence source text must be a string")
    references: list[EvidenceRef] = []
    for line in text.splitlines():
        references.extend(_line_references(line))
    return references


def _line_references(line: str) -> list[EvidenceRef]:
    references: list[EvidenceRef] = []
    cursor = 0
    while True:
        start = line.find("daily:", cursor)
        if start < 0:
            return references
        _require_reference_prefix(line, start)
        candidate, cursor = _reference_candidate(line, start)
        references.append(_parsed_reference(candidate))


def _require_reference_prefix(line: str, start: int) -> None:
    if start and (line[start - 1].isalnum() or line[start - 1] == "_"):
        raise ValueError("evidence reference has an invalid prefix")


def _reference_candidate(line: str, start: int) -> tuple[str, int]:
    """A backtick-quoted reference ends at its closing backtick, not at the line."""
    if start == 0 or line[start - 1] != "`":
        return line[start:].strip(), len(line)
    end = line.find("`", start)
    if end < 0:
        raise ValueError("evidence reference has no closing delimiter")
    return line[start:end], end + 1


def _parsed_reference(candidate: str) -> EvidenceRef:
    try:
        return EvidenceRef.parse(candidate)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"evidence reference is not canonical: {candidate}") from exc


def verify_supplied_citation(
    citation: Mapping[str, object],
    supplied: Mapping[str, object],
    *,
    vault: Path,
) -> None:
    """Bind one generated citation to the exact span supplied for generation."""
    _require_citation_fields(citation, supplied)
    verify_evidence_span(supplied, vault=vault)


def verify_evidence_span(supplied: Mapping[str, object], *, vault: Path) -> None:
    """Bind one supplied evidence span to the source it was cut from.

    This is the half of the check that reads the vault: the path resolves inside
    it, the file still hashes to what generation was shown, and the recorded byte
    range still holds the recorded span. It takes no generated text, so it says
    nothing about who cited it — only that the span is still what it was.
    """
    source_path = _citation_source_path(supplied.get("relative_path"), vault)
    text = supplied.get("text")
    _require_citation_span(supplied, text)
    source = _citation_source_bytes(source_path)
    _require_citation_binding(supplied, source, str(text))


_CITATION_FIELDS = frozenset(
    {
        "citation_id",
        "relative_path",
        "source_sha256",
        "revision",
        "byte_start",
        "byte_end",
        "line_start",
        "line_end",
        "span_sha256",
    }
)


def _require_citation_fields(
    citation: Mapping[str, object], supplied: Mapping[str, object]
) -> None:
    if set(citation) != _CITATION_FIELDS or any(
        citation.get(key) != supplied.get(key) for key in _CITATION_FIELDS
    ):
        raise EvidenceResolutionError("citation does not match supplied evidence")


def _citation_source_path(relative: object, vault: Path) -> Path:
    try:
        from reliable_memory import restricted_relative_path

        normalized = restricted_relative_path(str(relative), ("knowledge",))
        root = Path(vault).resolve(strict=True)
        source_path = (root / Path(*normalized.parts)).resolve(strict=True)
    except (OSError, TypeError, ValueError) as exc:
        raise EvidenceResolutionError("citation path is invalid") from exc
    if not source_path.is_relative_to(root):
        raise EvidenceResolutionError("citation path escapes the vault root")
    return source_path


def _require_citation_span(citation: Mapping[str, object], text: object) -> None:
    _require_span_bounds(citation.get("byte_start"), citation.get("byte_end"))
    _require_span_text(text, citation.get("span_sha256"))


def _require_span_bounds(start: object, end: object) -> None:
    if not _is_plain_int(start) or not _is_plain_int(end):
        raise EvidenceResolutionError("citation span is invalid")
    if start < 0 or start >= end:
        raise EvidenceResolutionError("citation span is invalid")


def _require_span_text(text: object, span_sha256: object) -> None:
    if not isinstance(text, str) or sha256_bytes(text.encode("utf-8")) != span_sha256:
        raise EvidenceResolutionError("citation span is invalid")


def _citation_source_bytes(source_path: Path) -> bytes:
    try:
        return read_stable_bytes(
            source_path, MAX_GROUNDED_SOURCE_BYTES, label="grounded citation source"
        )
    except (OSError, ValueError) as exc:
        raise EvidenceResolutionError("citation source cannot be verified") from exc


def _require_citation_binding(
    citation: Mapping[str, object], source: bytes, text: str
) -> None:
    if sha256_bytes(source) != citation.get("source_sha256"):
        raise EvidenceResolutionError("citation source hash mismatch")
    start = int(citation["byte_start"])
    end = int(citation["byte_end"])
    _require_citation_range(citation, source, text, start, end)


def _require_citation_range(
    citation: Mapping[str, object], source: bytes, text: str, start: int, end: int
) -> None:
    if end > len(source):
        raise EvidenceResolutionError("citation range exceeds its source")
    if _citation_span_disagrees(citation, source, text, start, end):
        raise EvidenceResolutionError("citation range or span hash mismatch")


def _citation_span_disagrees(
    citation: Mapping[str, object], source: bytes, text: str, start: int, end: int
) -> bool:
    span = source[start:end]
    if sha256_bytes(span) != citation.get("span_sha256"):
        return True
    if span != text.encode("utf-8"):
        return True
    return _line_span(source, start, end) != (
        citation.get("line_start"),
        citation.get("line_end"),
    )
