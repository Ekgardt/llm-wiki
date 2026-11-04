"""Complete permanent sources through the existing Markdown transaction boundary.

Publishing a source alone never grants terminal completion or runtime cleanup.
"""
from __future__ import annotations

from pathlib import Path, PurePosixPath

import breadcrumb_protocol as protocol
from bounded_io import (
    MAX_KNOWLEDGE_PAGE_BYTES,
    _check_deadline,
    _validated_deadline,
    read_stable_bytes,
)
from llm_wiki.breadcrumb_documents import _FRONTMATTER as _FRONTMATTER
from llm_wiki.breadcrumb_documents import _RECORD_MARKER as _RECORD_MARKER
from llm_wiki.breadcrumb_documents import SOURCE_VERSION as SOURCE_VERSION
from llm_wiki.breadcrumb_documents import _bounded_document as _bounded_document
from llm_wiki.breadcrumb_documents import _document as _document
from llm_wiki.breadcrumb_documents import _embedded_record as _embedded_record
from llm_wiki.breadcrumb_documents import _read_head as _read_head
from llm_wiki.breadcrumb_documents import _require_exact as _require_exact
from llm_wiki.breadcrumb_documents import archived_source_path as archived_source_path
from llm_wiki.breadcrumb_documents import is_breadcrumb_document as is_breadcrumb_document
from llm_wiki.breadcrumb_documents import render_head as render_head
from llm_wiki.breadcrumb_documents import render_part as render_part
from llm_wiki.breadcrumb_documents import restore_source as restore_source
from llm_wiki.breadcrumb_documents import source_documents as source_documents
from llm_wiki.breadcrumb_documents import source_part_path as source_part_path
from llm_wiki.breadcrumb_documents import source_path as source_path
from llm_wiki.formats.canonical import canonical_json_bytes, sha256_bytes


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
    operation_id = "breadcrumb-source:" + sha256_bytes(canonical_json_bytes({
        "path": relative, "sha256": sha256_bytes(document),
    }))
    if _source_already_matches(coordinator.vault, relative, document):
        _commit_present_source_retry(coordinator, owner, operation_id, relative, document, preconditions)
        return
    _commit_source_attempt(coordinator, owner, operation_id, relative, document, preconditions)


def _commit_source_attempt(coordinator, owner, operation_id, relative, document, preconditions):
    from markdown_transaction import ABSENT, MarkdownChange

    with coordinator.writer_gate(owner=owner):
        coordinator.ensure_target_parent(relative)
        attempt_id, parent = coordinator.attempt_operation_id(operation_id)
        _apply_source_attempt(coordinator, attempt_id, parent, MarkdownChange.create(relative, document),
                              {**preconditions, relative: ABSENT})


def _commit_present_source_retry(coordinator, owner, operation_id, relative, document, preconditions):
    """Durably reassert identical bytes when a refused create lacks a committed retry."""
    from markdown_transaction import MarkdownChange

    with coordinator.writer_gate(owner=owner):
        attempt_id, parent = coordinator.attempt_operation_id(operation_id)
        if parent is None or coordinator.committed_attempt(operation_id) is not None:
            return
        _apply_source_attempt(coordinator, attempt_id, parent, MarkdownChange.replace(relative, document),
                              {**preconditions, relative: sha256_bytes(document)})


def _apply_source_attempt(coordinator, attempt_id, parent, change, preconditions):
    transaction = coordinator.prepare(
        [change], operation_id=attempt_id, preconditions=preconditions,
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
