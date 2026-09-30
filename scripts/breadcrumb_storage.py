"""Durable breadcrumb publication on the existing fenced v3 capture boundary.

This module does not enable host producers. Records are accepted only once the
complete manifest is durable; queue registration can then be retried without
the producer's memory or another model call.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path

import breadcrumb_protocol as protocol
from bounded_io import _check_deadline
from memory_state import MAX_CAPTURE_INTENT_BYTES
from model_dlp import require_safe_publication
from reliable_memory import (
    canonical_json_bytes,
    publish_runtime_file,
    read_runtime_bytes,
    sha256_bytes,
)
from secret_redact import describe_error

# A distinct handler dispatch version; existing session captures keep version 1.
HANDLER_VERSION = 2


@dataclass(frozen=True)
class BreadcrumbBundle:
    manifest: bytes
    anchor: bytes
    parts: tuple[bytes, ...]
    content: bytes


@dataclass(frozen=True)
class BreadcrumbPublication:
    intent_id: str
    registered: bool
    registration_error: str | None = None


def anchor_path(state_root: Path, intent_id: str) -> Path:
    identity = protocol.require_digest(intent_id)
    return state_root / "run/capture-intents/pending" / identity[:2] / f"{identity}.anchor"


def part_path(state_root: Path, intent_id: str, digest: str) -> Path:
    identity = protocol.require_digest(intent_id)
    fingerprint = protocol.require_digest(digest)
    return anchor_path(state_root, identity).with_name(f"{identity}.{fingerprint}.part")


def _read(state_root: Path, path: Path) -> bytes:
    return read_runtime_bytes(path, state_root, max_bytes=MAX_CAPTURE_INTENT_BYTES, owner_only=True)


def _publish(state_root: Path, path: Path, content: bytes) -> None:
    publish_runtime_file(path, content, state_root=state_root, create_only=True, mode=0o600)


def _existing_anchor(state_root: Path, intent_id: str) -> bytes | None:
    try:
        return _read(state_root, anchor_path(state_root, intent_id))
    except FileNotFoundError:
        return None


def _require_same_input(anchor: bytes, content: bytes) -> None:
    record = protocol.read_anchor(anchor)
    expected = (sha256_bytes(content), len(content))
    if (record["input_sha256"], record["input_bytes"]) != expected:
        raise ValueError("breadcrumb occurrence conflicts with its accepted input")


def _require_same_host_time(anchor: bytes, occurred_at: datetime, time_origin: str) -> None:
    record = protocol.read_anchor(anchor)
    if record["time_origin"] != "host" or time_origin != "host":
        return
    if datetime.fromisoformat(record["occurred_at"]) != occurred_at:
        raise ValueError("breadcrumb occurrence conflicts with its host time")


def _selected_anchor(
    state_root: Path, intent_id: str, content: bytes, *,
    occurred_at: datetime, accepted_at: datetime, time_origin: str,
) -> bytes:
    existing = _existing_anchor(state_root, intent_id)
    if existing is not None:
        _require_same_input(existing, content)
        _require_same_host_time(existing, occurred_at, time_origin)
        return existing
    return protocol.make_anchor(
        intent_id, content, occurred_at=occurred_at, accepted_at=accepted_at,
        time_origin=time_origin,
    )


def _store_parts(state_root: Path, intent_id: str, parts: tuple[bytes, ...]) -> None:
    for part in parts:
        _publish(state_root, part_path(state_root, intent_id, sha256_bytes(part)), part)


def _store_manifest(state_root: Path, intent_id: str, manifest: bytes) -> None:
    from integration_adapter import _capture_relative_paths

    pending, _ready = _capture_relative_paths(intent_id)
    _publish(state_root, state_root / pending, manifest)


def _store_bundle(
    queue: object, coordinator: object, intent_id: str, content: bytes, *,
    occurred_at: datetime, accepted_at: datetime, time_origin: str,
) -> bytes:
    from integration_adapter import _capture_publication_fence, _ensure_capture_intent_directories

    state_root = Path(queue.state_root)
    _ensure_capture_intent_directories(state_root, intent_id)
    with _capture_publication_fence(queue, coordinator, intent_id):
        anchor = _selected_anchor(
            state_root, intent_id, content, occurred_at=occurred_at,
            accepted_at=accepted_at, time_origin=time_origin,
        )
        parts = protocol.encode_parts(intent_id, content)
        manifest = protocol.make_manifest(anchor, parts)
        _publish(state_root, anchor_path(state_root, intent_id), anchor)
        _store_parts(state_root, intent_id, parts)
        _store_manifest(state_root, intent_id, manifest)
    return manifest


def _read_before_deadline(state_root: Path, path: Path, deadline: float) -> bytes:
    _check_deadline(deadline, "breadcrumb verification")
    return _read(state_root, path)


def load_bundle(state_root: Path, manifest: bytes, *, deadline: float = float("inf")) -> BreadcrumbBundle:
    record = protocol.read_manifest(manifest)
    intent_id = record["intent_id"]
    anchor = _read_before_deadline(state_root, anchor_path(state_root, intent_id), deadline)
    read_parts: list[bytes] = []

    def read_part(digest: str) -> bytes:
        data = _read_before_deadline(state_root, part_path(state_root, intent_id, digest), deadline)
        read_parts.append(data)
        return data

    content = protocol.restore_input(manifest, anchor, read_part)
    _check_deadline(deadline, "breadcrumb verification")
    return BreadcrumbBundle(manifest, anchor, tuple(reversed(read_parts)), content)


def runtime_source_records(state_root: Path, bundle: BreadcrumbBundle) -> tuple[tuple[str, bytes], ...]:
    """Exact auxiliary records; discovery never uses a filename prefix glob."""
    intent_id = protocol.read_manifest(bundle.manifest)["intent_id"]
    anchor = anchor_path(state_root, intent_id)
    records = [(anchor.relative_to(state_root).as_posix(), bundle.anchor)]
    for part in bundle.parts:
        path = part_path(state_root, intent_id, sha256_bytes(part))
        records.append((path.relative_to(state_root).as_posix(), part))
    return tuple(records)


def _stored_manifest(state_root: Path, intent_id: str) -> bytes:
    from integration_adapter import _capture_relative_paths

    pending, ready = _capture_relative_paths(intent_id)
    try:
        return _read(state_root, state_root / ready)
    except FileNotFoundError:
        return _read(state_root, state_root / pending)


def load_bound_bundle(state_root: Path, intent_id: str, digest: str) -> BreadcrumbBundle:
    manifest = _stored_manifest(state_root, intent_id)
    actual = (protocol.read_manifest(manifest)["intent_id"], sha256_bytes(manifest))
    if actual != (intent_id, digest):
        raise ValueError("breadcrumb manifest conflicts with its capture binding")
    return load_bundle(state_root, manifest)


def register_manifest(queue: object, coordinator: object, manifest: bytes) -> None:
    bundle = load_bundle(Path(queue.state_root), manifest)
    intent_id = protocol.read_manifest(bundle.manifest)["intent_id"]
    _register_verified_manifest(queue, coordinator, manifest, intent_id, HANDLER_VERSION)


def _register_verified_manifest(queue, coordinator, manifest, intent_id, handler_version) -> None:
    from integration_adapter import _capture_relative_paths, _publish_capture_files_and_task

    pending, ready = _capture_relative_paths(intent_id)
    _publish_capture_files_and_task(
        queue, coordinator, intent_id=intent_id, payload=manifest,
        intent_sha256=sha256_bytes(manifest), pending_relative=pending,
        ready_relative=ready, handler_version=handler_version,
    )


def _register_or_retain(queue: object, coordinator: object, intent_id: str, manifest: bytes) -> BreadcrumbPublication:
    try:
        register_manifest(queue, coordinator, manifest)
    except Exception as error:
        stored = _stored_manifest(Path(queue.state_root), intent_id)
        if stored != manifest:
            raise ValueError("durable breadcrumb manifest changed") from error
        load_bundle(Path(queue.state_root), stored)
        return BreadcrumbPublication(intent_id, False, describe_error(error))
    return BreadcrumbPublication(intent_id, True)


def publish_breadcrumb(
    queue: object, coordinator: object, scope: Mapping[str, object],
    event: Mapping[str, object], *, occurred_at: datetime, accepted_at: datetime,
    time_origin: str,
) -> BreadcrumbPublication:
    """Persist complete redacted input before attempting queue bookkeeping."""
    content = canonical_json_bytes(dict(event))
    require_safe_publication(content)
    intent_id = protocol.occurrence_identity(scope)
    manifest = _store_bundle(
        queue, coordinator, intent_id, content, occurred_at=occurred_at,
        accepted_at=accepted_at, time_origin=time_origin,
    )
    return _register_or_retain(queue, coordinator, intent_id, manifest)


def _recover_pending_file(queue: object, coordinator: object, path: Path) -> int:
    from capture_adoption import verified_capture_handler
    from integration_adapter import _capture_relative_paths

    state_root = Path(queue.state_root)
    manifest = _read(state_root, path)
    identity = protocol.require_digest(path.stem)
    handler = verified_capture_handler(state_root, {"intent_id": identity}, manifest)
    pending, _ready = _capture_relative_paths(identity)
    if path != state_root / pending:
        raise ValueError("breadcrumb manifest path conflicts with its identity")
    _register_verified_manifest(queue, coordinator, manifest, identity, handler)
    return handler


def recover_pending(queue: object, coordinator: object) -> dict:
    """Recover accepted manifests even when no database row was ever written.

    Completed publications have no pending manifest. Scan only unfinished
    publication here, with no prefix cutoff that could hide a later valid file.
    Missing parts and unknown versions are retained and explicitly reported.
    The worker and nightly adoption pass share this reader.
    """
    result = {"recovered": [], "skipped": [], "legacy": 0}
    visit = partial(_recover_manifest_path, queue, coordinator, result=result)
    report_error = partial(_record_scan_error, queue, result=result)
    _visit_pending(Path(queue.state_root), visit, report_error)
    return result


def _recover_manifest_path(queue, coordinator, path: Path, *, result: dict) -> None:
    if path.suffix == ".json":
        _recover_pending_report(queue, coordinator, path, result)


def _directory_paths(directory: Path, state_root: Path, *, deadline: float):
    from integration_adapter import _validate_capture_directory

    _check_deadline(deadline, "breadcrumb inspection")
    _validate_capture_directory(directory, state_root)
    with os.scandir(directory) as entries:
        for entry in entries:
            _check_deadline(deadline, "breadcrumb inspection")
            yield Path(entry.path)


def _visit_pending(state_root: Path, visit, report_error, *, deadline: float = float("inf")) -> None:
    pending = state_root / "run/capture-intents/pending"
    try:
        for shard in _directory_paths(pending, state_root, deadline=deadline):
            _visit_pending_shard(shard, state_root, visit, report_error, deadline=deadline)
    except FileNotFoundError:
        return  # No pending directory is a normal fresh-vault state.
    except TimeoutError:
        raise
    except OSError as error:
        report_error(pending, error=error)


def _visit_pending_shard(shard: Path, state_root: Path, visit, report_error, *, deadline: float) -> None:
    try:
        for path in _directory_paths(shard, state_root, deadline=deadline):
            visit(path)
    except TimeoutError:
        raise
    except OSError as error:
        report_error(shard, error=error)


def inspect_pending_sources(state_root: Path, indexed: set[str], *, deadline: float) -> dict:
    """Read-only evidence inventory; no publication, ownership or queue mutation."""
    result = {"complete": 0, "incomplete": 0}
    seen = set(indexed)

    def visit(path):
        _inspect_pending_path(state_root, path, seen, result, deadline)

    def report_error(path, *, error):
        relative = path.relative_to(state_root).as_posix()
        raise ValueError(f"Cannot inspect {relative}: {describe_error(error)}") from error

    _visit_pending(state_root, visit, report_error, deadline=deadline)
    return result


def _inspect_pending_path(state_root, path, seen, result, deadline) -> None:
    _check_deadline(deadline, "breadcrumb inspection")
    identity = protocol.require_digest(path.name.partition(".")[0])
    if identity in seen:
        return
    seen.add(identity)
    result[_pending_source_status(state_root, identity, deadline)] += 1


def _pending_source_status(state_root: Path, identity: str, deadline: float) -> str:
    from capture_adoption import verified_capture_handler

    try:
        manifest = _stored_manifest(state_root, identity)
    except FileNotFoundError:
        return "incomplete"
    verified_capture_handler(state_root, {"intent_id": identity}, manifest, deadline=deadline)
    return "complete"


def _record_scan_error(queue, path: Path, result: dict, error: OSError) -> None:
    relative = path.relative_to(queue.state_root).as_posix()
    result["skipped"].append({
        "intent_id": None, "path": relative,
        "reason": f"Cannot inspect {relative}: {describe_error(error)}",
        "retried": False,
    })


def _recover_pending_report(queue: object, coordinator: object, path: Path, result: dict) -> None:
    from capture_diagnostics import is_contention

    try:
        handler = _recover_pending_file(queue, coordinator, path)
    except Exception as error:
        result["skipped"].append({
            "intent_id": path.stem, "reason": describe_error(error),
            "retried": is_contention(error),
        })
        return
    result["recovered"].append(path.stem)
    if handler == 1:
        result["legacy"] += 1
