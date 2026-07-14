"""Resolve content-addressed daily evidence from flat files or sealed bags."""
from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from bounded_io import read_stable_bytes
from reliable_memory import canonical_json_bytes, sha256_bytes, validate_schema

MAX_DAILY_BYTES = 16 * 1024 * 1024
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
_HASH_LINE_RE = re.compile(r"([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._/-]*)\n")


class EvidenceResolutionError(ValueError):
    """Evidence could not be proven from one unambiguous immutable source."""


@dataclass(frozen=True)
class EvidenceRef:
    daily_id: str
    source_sha256: str
    block_id: str
    byte_start: int
    byte_end: int

    def __post_init__(self) -> None:
        if not isinstance(self.daily_id, str) or re.fullmatch(
            _DAILY_ID_PATTERN, self.daily_id
        ) is None:
            raise ValueError("evidence daily ID is invalid")
        try:
            if date.fromisoformat(self.daily_id).isoformat() != self.daily_id:
                raise ValueError
        except ValueError as exc:
            raise ValueError("evidence daily ID is invalid") from exc
        if not isinstance(self.source_sha256, str) or re.fullmatch(
            _SHA256_PATTERN, self.source_sha256
        ) is None:
            raise ValueError("evidence SHA-256 is invalid")
        if not isinstance(self.block_id, str) or _BLOCK_ID_RE.fullmatch(self.block_id) is None:
            raise ValueError("evidence block ID is invalid")
        if (
            not isinstance(self.byte_start, int)
            or isinstance(self.byte_start, bool)
            or not isinstance(self.byte_end, int)
            or isinstance(self.byte_end, bool)
            or self.byte_start < 0
            or self.byte_start >= self.byte_end
        ):
            raise ValueError("evidence byte span must be non-empty and half-open")

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
        from markdown_transaction import (
            _acl_output_text,
            _run_acl_command,
            _windows_acl_identity,
        )

        verified = _run_acl_command(["icacls", str(path)])
        if verified.returncode != 0:
            return False
        identity = _windows_acl_identity()
        acl = _acl_output_text(verified.stdout)
        lines = [line.strip() for line in acl.splitlines() if ":(" in line]
        return bool(lines) and all(
            identity.casefold() in line.casefold() for line in lines
        ) and not any(marker in acl for marker in ("(F)", "(M)", "(W)"))
    expected = 0o500 if path.is_dir() else 0o400
    try:
        return stat.S_IMODE(path.stat(follow_symlinks=False).st_mode) == expected
    except OSError:
        return False


def _parse_hash_file(raw: bytes, *, label: str) -> dict[str, str]:
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise EvidenceResolutionError(f"{label} is not UTF-8") from exc
    position = 0
    result: dict[str, str] = {}
    while position < len(text):
        match = _HASH_LINE_RE.match(text, position)
        if match is None or match[2] in result or ".." in Path(match[2]).parts:
            raise EvidenceResolutionError(f"{label} is not canonical")
        result[match[2]] = match[1]
        position = match.end()
    if not result:
        raise EvidenceResolutionError(f"{label} is empty")
    return result


