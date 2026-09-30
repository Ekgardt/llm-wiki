"""Complete, integrity-linked breadcrumb evidence; no publication or model calls.

Each physical record fits the existing capture transport. Logical input is not
truncated or capped by a part count. See the 2026-09-29 breadcrumb decision.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path

from memory_state import MAX_CAPTURE_INTENT_BYTES
from reliable_memory import canonical_json_bytes, sha256_bytes, validate_schema

SCHEMAS = Path(__file__).with_name("schemas")
MANIFEST_VERSION = "capture-intent/v2"
PART_VERSION = "breadcrumb-part/v1"
ANCHOR_VERSION = "breadcrumb-occurrence/v1"


def require_digest(value: object) -> str:
    """Only canonical SHA-256 identities may enter an evidence filename."""
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("breadcrumb digest is invalid")
    return value


def _require_equal(actual: object, expected: object, message: str) -> None:
    if actual != expected:
        raise ValueError(message)


def _record_bytes(record: Mapping[str, object], schema: str) -> bytes:
    validate_schema(dict(record), SCHEMAS / schema)
    encoded = canonical_json_bytes(dict(record))
    if len(encoded) > MAX_CAPTURE_INTENT_BYTES:
        raise ValueError("breadcrumb physical record exceeds the capture transport")
    return encoded


def _read_record(data: bytes, schema: str) -> dict:
    value = json.loads(data.decode("utf-8", errors="strict"))
    if not isinstance(value, dict):
        raise ValueError("breadcrumb record must be an object")
    encoded = _record_bytes(value, schema)
    _require_equal(data, encoded, "breadcrumb record is not canonical")
    return value


def occurrence_identity(scope: Mapping[str, object]) -> str:
    """The caller supplies occurrence identity, independently of content."""
    return sha256_bytes(canonical_json_bytes({"schema_version": ANCHOR_VERSION, "scope": dict(scope)}))


def _aware(moment: datetime) -> str:
    if moment.utcoffset() is None:
        raise ValueError("breadcrumb time requires a timezone")
    return moment.isoformat()


def make_anchor(
    intent_id: str, content: bytes, *, occurred_at: datetime,
    accepted_at: datetime, time_origin: str,
) -> bytes:
    """Persist once under the occurrence fence; retries reuse these exact bytes."""
    record = {
        "schema_version": ANCHOR_VERSION, "intent_id": intent_id,
        "input_sha256": sha256_bytes(content), "input_bytes": len(content),
        "occurred_at": _aware(occurred_at), "accepted_at": _aware(accepted_at),
        "day": occurred_at.date().isoformat(), "time_origin": time_origin,
    }
    return _record_bytes(record, "breadcrumb-occurrence-v1.json")


def read_anchor(data: bytes) -> dict:
    record = _read_record(data, "breadcrumb-occurrence-v1.json")
    occurred = datetime.fromisoformat(record["occurred_at"])
    _aware(occurred)
    _aware(datetime.fromisoformat(record["accepted_at"]))
    _require_equal(record["day"], occurred.date().isoformat(), "breadcrumb day conflicts with occurrence")
    return record


def _part_record(intent_id: str, index: int, previous: str | None, text: str) -> dict:
    return {
        "schema_version": PART_VERSION, "intent_id": intent_id,
        "index": index, "previous_sha256": previous, "text": text,
    }


def _part_size(intent_id: str, index: int, previous: str | None, text: str) -> int:
    return len(canonical_json_bytes(_part_record(intent_id, index, previous, text)))


def _fitting_end(text: str, start: int, intent_id: str, index: int, previous: str | None) -> int:
    """Binary search the actual serialized size, without a shrink-attempt cap."""
    # Every character requires at least one encoded byte. Looking beyond the
    # physical transport size cannot find a fitting prefix and makes repeated
    # partitioning quadratic for large logical inputs.
    low, high = start, min(len(text), start + MAX_CAPTURE_INTENT_BYTES)
    if _part_size(intent_id, index, previous, text[start:high]) <= MAX_CAPTURE_INTENT_BYTES:
        return high
    while low < high:
        middle = (low + high + 1) // 2
        if _part_size(intent_id, index, previous, text[start:middle]) <= MAX_CAPTURE_INTENT_BYTES:
            low = middle
        else:
            high = middle - 1
    return low


def _next_part(text: str, start: int, intent_id: str, index: int, previous: str | None) -> tuple[int, bytes]:
    end = _fitting_end(text, start, intent_id, index, previous)
    if end == start:
        raise ValueError("capture transport cannot hold breadcrumb part metadata and content")
    record = _part_record(intent_id, index, previous, text[start:end])
    return end, _record_bytes(record, "breadcrumb-part-v1.json")


def encode_parts(intent_id: str, content: bytes) -> tuple[bytes, ...]:
    text = content.decode("utf-8", errors="strict")
    parts: list[bytes] = []
    start, previous = 0, None
    while start < len(text):
        start, encoded = _next_part(text, start, intent_id, len(parts), previous)
        parts.append(encoded)
        previous = sha256_bytes(encoded)
    if not parts:
        raise ValueError("breadcrumb input must not be empty")
    return tuple(parts)


def make_manifest(anchor_bytes: bytes, parts: tuple[bytes, ...]) -> bytes:
    anchor = read_anchor(anchor_bytes)
    if not parts:
        raise ValueError("breadcrumb manifest requires complete parts")
    record = {
        "schema_version": MANIFEST_VERSION, "intent_id": anchor["intent_id"],
        "anchor_sha256": sha256_bytes(anchor_bytes),
        "input_sha256": anchor["input_sha256"], "input_bytes": anchor["input_bytes"],
        "part_count": len(parts), "last_part_sha256": sha256_bytes(parts[-1]),
    }
    encoded = _record_bytes(record, "capture-intent-v2.json")
    by_digest = {sha256_bytes(part): part for part in parts}
    restore_input(encoded, anchor_bytes, by_digest.__getitem__)
    return encoded


def read_manifest(data: bytes) -> dict:
    return _read_record(data, "capture-intent-v2.json")


def _bound_anchor(manifest: dict, anchor_bytes: bytes) -> dict:
    _require_equal(sha256_bytes(anchor_bytes), manifest["anchor_sha256"], "breadcrumb anchor digest changed")
    anchor = read_anchor(anchor_bytes)
    fields = ("intent_id", "input_sha256", "input_bytes")
    _require_equal(tuple(anchor[k] for k in fields), tuple(manifest[k] for k in fields), "breadcrumb anchor conflicts")
    return anchor


def _verified_part(data: bytes, digest: str, intent_id: str, index: int) -> dict:
    _require_equal(sha256_bytes(data), digest, "breadcrumb part digest changed")
    record = read_part(data)
    _require_equal((record["intent_id"], record["index"]), (intent_id, index), "breadcrumb part binding conflicts")
    return record


def read_part(data: bytes) -> dict:
    return _read_record(data, "breadcrumb-part-v1.json")


def _restored_chunks(manifest: dict, read_part: Callable[[str], bytes]) -> list[bytes]:
    digest, byte_count = manifest["last_part_sha256"], 0
    chunks = []
    for index in reversed(range(manifest["part_count"])):
        record = _verified_part(read_part(digest), digest, manifest["intent_id"], index)
        chunk = record["text"].encode("utf-8")
        byte_count += len(chunk)
        if byte_count > manifest["input_bytes"]:
            raise ValueError("breadcrumb parts exceed their declared input")
        chunks.append(chunk)
        digest = record["previous_sha256"]
    _require_equal(digest, None, "breadcrumb chain has an unexpected predecessor")
    return chunks


def restore_input(manifest_bytes: bytes, anchor_bytes: bytes, read_part: Callable[[str], bytes]) -> bytes:
    manifest = read_manifest(manifest_bytes)
    _bound_anchor(manifest, anchor_bytes)
    content = b"".join(reversed(_restored_chunks(manifest, read_part)))
    _require_equal(
        (len(content), sha256_bytes(content)),
        (manifest["input_bytes"], manifest["input_sha256"]),
        "breadcrumb complete input changed",
    )
    return content
