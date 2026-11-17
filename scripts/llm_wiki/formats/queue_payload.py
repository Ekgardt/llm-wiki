"""Canonical queue payload validation, without queue execution or runtime I/O."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from .canonical import canonical_json_bytes

# Existing wire limits, moved unchanged; their historical rationale is not enlarged here.
MAX_QUEUE_PAYLOAD_BYTES = 1024 * 1024
_MAX_QUEUE_DEPTH = 32
_MAX_QUEUE_STRING_BYTES = 256 * 1024
_MAX_QUEUE_CONTAINER_MEMBERS = 1024


@dataclass(frozen=True)
class PayloadValidation:
    raw: bytes
    input_hash: str
    payload: dict[str, object] | None
    code: str | None


def _reject_duplicate_payload_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError("duplicate payload key")
        payload[key] = value
    return payload


def _reject_payload_constant(_value: str) -> object:
    raise ValueError("non-finite payload number")


def _parse_payload_integer(value: str) -> int:
    if len(value.encode("ascii")) > _MAX_QUEUE_STRING_BYTES:
        raise ValueError("payload integer token exceeds its bound")
    return int(value)


def _validate_payload_string(value: str, message: str) -> None:
    if len(value.encode("utf-8", errors="strict")) > _MAX_QUEUE_STRING_BYTES:
        raise ValueError(message)


def _validate_payload_array(value: list[object], *, depth: int) -> None:
    if len(value) > _MAX_QUEUE_CONTAINER_MEMBERS:
        raise ValueError("payload array exceeds its member bound")
    for item in value:
        _validate_payload_value(item, depth=depth + 1)


def _validate_payload_object(value: dict[object, object], *, depth: int) -> None:
    if len(value) > _MAX_QUEUE_CONTAINER_MEMBERS:
        raise ValueError("payload object exceeds its member bound")
    for key, item in value.items():
        _validate_payload_key(key)
        _validate_payload_value(item, depth=depth + 1)


def _validate_payload_key(key: object) -> None:
    if not isinstance(key, str):
        raise ValueError("payload keys must be strings")
    _validate_payload_string(key, "payload key exceeds its byte bound")


def _is_payload_integer(value: object) -> bool:
    """A value JSON keeps exactly: null, a boolean, or an integer."""
    return value is None or isinstance(value, (bool, int))


def _reject_payload_float(value: object) -> None:
    if isinstance(value, float):
        raise ValueError("payload floats are not permitted")


def _validate_payload_scalar(value: object) -> bool:
    """True when the value is a scalar we accept as it stands."""
    if _is_payload_integer(value):
        return True
    _reject_payload_float(value)
    if not isinstance(value, str):
        return False
    _validate_payload_string(value, "payload string exceeds its byte bound")
    return True


def _require_payload_depth(depth: int) -> None:
    if depth > _MAX_QUEUE_DEPTH:
        raise ValueError("payload exceeds its depth bound")


_PAYLOAD_CONTAINERS = (
    (list, _validate_payload_array),
    (dict, _validate_payload_object),
)


def _validate_payload_container(value: object, *, depth: int) -> bool:
    """Validate a container in place; False when the value is not one."""
    for kind, validate in _PAYLOAD_CONTAINERS:
        if isinstance(value, kind):
            validate(value, depth=depth)  # type: ignore[arg-type]
            return True
    return False


def _validate_payload_value(value: object, *, depth: int) -> None:
    _require_payload_depth(depth)
    if _validate_payload_scalar(value):
        return
    if not _validate_payload_container(value, depth=depth):
        raise ValueError("payload contains an unsupported value")


def validate_payload_blob(
    raw: bytes, stored_hash: str, *, parse: bool
) -> PayloadValidation:
    _check_payload_blob_arguments(raw, parse)
    input_hash = _payload_digest(raw)
    if not _payload_hash_matches(raw, stored_hash, input_hash):
        return PayloadValidation(raw, input_hash, None, "payload_hash_mismatch")
    if not parse:
        return PayloadValidation(raw, input_hash, None, None)
    return _parsed_payload_validation(raw, input_hash)


def _parsed_payload_validation(raw: bytes, input_hash: str) -> PayloadValidation:
    """The parsed payload, or the mismatch a payload that will not parse means."""
    payload = _parsed_canonical_payload(raw)
    if payload is None:
        return PayloadValidation(raw, input_hash, None, "payload_hash_mismatch")
    return PayloadValidation(raw, input_hash, payload, None)


def _check_payload_blob_arguments(raw: object, parse: object) -> None:
    if not isinstance(raw, bytes):
        raise TypeError("queue payload must be bytes")
    if not isinstance(parse, bool):
        raise TypeError("parse must be a boolean")


def _payload_digest(raw: bytes) -> str:
    """The payload's digest, read in bounded chunks."""
    digest = hashlib.sha256()
    view = memoryview(raw)
    for offset in range(0, len(view), 64 * 1024):
        digest.update(view[offset : offset + 64 * 1024])
    return digest.hexdigest()


def _payload_hash_matches(raw: bytes, stored_hash: object, input_hash: str) -> bool:
    """The payload is within bounds and hashes to the digest the row stored."""
    return (
        len(raw) <= MAX_QUEUE_PAYLOAD_BYTES
        and isinstance(stored_hash, str)
        and re.fullmatch(r"[0-9a-f]{64}", stored_hash) is not None
        and input_hash == stored_hash
    )


def _parsed_canonical_payload(raw: bytes) -> dict[str, object] | None:
    """The payload object, when these bytes are its own canonical encoding."""
    try:
        payload = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_payload_keys,
            parse_constant=_reject_payload_constant,
            parse_int=_parse_payload_integer,
        )
        _validate_payload_value(payload, depth=1)
        if not isinstance(payload, dict) or canonical_json_bytes(payload) != raw:
            raise ValueError("payload is not a canonical object")
    except (
        json.JSONDecodeError,
        OverflowError,
        RecursionError,
        TypeError,
        UnicodeError,
        ValueError,
    ):
        return None
    return payload

