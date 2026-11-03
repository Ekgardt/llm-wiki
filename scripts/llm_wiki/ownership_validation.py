"""Pure ownership input and marker validation; registry execution stays separate."""
from __future__ import annotations

import json
import re
import sqlite3
from typing import cast

import process_liveness
from process_liveness import _is_plain_int

from llm_wiki.formats.canonical import canonical_json_bytes, sha256_bytes
from llm_wiki.ownership_contracts import (
    MarkerIdentity,
    OperationalOwnershipError,
    OwnerRole,
    ProcessIdentity,
)
from llm_wiki.restricted_paths import restricted_relative_path
from llm_wiki.runtime_contracts import RuntimeFileIdentity

_ROLES = frozenset(
    {
        "capture",
        "project",
        "markdown-writer",
        "queue-worker",
        "compile",
        "doctor",
        "nightly",
        "weekly",
        "lsp",
        "queue-operator",
        "repair",
        "runtime-deletion-check",
    }
)

_MARKER_ROLES = frozenset({"compile", "nightly", "weekly"})

# A marker identity is canonical JSON of a file identity and a pid, under 300 bytes; 4 KiB refuses
# a marker that is not one.
_MAX_MARKER_BYTES = 4096

def _validate_process(identity: ProcessIdentity) -> None:
    if not isinstance(identity, ProcessIdentity):
        raise TypeError("process must be a ProcessIdentity")
    if isinstance(identity.pid, bool) or not isinstance(identity.pid, int) or identity.pid <= 0:
        raise ValueError("process pid must be a positive integer")
    _bounded_text(identity.start_identity, "process start identity", process_liveness.MAX_START_IDENTITY_CHARS)

def _utf8_size(value: str, label: str) -> int:
    try:
        return len(value.encode("utf-8", errors="strict"))
    except UnicodeError as exc:
        raise ValueError(f"{label} must be valid UTF-8") from exc

