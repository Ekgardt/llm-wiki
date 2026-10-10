"""Complete permanent sources through the existing Markdown transaction boundary.

Publishing a source alone never grants terminal completion or runtime cleanup.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from datetime import date
from pathlib import Path, PurePosixPath

import breadcrumb_protocol as protocol
from bounded_io import (
    MAX_KNOWLEDGE_PAGE_BYTES,
    _check_deadline,
    _validated_deadline,
    read_stable_bytes,
)
from reliable_memory import canonical_json_bytes, sha256_bytes

SOURCE_VERSION = "breadcrumb-source/v1"
_RECORD_MARKER = b"## Integrity record\n\n    "
_FRONTMATTER = (
    "---\ntype: raw-source\nsource_authority: session\nconfidence: medium\n"
    "record_schema: breadcrumb-source/v1\n---\n\n"
)


def source_path(anchor: bytes) -> str:
    record = protocol.read_anchor(anchor)
    return f"knowledge/raw/sessions/{record['day']}/{record['intent_id']}.breadcrumb.md"


def source_part_path(anchor: bytes, digest: str) -> str:
    fingerprint = protocol.require_digest(digest)
    # The full part digest already binds the occurrence. Repeating both hashes
    # exceeds the existing Markdown transaction component limit (128 bytes).
    name = f"{fingerprint}.breadcrumb-part.md"
    return str(PurePosixPath(source_path(anchor)).with_name(name))


def is_breadcrumb_document(path: Path) -> bool:
    """Reserved full-digest filenames are integrity records, not sessions."""
    return re.fullmatch(r"[0-9a-f]{64}\.breadcrumb(?:-part)?\.md", path.name) is not None


def archived_source_path(relative: str) -> str:
    path = PurePosixPath(relative)
    if len(path.parts) != 5 or path.parts[:3] != ("knowledge", "raw", "sessions"):
        raise ValueError("breadcrumb source has no canonical archive location")
    day = path.parent.name
    _require_exact(date.fromisoformat(day).isoformat(), day)
    return f"knowledge/raw/sessions/archive/{day[:7]}/{day}/{path.name}"


def _bounded_document(data: bytes) -> bytes:
    if len(data) > MAX_KNOWLEDGE_PAGE_BYTES:
        raise ValueError("breadcrumb source exceeds the common Markdown reader bound")
    return data


def _document(title: str, links: str, record: bytes) -> bytes:
    prefix = f"{_FRONTMATTER}# {title}\n\n{links}\n\n"
    return _bounded_document(prefix.encode() + _RECORD_MARKER + record + b"\n")


def _embedded_record(document: bytes) -> bytes:
    _bounded_document(document)
    _prefix, marker, record = document.partition(_RECORD_MARKER)
    if not marker or not record.endswith(b"\n"):
        raise ValueError("breadcrumb source has no complete integrity record")
    return record[:-1]


def _require_exact(actual: object, expected: object) -> None:
    if actual != expected:
        raise ValueError("breadcrumb permanent evidence changed or conflicts")


def render_head(manifest: bytes, anchor: bytes) -> bytes:
    record = protocol.read_manifest(manifest)
    occurrence = protocol.read_anchor(anchor)
    _require_exact(record["anchor_sha256"], sha256_bytes(anchor))
    _require_exact(record["intent_id"], occurrence["intent_id"])
    last = PurePosixPath(source_part_path(anchor, record["last_part_sha256"])).name
    links = f"Source: captured event `{record['intent_id']}`.\n\n[Last part]({last})"
    payload = canonical_json_bytes({
        "schema_version": SOURCE_VERSION, "manifest": record, "anchor": occurrence,
    })
    return _document("Complete captured event", links, payload)


def render_part(anchor: bytes, part: bytes) -> bytes:
    record = protocol.read_part(part)
    occurrence = protocol.read_anchor(anchor)
    _require_exact(record["intent_id"], occurrence["intent_id"])
    head = PurePosixPath(source_path(anchor)).name
    links = f"Source: [complete captured event]({head})."
    if record["previous_sha256"] is not None:
        previous = PurePosixPath(source_part_path(anchor, record["previous_sha256"])).name
        links += f"\n\n[Previous part]({previous})"
    return _document(f"Captured event — part {record['index'] + 1}", links, part)


def source_documents(manifest: bytes, anchor: bytes, parts: tuple[bytes, ...]) -> Iterator[tuple[str, bytes]]:
    """Yield verified parts first and the head last; never truncate the source."""
    by_digest = {sha256_bytes(part): part for part in parts}
    protocol.restore_input(manifest, anchor, by_digest.__getitem__)
    _require_exact(len(parts), protocol.read_manifest(manifest)["part_count"])
    for part in parts:
        digest = sha256_bytes(part)
        yield source_part_path(anchor, digest), render_part(anchor, part)
    yield source_path(anchor), render_head(manifest, anchor)


def _read_head(document: bytes) -> tuple[bytes, bytes]:
    record = json.loads(_embedded_record(document).decode("utf-8", errors="strict"))
    _require_exact(set(record), {"schema_version", "manifest", "anchor"})
    _require_exact(record["schema_version"], SOURCE_VERSION)
    manifest = canonical_json_bytes(record["manifest"])
    anchor = canonical_json_bytes(record["anchor"])
    _require_exact(document, render_head(manifest, anchor))
    return manifest, anchor


def restore_source(head_relative: str, read_document: Callable[[str], bytes]) -> bytes:
    """Verify permanent evidence independently of disposable runtime parts.

    The caller must provide a contained, stable, bounded Markdown reader. Every
    requested part path is regenerated from the validated occurrence and digest.
    """
    manifest, anchor = _read_head(read_document(head_relative))
    _require_exact(head_relative, source_path(anchor))

    def read_part(digest: str) -> bytes:
        document = read_document(source_part_path(anchor, digest))
        part = _embedded_record(document)
        _require_exact(document, render_part(anchor, part))
        return part

    return protocol.restore_input(manifest, anchor, read_part)


def _source_target(vault: Path, relative: str) -> Path:
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("breadcrumb source path is not relative and contained")
    if path.parts[:3] != ("knowledge", "raw", "sessions"):
        raise ValueError("breadcrumb source is outside session evidence")
    target = vault / relative
    target.resolve().relative_to(vault.resolve(strict=True))
    return target


def _read_source_document(vault: Path, relative: str, *, deadline: float | None = None) -> bytes:
    try:
        return _read_exact_source_document(vault, relative, deadline=deadline)
    except FileNotFoundError:
        return _read_exact_source_document(vault, archived_source_path(relative), deadline=deadline)


def _read_exact_source_document(vault: Path, relative: str, *, deadline: float | None = None) -> bytes:
    _check_deadline(deadline, "breadcrumb permanent source")
    return read_stable_bytes(
        _source_target(vault, relative), MAX_KNOWLEDGE_PAGE_BYTES,
        label="breadcrumb permanent source", deadline=deadline,
    )


def read_permanent_source(vault: Path, head_relative: str, *, deadline: float | None = None) -> bytes:
    """Read a complete event under one optional caller-owned monotonic deadline.

    Checks are cooperative: a single blocked OS call cannot be preempted here.
    Expiry never returns partial evidence or grants a fresh archive-read budget.
    """
    deadline = _validated_deadline(deadline)
    _check_deadline(deadline, "breadcrumb permanent source")

    def reader(relative: str) -> bytes:
        return _read_source_document(vault, relative, deadline=deadline)

    content = restore_source(head_relative, reader)
    _check_deadline(deadline, "breadcrumb permanent source")
    return content


def _source_already_matches(vault: Path, relative: str, document: bytes) -> bool:
    try:
        current = _read_source_document(vault, relative)
    except FileNotFoundError:
        return False
    _require_exact(current, document)
    return True


def _create_source_document(coordinator, owner, relative: str, document: bytes, preconditions: dict) -> None:
    if _source_already_matches(coordinator.vault, relative, document):
        return
    operation_id = "breadcrumb-source:" + sha256_bytes(canonical_json_bytes({
        "path": relative, "sha256": sha256_bytes(document),
    }))
    _commit_source_attempt(coordinator, owner, operation_id, relative, document, preconditions)


def _commit_source_attempt(coordinator, owner, operation_id, relative, document, preconditions):
    from markdown_transaction import ABSENT, MarkdownChange

    with coordinator.writer_gate(owner=owner):
        coordinator.ensure_target_parent(relative)
        attempt_id, parent = coordinator.attempt_operation_id(operation_id)
        transaction = coordinator.prepare(
            [MarkdownChange.create(relative, document)], operation_id=attempt_id,
            preconditions={**preconditions, relative: ABSENT},
            _parent_transaction_id=parent,
        )
        committed = coordinator.apply(transaction.id)
        _require_exact(committed.state, "committed")


def _bound_source_manifest(active, bundle) -> dict:
    record = protocol.read_manifest(bundle.manifest)
    if active.seal_digest is None:
        raise ValueError("breadcrumb source requires a sealed capture binding")
    _require_exact(
        (active.intent_id, active.intent_sha256, sha256_bytes(bundle.content), len(bundle.content)),
        (record["intent_id"], sha256_bytes(bundle.manifest), record["input_sha256"], record["input_bytes"]),
    )
    return record


def publish_source(queue, coordinator, lease, intent_fence, owner, bundle) -> str:
    """Create immutable complete evidence; refuse changes to existing source bytes."""
    from flush_memory import _capture_transaction_preconditions
    from model_dlp import require_safe_publication

    active = queue.active_capture_binding(None, lease.id)
    _bound_source_manifest(active, bundle)
    require_safe_publication(bundle.content)
    coordinator.project_capture_binding(active, intent_fence=intent_fence)
    preconditions = _capture_transaction_preconditions(active, intent_fence)
    for relative, document in source_documents(bundle.manifest, bundle.anchor, bundle.parts):
        _create_source_document(coordinator, owner, relative, document, preconditions)
    head = source_path(bundle.anchor)
    _require_exact(read_permanent_source(coordinator.vault, head), bundle.content)
    return head
