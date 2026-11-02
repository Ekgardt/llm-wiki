"""Pure queue source identities; no database, executor, or runtime IO."""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from types import MappingProxyType

from llm_wiki.sqlite_contracts import _migration_error

_SOURCE_IDENTITY_KEYS = MappingProxyType(
    {
        "source_path": "paths",
        "logical_path": "paths",
        "source_digest": "digests",
        "digest": "digests",
        "hash": "digests",
        "daily_id": "daily_ids",
    }
)


def _collect_identity_from_mapping(
    value: dict[object, object], found: dict[str, list[str]]
) -> None:
    """Take identity strings from one mapping, then walk into its values."""
    for key, item in value.items():
        bucket = _SOURCE_IDENTITY_KEYS.get(str(key).casefold())
        if bucket is not None and isinstance(item, str):
            found[bucket].append(item)
        _collect_source_identity_strings(item, found)


def _collect_source_identity_strings(
    value: object, found: dict[str, list[str]]
) -> None:
    """Gather every source-identity string reachable from this payload value."""
    if isinstance(value, dict):
        _collect_identity_from_mapping(value, found)
        return
    if isinstance(value, list):
        for item in value:
            _collect_source_identity_strings(item, found)


def _source_identity_strings(payload: object) -> dict[str, list[str]]:
    """The paths, digests and daily ids a payload names, in encounter order."""
    found: dict[str, list[str]] = {"paths": [], "digests": [], "daily_ids": []}
    _collect_source_identity_strings(payload, found)
    return found


def _distinct_source_identities(payload: object) -> dict[str, set[str]]:
    """The distinct identity strings a payload names, daily id standing in."""
    found = {
        key: set(values) for key, values in _source_identity_strings(payload).items()
    }
    if not found["paths"] and len(found["daily_ids"]) == 1:
        found["paths"].add(f"knowledge/daily/{next(iter(found['daily_ids']))}.md")
    return found


def _payload_identity_fields(payload_json: str) -> dict[str, set[str]] | None:
    """The identity strings a stored payload names, or None when it cannot be read."""
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError:
        return None
    return {
        key: set(values) for key, values in _source_identity_strings(payload).items()
    }


def _identity_paths_or_daily(found: dict[str, list[str]]) -> list[str]:
    """The named paths, falling back to the page the first daily id names."""
    if found["paths"] or not found["daily_ids"]:
        return found["paths"]
    return [f"knowledge/daily/{found['daily_ids'][0]}.md"]


def _validated_failure_identity(path: str, digest: str) -> tuple[str, str] | None:
    """The pair, or None when it is not a usable source failure identity."""
    try:
        _validate_failure_identity(path, digest)
    except ValueError:
        return None
    return path, digest


def _unsafe_link_shape(logical_path: str) -> bool:
    """Whether the path's shape puts it outside the vault's naming rules."""
    if logical_path.startswith(("/", "\\")) or "\\" in logical_path:
        return True
    return any(part in {"", ".", ".."} for part in logical_path.split("/"))


def _unsafe_link_path(logical_path: object) -> bool:
    if not isinstance(logical_path, str) or not logical_path:
        return True
    if len(logical_path.encode("utf-8")) > 4096:
        return True
    return _unsafe_link_shape(logical_path)


def _link_pair(item: object) -> tuple[object, object]:
    if not isinstance(item, tuple) or len(item) != 2:
        raise ValueError("source links must be path and digest pairs")
    return item


def _normalized_source_link(item: object) -> tuple[str, str]:
    logical_path, source_digest = _link_pair(item)
    if _unsafe_link_path(logical_path):
        raise ValueError("source link path is invalid")
    if (
        not isinstance(source_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", source_digest) is None
    ):
        raise ValueError("source link digest is invalid")
    return logical_path, source_digest


def _normalized_source_links(
    source_links: tuple[tuple[str, str], ...] | list[tuple[str, str]],
) -> list[tuple[str, str]]:
    normalized = [_normalized_source_link(item) for item in source_links]
    if len(normalized) != len(set(normalized)):
        raise ValueError("source links must be unique")
    return normalized


def _require_canonical_daily_id(daily_id: object) -> None:
    """Refuse anything that is not a canonical `YYYY-MM-DD` daily id."""
    if (
        not isinstance(daily_id, str)
        or re.fullmatch(r"\d{4}-\d{2}-\d{2}", daily_id) is None
    ):
        raise ValueError("daily_id must be a canonical date")
    try:
        if date.fromisoformat(daily_id).isoformat() != daily_id:
            raise ValueError
    except ValueError as exc:
        raise ValueError("daily_id must be a canonical date") from exc


def _validate_source_identity(daily_id: str, source_digest: str) -> None:
    _require_canonical_daily_id(daily_id)
    if (
        not isinstance(source_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", source_digest) is None
    ):
        raise ValueError("source_digest must be a lowercase SHA-256")


def _validate_failure_identity(logical_path: str, source_digest: str) -> None:
    if (
        not isinstance(logical_path, str)
        or re.fullmatch(r"knowledge/daily/\d{4}-\d{2}-\d{2}\.md", logical_path)
        is None
    ):
        raise ValueError("logical_path must name a canonical daily source")
    _validate_source_identity(
        Path(logical_path).stem, source_digest
    )


def _source_identity_from_payload(payload_json: str) -> tuple[str, str] | None:
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError:
        return None
    found = _source_identity_strings(payload)
    paths = _identity_paths_or_daily(found)
    if not paths or not found["digests"]:
        return None
    return _validated_failure_identity(paths[0], found["digests"][0])



def _require_single_source_identity(found: dict[str, set[str]]) -> tuple[str, str]:
    """The one path and digest this payload names, or a refusal."""
    if len(found["paths"]) != 1 or len(found["digests"]) != 1:
        raise _migration_error(
            "queue_v2_source_identity_ambiguous",
            "queue v2 payload has an ambiguous source identity",
        )
    logical_path = next(iter(found["paths"]))
    source_digest = next(iter(found["digests"]))
    try:
        _validate_failure_identity(logical_path, source_digest)
    except ValueError as exc:
        raise _migration_error(
            "queue_v2_source_identity_ambiguous",
            "queue v2 payload has an invalid source identity",
        ) from exc
    return (logical_path, source_digest)


def _queue_v3_source_links(payload_bytes: bytes) -> tuple[tuple[str, str], ...]:
    try:
        payload = json.loads(payload_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ()
    found = _distinct_source_identities(payload)
    if not found["paths"] and not found["digests"]:
        return ()
    return (_require_single_source_identity(found),)