def _bounded_text(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    if _utf8_size(value, label) > maximum or "\0" in value:
        raise ValueError(f"{label} exceeds its bound")
    return value

def _validate_role(role: object) -> OwnerRole:
    if not isinstance(role, str) or role not in _ROLES:
        raise ValueError("role is not one of the closed ownership roles")
    return cast(OwnerRole, role)

def _valid_marker_size(value: object) -> bool:
    return _is_plain_int(value) and 0 <= value <= _MAX_MARKER_BYTES

def _valid_marker_mtime(value: object) -> bool:
    return _is_plain_int(value) and value >= 0

def _require_marker_metadata(identity: RuntimeFileIdentity) -> None:
    if not _valid_marker_size(identity.size) or not _valid_marker_mtime(
        identity.mtime_ns
    ):
        raise ValueError("marker file identity has invalid metadata")

def _file_identity_value(identity: RuntimeFileIdentity) -> dict[str, object]:
    if not isinstance(identity, RuntimeFileIdentity):
        raise TypeError("marker file_identity must be a RuntimeFileIdentity")
    _bounded_text(identity.platform, "marker identity platform", 64)
    _bounded_text(identity.volume, "marker identity volume", 512)
    _bounded_text(identity.file_id, "marker identity file ID", 512)
    _require_marker_metadata(identity)
    return {
        "file_id": identity.file_id,
        "mtime_ns": identity.mtime_ns,
        "platform": identity.platform,
        "size": identity.size,
        "volume": identity.volume,
    }

def _marker_json(marker: MarkerIdentity) -> bytes:
    return canonical_json_bytes(
        {"file_identity": _file_identity_value(marker.file_identity), "pid": marker.pid}
    )

def _valid_marker_columns(path: object, digest: object, raw: object) -> bool:
    return isinstance(path, str) and isinstance(digest, str) and isinstance(raw, bytes)

def _runtime_file_identity(file_value: object) -> RuntimeFileIdentity:
    return RuntimeFileIdentity(
        platform=file_value["platform"],
        volume=file_value["volume"],
        file_id=file_value["file_id"],
        size=file_value["size"],
        mtime_ns=file_value["mtime_ns"],
    )

def _marker_from_columns(path: str, digest: str, raw: bytes) -> MarkerIdentity:
    try:
        value = json.loads(raw)
        marker = MarkerIdentity(
            relative_path=path,
            sha256=digest,
            file_identity=_runtime_file_identity(value["file_identity"]),
            pid=value["pid"],
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise OperationalOwnershipError("owner_record_invalid") from exc
    if canonical_json_bytes(value) != raw:
        raise OperationalOwnershipError("owner_record_invalid")
    return marker

def _marker_from_row(row: sqlite3.Row) -> MarkerIdentity | None:
    values = (row["marker_path"], row["marker_sha256"], row["marker_identity_json"])
    if values == (None, None, None):
        return None
    path, digest, raw = values
    if not _valid_marker_columns(path, digest, raw):
        raise OperationalOwnershipError("owner_record_invalid")
    return _marker_from_columns(path, digest, raw)

def _require_marker_given(role: OwnerRole, marker: object) -> None:
    if marker is None:
        raise ValueError(f"{role} ownership requires marker identity")

def _require_marker_presence(role: OwnerRole, marker: object) -> None:
    """A marker role must carry one; every other role must not."""
    if role in _MARKER_ROLES:
        _require_marker_given(role, marker)
        return
    if marker is not None:
        raise ValueError(f"{role} ownership does not accept marker identity")

def _require_marker_identity_fields(
    marker: MarkerIdentity, process: ProcessIdentity
) -> None:
    if re.fullmatch(r"[0-9a-f]{64}", marker.sha256) is None:
        raise ValueError("marker sha256 must be lowercase 64-hex")
    if marker.pid != process.pid:
        raise ValueError("marker pid does not match the owner process")

def _validated_marker_path(marker: object, process: ProcessIdentity) -> str:
    if not isinstance(marker, MarkerIdentity):
        raise TypeError("marker must be a MarkerIdentity")
    relative = restricted_relative_path(marker.relative_path, ("run",)).as_posix()
    if len(relative.encode("utf-8")) > 4096:
        raise ValueError("marker path exceeds its bound")
    _require_marker_identity_fields(marker, process)
    return relative

def _validated_marker_json(marker: MarkerIdentity) -> bytes:
    marker_json = _marker_json(marker)
    if len(marker_json) > _MAX_MARKER_BYTES:
        raise ValueError("marker identity exceeds its bound")
    return marker_json

def _require_marker_match(
    before: object, after: object, content: bytes, marker: MarkerIdentity
) -> None:
    if before != marker.file_identity or after != marker.file_identity:
        raise OperationalOwnershipError("marker_identity_invalid")
    if sha256_bytes(content) != marker.sha256:
        raise OperationalOwnershipError("marker_identity_invalid")

# Retain historical serialized callable paths.
_validate_process.__module__ = "operational_ownership"
_utf8_size.__module__ = "operational_ownership"
_bounded_text.__module__ = "operational_ownership"
_validate_role.__module__ = "operational_ownership"
_valid_marker_size.__module__ = "operational_ownership"
_valid_marker_mtime.__module__ = "operational_ownership"
_require_marker_metadata.__module__ = "operational_ownership"
_file_identity_value.__module__ = "operational_ownership"
_marker_json.__module__ = "operational_ownership"
_valid_marker_columns.__module__ = "operational_ownership"
_runtime_file_identity.__module__ = "operational_ownership"
_marker_from_columns.__module__ = "operational_ownership"
_marker_from_row.__module__ = "operational_ownership"
_require_marker_given.__module__ = "operational_ownership"
_require_marker_presence.__module__ = "operational_ownership"
_require_marker_identity_fields.__module__ = "operational_ownership"
_validated_marker_path.__module__ = "operational_ownership"
_validated_marker_json.__module__ = "operational_ownership"
_require_marker_match.__module__ = "operational_ownership"
