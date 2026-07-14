"""Resolve content-addressed daily evidence from flat files or sealed bags."""
from __future__ import annotations

import json
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from bounded_io import read_stable_bytes
from reliable_memory import canonical_json_bytes, sha256_bytes, validate_schema

MAX_DAILY_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_MANIFEST_BYTES = 1024 * 1024
MAX_TAG_FILE_BYTES = 1024 * 1024
MAX_BAGS_PER_MONTH = 10_000
ARCHIVE_SCHEMA = Path(__file__).with_name("schemas") / "archive-manifest-v1.json"
_REF_RE = re.compile(
    r"daily:(?P<daily>\d{4}-\d{2}-\d{2}) "
    r"sha256:(?P<sha>[0-9a-f]{64}) "
    r"block:(?P<block>[A-Za-z0-9][A-Za-z0-9._:-]{0,199}) "
    r"bytes:(?P<start>0|[1-9]\d*)-(?P<end>0|[1-9]\d*)"
)
_REF_SEARCH_RE = re.compile(
    r"daily:\d{4}-\d{2}-\d{2} sha256:[0-9a-f]{64} "
    r"block:[A-Za-z0-9][A-Za-z0-9._:-]{0,199} bytes:(?:0|[1-9]\d*)-(?:0|[1-9]\d*)"
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

    @classmethod
    def parse(cls, value: str) -> EvidenceRef:
        if not isinstance(value, str):
            raise TypeError("evidence reference must be a string")
        match = _REF_RE.fullmatch(value)
        if match is None:
            raise ValueError("evidence reference is not canonical")
        start, end = int(match["start"]), int(match["end"])
        if start >= end:
            raise ValueError("evidence byte span must be non-empty and half-open")
        try:
            from datetime import datetime

            parsed = datetime.strptime(match["daily"], "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("evidence daily ID is invalid") from exc
        if parsed.strftime("%Y-%m-%d") != match["daily"]:
            raise ValueError("evidence daily ID is not canonical")
        return cls(match["daily"], match["sha"], match["block"], start, end)

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
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        blocks.append((block_id, match.start(), end))
    return blocks


def _line_span(content: bytes, start: int, end: int) -> tuple[int, int]:
    return content[:start].count(b"\n") + 1, content[: end - 1].count(b"\n") + 2


def validate_bag(path: Path) -> ValidatedBag:
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
        if {item.name for item in path.iterdir()} != expected:
            raise EvidenceResolutionError("archive bag members are not canonical")
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
        payload_entries = list((path / "data").iterdir())
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
        if f"External-Identifier: daily:{daily_id}\n".encode() not in bag_info:
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
        return ValidatedBag(path, manifest, path / payload_name, payload)
    except EvidenceResolutionError:
        raise
    except (OSError, ValueError, TypeError, KeyError, UnicodeDecodeError) as exc:
        raise EvidenceResolutionError(f"invalid archive bag: {exc}") from exc


class EvidenceResolver:
    def __init__(self, vault: Path):
        self.vault = Path(vault).resolve(strict=True)
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
            candidates = sorted(item for item in month.iterdir() if item.name.startswith("bag-"))
            if len(candidates) > MAX_BAGS_PER_MONTH:
                raise EvidenceResolutionError("archive month exceeds the bag scan limit")
            matches: list[ValidatedBag] = []
            for candidate in candidates:
                bag = validate_bag(candidate)
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
    """Extract canonical logical references in stable source order."""
    if not isinstance(text, str):
        raise TypeError("evidence source text must be a string")
    return [EvidenceRef.parse(match.group(0)) for match in _REF_SEARCH_RE.finditer(text)]