def _blocks(content: bytes) -> list[tuple[str, int, int]]:
    matches = list(_HEADER_RE.finditer(content))
    blocks: list[tuple[str, int, int]] = []
    for index, match in enumerate(matches):
        try:
            block_id = match[1].decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise EvidenceResolutionError("daily block ID is not UTF-8") from exc
        if _BLOCK_ID_RE.fullmatch(block_id) is None:
            raise EvidenceResolutionError("daily block ID is invalid")
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        blocks.append((block_id, match.start(), end))
    return blocks


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
    if not isinstance(authority, dict):
        raise EvidenceResolutionError("archive compile authority is missing")
    required = {
        "schema",
        "transaction_id",
        "state",
        "committed_at",
        "commit_sequence",
        "operation_ids",
        "coordinator_record",
        "coordinator_record_digest",
    }
    if set(authority) != required:
        raise EvidenceResolutionError("archive compile authority fields are invalid")
    try:
        committed_at_raw = str(authority["committed_at"])
        if committed_at_raw.endswith("Z"):
            committed_at_raw = committed_at_raw[:-1] + "+00:00"
        committed_at = datetime.fromisoformat(committed_at_raw)
    except ValueError as exc:
        raise EvidenceResolutionError("archive compile authority time is invalid") from exc
    operation_ids = authority["operation_ids"]
    coordinator_record = authority["coordinator_record"]
    if (
        authority["schema"] != "archive-compile-authority/v1"
        or authority["state"] != "committed"
        or committed_at.tzinfo is None
        or not isinstance(authority["commit_sequence"], int)
        or isinstance(authority["commit_sequence"], bool)
        or authority["commit_sequence"] < 1
        or operation_ids != [receipt["operation_id"]]
        or not isinstance(coordinator_record, dict)
        or re.fullmatch(_SHA256_PATTERN, str(authority["coordinator_record_digest"]))
        is None
    ):
        raise EvidenceResolutionError("archive compile authority is invalid")
    record_fields = {
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
    if (
        set(coordinator_record) != record_fields
        or sha256_bytes(canonical_json_bytes(coordinator_record))
        != authority["coordinator_record_digest"]
        or coordinator_record["transaction_id"] != authority["transaction_id"]
        or coordinator_record["operation_id"] != receipt["operation_id"]
        or coordinator_record["state"] != authority["state"]
        or coordinator_record["updated_at"] != authority["committed_at"]
        or not isinstance(coordinator_record["operations"], list)
    ):
        raise EvidenceResolutionError("archive compile authority record is invalid")
    record_operations = {
        item.get("path"): item
        for item in coordinator_record["operations"]
        if isinstance(item, dict)
    }
    if len(record_operations) != len(coordinator_record["operations"]):
        raise EvidenceResolutionError("archive compile authority operations are invalid")
    receipt_operation = record_operations.get(receipt_path)
    if (
        receipt_operation is None
        or receipt_operation.get("after_hash") != receipt_hash
        or any(
            record_operations.get(item["path"], {}).get("kind") != item["kind"]
            or record_operations.get(item["path"], {}).get("after_hash")
            != item["after_sha256"]
            for item in receipt["operations"]
        )
    ):
        raise EvidenceResolutionError("archive compile authority operation binding failed")
    if coordinator is None:
        return
    transaction = coordinator._record_for_operation_id(str(receipt["operation_id"]))
    if transaction is None:
        raise EvidenceResolutionError("archive compile authority transaction is missing")
    with coordinator._connect() as database:
        row = database.execute(
            'SELECT rowid AS commit_sequence FROM "transaction" WHERE id=?',
            (transaction.id,),
        ).fetchone()
    if row is None or authority != compile_authority_attestation(
        transaction, int(row["commit_sequence"])
    ):
        raise EvidenceResolutionError("archive compile authority does not match coordinator")


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
        _regular_directory(path, label="archive bag")
        expected = {
            "archive-manifest.json",
            "bag-info.txt",
            "bagit.txt",
            "data",
            "manifest-sha256.txt",
            "tagmanifest-sha256.txt",
        }
        if allow_build_intent:
            expected.add("build-intent.json")
        members = {
            item.name
            for item in bounded_directory_entries(
                path, len(expected) + 1, label="archive bag"
            )
        }
        _regular_directory(path / "data", label="archive payload directory")
        bagit = read_stable_bytes(path / "bagit.txt", 256, label="bagit tag")
        if bagit != b"BagIt-Version: 1.0\nTag-File-Character-Encoding: UTF-8\n":
            raise EvidenceResolutionError("bagit tag is invalid")
        manifest_raw = read_stable_bytes(
            path / "archive-manifest.json",
            MAX_ARCHIVE_MANIFEST_BYTES,
            label="archive manifest",
        )
        manifest = json.loads(manifest_raw.decode("utf-8", errors="strict"))
        validate_schema(manifest, ARCHIVE_SCHEMA)
        if canonical_json_bytes(manifest) != manifest_raw:
            raise EvidenceResolutionError("archive manifest is not canonical")
        daily_id = str(manifest["logical_daily_id"])
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", daily_id) is None:
            raise EvidenceResolutionError("archive logical ID is invalid")
        if manifest["original_path"] != f"knowledge/daily/{daily_id}.md":
            raise EvidenceResolutionError("archive original path is invalid")
        payload_name = f"data/{daily_id}.md"
        payload_entries = bounded_directory_entries(
            path / "data", 1, label="archive payload directory"
        )
        if len(payload_entries) != 1 or payload_entries[0].name != f"{daily_id}.md":
            raise EvidenceResolutionError("archive payload members are not canonical")
        payload = read_stable_bytes(
            path / payload_name, MAX_DAILY_BYTES, label="archive payload"
        )
        payload_hashes = _parse_hash_file(
            read_stable_bytes(
                path / "manifest-sha256.txt", MAX_TAG_FILE_BYTES, label="payload manifest"
            ),
            label="payload manifest",
        )
        payload_hash = sha256_bytes(payload)
        if payload_hashes != {payload_name: payload_hash} or any(
            manifest[field] != payload_hash for field in ("source_hash", "payload_hash")
        ):
            raise EvidenceResolutionError("archive payload hash mismatch")
        receipt_ref = manifest["compile_receipt_ref"]
        if (
            receipt_ref["path"]
            != f"knowledge/daily/receipts/{payload_hash}.md"
            or receipt_ref["source_digest"] != payload_hash
        ):
            raise EvidenceResolutionError("archive compile receipt reference is invalid")
        embedded_path = receipt_ref.get("embedded_path")
        authority = manifest.get("compile_authority")
        self_contained = embedded_path is not None or authority is not None
        if self_contained:
            if embedded_path != "compile-receipt.md" or authority is None:
                raise EvidenceResolutionError("archive embedded receipt reference is invalid")
            expected.add("compile-receipt.md")
            receipt_path = path / "compile-receipt.md"
        else:
            if coordinator is None or vault is None:
                raise EvidenceResolutionError("archive receipt authority is required")
            receipt_path = Path(vault) / str(receipt_ref["path"])
        if members != expected:
            raise EvidenceResolutionError("archive bag members are not canonical")
        receipt_bytes = read_stable_bytes(
            receipt_path, MAX_ARCHIVE_MANIFEST_BYTES, label="archive compile receipt"
        )
        if sha256_bytes(receipt_bytes) != receipt_ref["receipt_file_hash"]:
            raise EvidenceResolutionError("archive compile receipt hash mismatch")
        try:
            if self_contained:
                from compile_memory import parse_compile_receipt

                receipt = parse_compile_receipt(receipt_bytes, payload_hash)
                _validate_compile_authority(
                    authority,
                    receipt,
                    coordinator,
                    receipt_path=str(receipt_ref["path"]),
                    receipt_hash=str(receipt_ref["receipt_file_hash"]),
                )
            else:
                from compile_memory import read_compile_receipt

                receipt = read_compile_receipt(
                    payload_hash,
                    coordinator,  # type: ignore[arg-type]
                    path=receipt_path,
                    vault=Path(vault),
                )
        except EvidenceResolutionError:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            raise EvidenceResolutionError("archive compile receipt is not authoritative") from exc
        if receipt is None or manifest["operations"] != [
            {"operation_id": receipt["operation_id"], "state": "succeeded"}
        ]:
            raise EvidenceResolutionError("archive compile receipt operation mismatch")
        if not manifest["operations"] or any(
            item["state"] not in {"succeeded", "dead", "cancelled"}
            for item in manifest["operations"]
        ):
            raise EvidenceResolutionError("archive operations are not terminal")
        if (
            manifest["queue_preflight"]["passed"] is not True
            or manifest["queue_preflight"]["blocking_task_ids"]
        ):
            raise EvidenceResolutionError("archive queue preflight did not pass")
        if manifest["pins"]:
            raise EvidenceResolutionError("archive manifest contains active pins")
        tag_names = (
            "archive-manifest.json",
            "bag-info.txt",
            "bagit.txt",
            *(("compile-receipt.md",) if self_contained else ()),
            "manifest-sha256.txt",
        )
        tag_hashes = _parse_hash_file(
            read_stable_bytes(
                path / "tagmanifest-sha256.txt", MAX_TAG_FILE_BYTES, label="tag manifest"
            ),
            label="tag manifest",
        )
        expected_tags = {
            name: sha256_bytes(
                read_stable_bytes(path / name, MAX_TAG_FILE_BYTES, label=f"archive tag {name}")
            )
            for name in tag_names
        }
        if tag_hashes != expected_tags:
            raise EvidenceResolutionError("archive tag hash mismatch")
        canonical_tags = "".join(
            f"{expected_tags[name]}  {name}\n" for name in sorted(expected_tags)
        ).encode()
        if read_stable_bytes(
            path / "tagmanifest-sha256.txt",
            MAX_TAG_FILE_BYTES,
            label="tag manifest",
        ) != canonical_tags:
            raise EvidenceResolutionError("archive tag manifest is not canonical")
        bag_info = read_stable_bytes(path / "bag-info.txt", 4096, label="bag info")
        try:
            bagging_date = date.fromisoformat(
                bag_info.decode("utf-8", errors="strict").splitlines()[0].removeprefix(
                    "Bagging-Date: "
                )
            ).isoformat()
        except (IndexError, UnicodeDecodeError, ValueError) as exc:
            raise EvidenceResolutionError("archive bag info is invalid") from exc
        expected_bag_info = (
            f"Bagging-Date: {bagging_date}\n"
            f"Payload-Oxum: {len(payload)}.1\n"
            f"External-Identifier: daily:{daily_id}\n"
        ).encode()
        if bag_info != expected_bag_info:
            raise EvidenceResolutionError("archive bag info is invalid")
        block_map = {block_id: (start, end) for block_id, start, end in _blocks(payload)}
        if len(block_map) != len(_blocks(payload)):
            raise EvidenceResolutionError("archive payload has ambiguous block IDs")
        evidence_ids = [str(item["block_id"]) for item in manifest["evidence"]]
        if len(evidence_ids) != len(set(evidence_ids)) or set(evidence_ids) != set(block_map):
            raise EvidenceResolutionError("archive evidence does not cover every block exactly once")
        for evidence in manifest["evidence"]:
            block_id = str(evidence["block_id"])
            span = block_map.get(block_id)
            if span is None or span != (evidence["byte_start"], evidence["byte_end"]):
                raise EvidenceResolutionError("archive evidence block span mismatch")
            start, end = span
            if (
                evidence["sha256"] != sha256_bytes(payload[start:end])
                or (evidence["line_start"], evidence["line_end"])
                != _line_span(payload, start, end)
            ):
                raise EvidenceResolutionError("archive evidence hash or line span mismatch")
        immutable_paths = [
            path,
            path / "data",
            path / payload_name,
            *(path / name for name in expected if name != "data"),
        ]
        if any(not _archive_path_is_read_only(item) for item in immutable_paths):
            raise EvidenceResolutionError("archive bag is not immutable")
        return ValidatedBag(path, manifest, path / payload_name, payload)
    except EvidenceResolutionError:
        raise
    except (OSError, ValueError, TypeError, KeyError, UnicodeDecodeError) as exc:
        raise EvidenceResolutionError(f"invalid archive bag: {exc}") from exc


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
        try:
            content = read_stable_bytes(flat, MAX_DAILY_BYTES, label="daily source")
        except FileNotFoundError:
            return self._resolve_archive(ref)
        except (OSError, ValueError) as exc:
            raise EvidenceResolutionError(str(exc)) from exc
        if sha256_bytes(content) != ref.source_sha256:
            raise EvidenceResolutionError("flat daily source hash mismatch")
        return self._slice(ref, content, flat, "flat")

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
        try:
            _regular_directory(self.archive_root, label="archive root")
            _regular_directory(month, label="archive month")
            entries = bounded_directory_entries(
                month, MAX_DIRECTORY_ENTRIES, label="archive month"
            )
            candidates = sorted(item for item in entries if item.name.startswith("bag-"))
            if len(candidates) > MAX_BAGS_PER_MONTH:
                raise EvidenceResolutionError("archive month exceeds the bag scan limit")
            if self.state_root is None:
                from memory_state import STATE_ROOT

                self.state_root = Path(STATE_ROOT)
            coordinator = None
            coordinator_db = self.state_root / "run/markdown-transactions.sqlite3"
            if coordinator_db.exists():
                from markdown_transaction import MarkdownCoordinator

                coordinator = MarkdownCoordinator(self.vault, self.state_root)
            matches: list[ValidatedBag] = []
            for candidate in candidates:
                bag = validate_bag(
                    candidate,
                    coordinator=coordinator,
                    vault=self.vault,
                )
                if bag.manifest["logical_daily_id"] != ref.daily_id:
                    continue
                if bag.manifest["source_hash"] != ref.source_sha256:
                    raise EvidenceResolutionError("archive daily source hash mismatch")
                matches.append(bag)
        except EvidenceResolutionError:
            raise
        except (OSError, ValueError) as exc:
            raise EvidenceResolutionError(f"invalid archive boundary: {exc}") from exc
        if len(matches) != 1:
            reason = "not found" if not matches else "ambiguous"
            raise EvidenceResolutionError(f"archive evidence source is {reason}")
        bag = matches[0]
        return self._slice(ref, bag.payload, bag.payload_path, "archive")

    @staticmethod
    def _slice(
        ref: EvidenceRef, content: bytes, source_path: Path, location: str
    ) -> ResolvedEvidence:
        try:
            content.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise EvidenceResolutionError("daily source is not UTF-8") from exc
        if ref.byte_end > len(content):
            raise EvidenceResolutionError("evidence byte span exceeds the source")
        selected = content[ref.byte_start : ref.byte_end]
        try:
            selected.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise EvidenceResolutionError("evidence span is not on UTF-8 boundaries") from exc
        matching = [item for item in _blocks(content) if item[0] == ref.block_id]
        if len(matching) != 1:
            raise EvidenceResolutionError("evidence block is ambiguous or missing")
        _block_id, block_start, block_end = matching[0]
        if ref.byte_start < block_start or ref.byte_end > block_end:
            raise EvidenceResolutionError("evidence span is outside its block")
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


def extract_evidence_references(text: str) -> list[EvidenceRef]:
    """Parse every ``daily:`` candidate instead of skipping malformed references."""
    if not isinstance(text, str):
        raise TypeError("evidence source text must be a string")
    references: list[EvidenceRef] = []
    for line in text.splitlines():
        cursor = 0
        while True:
            start = line.find("daily:", cursor)
            if start < 0:
                break
            if start and (line[start - 1].isalnum() or line[start - 1] == "_"):
                raise ValueError("evidence reference has an invalid prefix")
            quoted = start > 0 and line[start - 1] == "`"
            if quoted:
                end = line.find("`", start)
                if end < 0:
                    raise ValueError("evidence reference has no closing delimiter")
                candidate = line[start:end]
                cursor = end + 1
            else:
                candidate = line[start:].strip()
                cursor = len(line)
            try:
                references.append(EvidenceRef.parse(candidate))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"evidence reference is not canonical: {candidate}") from exc
    return references
