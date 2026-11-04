"""Read complete permanent breadcrumb sources under the caller's deadline."""
from __future__ import annotations

from pathlib import Path, PurePosixPath

from bounded_io import (
    MAX_KNOWLEDGE_PAGE_BYTES,
    _check_deadline,
    _validated_deadline,
    read_stable_bytes,
)

from llm_wiki.breadcrumb_documents import archived_source_path, restore_source


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
