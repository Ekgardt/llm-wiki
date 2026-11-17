"""Durable breadcrumb publication on the existing fenced v3 capture boundary.

This module does not enable host producers. Records are accepted only once the
complete manifest is durable; queue registration can then be retried without
the producer's memory or another model call.
"""
from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path

import breadcrumb_protocol as protocol
from bounded_io import check_deadline
from breadcrumb_bundle import (
    HANDLER_VERSION,
    anchor_path,
    load_bundle,
    part_path,
    read,
    stored_manifest,
)
from breadcrumb_protocol import BreadcrumbBundle as BreadcrumbBundle
from llm_wiki.formats.canonical import canonical_json_bytes, sha256_bytes
from model_dlp import require_safe_publication
from reliable_memory import publish_runtime_file
from secret_redact import describe_error


@dataclass(frozen=True)
class BreadcrumbPublication:
    intent_id: str
    registered: bool
    registration_error: str | None = None


def publish(state_root: Path, path: Path, content: bytes) -> None:
    publish_runtime_file(path, content, state_root=state_root, create_only=True, mode=0o600)


def _existing_anchor(state_root: Path, intent_id: str) -> bytes | None:
    try:
        return read(state_root, anchor_path(state_root, intent_id))
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


def store_parts(state_root: Path, intent_id: str, parts: tuple[bytes, ...]) -> None:
    for part in parts:
        publish(state_root, part_path(state_root, intent_id, sha256_bytes(part)), part)


def store_manifest(state_root: Path, intent_id: str, manifest: bytes) -> None:
    from capture_publication import (
        capture_relative_paths,
    )

    pending, _ready = capture_relative_paths(intent_id)
    publish(state_root, state_root / pending, manifest)


def store_owned_bundle(
    queue: object, intent_id: str, content: bytes, *,
    occurred_at: datetime, accepted_at: datetime, time_origin: str,
) -> bytes:
    state_root = Path(queue.state_root)
    anchor = _selected_anchor(
        state_root, intent_id, content, occurred_at=occurred_at,
        accepted_at=accepted_at, time_origin=time_origin,
    )
    parts = protocol.encode_parts(intent_id, content)
    manifest = protocol.make_manifest(anchor, parts)
    publish(state_root, anchor_path(state_root, intent_id), anchor)
    store_parts(state_root, intent_id, parts)
    store_manifest(state_root, intent_id, manifest)
    return manifest


def register_manifest(
    queue: object, coordinator: object, manifest: bytes, *, ownership=None,
) -> None:
    bundle = load_bundle(Path(queue.state_root), manifest)
    intent_id = protocol.read_manifest(bundle.manifest)["intent_id"]
    _register_verified_manifest(queue, coordinator, manifest, intent_id, HANDLER_VERSION, ownership=ownership)


def _register_verified_manifest(
    queue, coordinator, manifest, intent_id, handler_version, *, ownership=None,
) -> None:
    from capture_publication import (
        capture_relative_paths,
        publish_capture_files_and_task,
    )

    pending, ready = capture_relative_paths(intent_id)
    publish_capture_files_and_task(
        queue, coordinator, intent_id=intent_id, payload=manifest,
        intent_sha256=sha256_bytes(manifest), pending_relative=pending,
        ready_relative=ready, handler_version=handler_version, ownership=ownership,
    )


def register_or_retain(
    queue: object, coordinator: object, intent_id: str, manifest: bytes, *, ownership=None,
) -> BreadcrumbPublication:
    try:
        register_manifest(queue, coordinator, manifest, ownership=ownership)
    except Exception as error:
        stored = stored_manifest(Path(queue.state_root), intent_id)
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
    from capture_publication import (
        capture_publication_fence,
        ensure_capture_intent_directories,
    )

    ensure_capture_intent_directories(Path(queue.state_root), intent_id)
    with capture_publication_fence(queue, coordinator, intent_id) as ownership:
        manifest = store_owned_bundle(
            queue, intent_id, content, occurred_at=occurred_at,
            accepted_at=accepted_at, time_origin=time_origin,
        )
        return register_or_retain(queue, coordinator, intent_id, manifest, ownership=ownership)


def recover_pending_file(queue: object, coordinator: object, path: Path) -> int:
    from capture_handler_identity import (
        verified_capture_handler,
    )
    from capture_publication import (
        capture_relative_paths,
    )

    state_root = Path(queue.state_root)
    identity = protocol.require_digest(path.stem)
    pending, _ready = capture_relative_paths(identity)
    if path != state_root / pending:
        raise ValueError("breadcrumb manifest path conflicts with its identity")
    manifest = _recoverable_pending_manifest(state_root, path, identity)
    handler = verified_capture_handler(state_root, {"intent_id": identity}, manifest)
    _register_verified_manifest(queue, coordinator, manifest, identity, handler)
    return handler


def _recoverable_pending_manifest(state_root: Path, path: Path, identity: str) -> bytes:
    """A discovered pending record may already have a verified ready successor."""
    try:
        return read(state_root, path)
    except FileNotFoundError:
        return stored_manifest(state_root, identity)


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
    from capture_publication import (
        validate_capture_directory,
    )

    check_deadline(deadline, "breadcrumb inspection")
    validate_capture_directory(directory, state_root)
    with os.scandir(directory) as entries:
        for entry in entries:
            check_deadline(deadline, "breadcrumb inspection")
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
    check_deadline(deadline, "breadcrumb inspection")
    identity = _pending_source_identity(path)
    if identity in seen:
        return
    seen.add(identity)
    result[_pending_source_status(state_root, identity, deadline)] += 1


def _pending_source_identity(path: Path) -> str:
    name = path.name
    if name.startswith("."):
        name = _staged_pending_target(name)
    return protocol.require_digest(name.partition(".")[0])


def _staged_pending_target(name: str) -> str:
    match = re.fullmatch(
        r"\.(?P<target>[0-9a-f]{64}(?:\.anchor|\.json|\.[0-9a-f]{64}\.part))"
        r"\.[0-9a-f]{32}\.tmp",
        name,
    )
    if match is None:
        raise ValueError("unrecognized pending capture staging name")
    return match["target"]


def _pending_source_status(state_root: Path, identity: str, deadline: float) -> str:
    from capture_handler_identity import (
        verified_capture_handler,
    )

    try:
        manifest = stored_manifest(state_root, identity)
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
        handler = recover_pending_file(queue, coordinator, path)
    except Exception as error:
        result["skipped"].append({
            "intent_id": path.stem, "reason": describe_error(error),
            "retried": is_contention(error),
        })
        return
    result["recovered"].append(path.stem)
    if handler == 1:
        result["legacy"] += 1
