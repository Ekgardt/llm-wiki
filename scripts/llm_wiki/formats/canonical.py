"""Canonical byte identity shared by all writers and readers."""
from __future__ import annotations

import hashlib
import json
import unicodedata


def canonical_json_bytes(value: object) -> bytes:
    """Encode the restricted JSON domain deterministically as UTF-8."""
    normalized = _canonical_value(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_key(key: object, seen: dict[str, object]) -> str:
    """One NFC-normalized object key, refusing a collision the encoding hides."""
    if not isinstance(key, str):
        raise TypeError("canonical JSON object keys must be strings")
    normalized = unicodedata.normalize("NFC", key)
    if normalized in seen:
        raise ValueError(f"normalized object-key collision: {normalized!r}")
    return normalized


def _canonical_mapping(value: dict) -> dict[str, object]:
    normalized: dict[str, object] = {}
    for key, item in value.items():
        normalized[_canonical_key(key, normalized)] = _canonical_value(item)
    return normalized


def _canonical_number(value: object) -> object:
    """Floats are refused: they have no canonical decimal form."""
    if isinstance(value, float):
        raise TypeError("canonical JSON does not permit float values")
    return value


def _canonical_atom(value: object) -> object:
    """None and booleans pass through; the caller has already excluded the rest."""
    if isinstance(value, (int, float)):
        return _canonical_number(value)
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    raise TypeError(f"canonical JSON does not permit {type(value).__name__} values")


def _canonical_scalar(value: object) -> object:
    """The scalar cases, kept apart so the shape of the domain reads at a glance."""
    if value is None:
        return value
    if isinstance(value, bool):
        return value
    return _canonical_atom(value)


def _canonical_value(value: object) -> object:
    if isinstance(value, list):
        return [_canonical_value(item) for item in value]
    if isinstance(value, dict):
        return _canonical_mapping(value)
    return _canonical_scalar(value)
