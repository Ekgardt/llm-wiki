"""Restricted paths with no connection or filesystem ownership."""
from __future__ import annotations

import re
from pathlib import PurePosixPath


def _is_posix_relative_text(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    return "\\" not in value and "//" not in value

def _require_posix_relative_text(value: object) -> None:
    if not _is_posix_relative_text(value):
        raise ValueError("path must be a non-empty normalized POSIX relative path")
    if re.match(r"^[A-Za-z]:", value):
        raise ValueError("drive-qualified paths are forbidden")

def _require_normalized_relative(path: PurePosixPath, value: str) -> None:
    unnormalized = (
        path.is_absolute()
        or str(path) != value
        or any(part in {"", ".", ".."} for part in path.parts)
    )
    if unnormalized:
        raise ValueError("path must be normalized and relative")

def _under_root(path: PurePosixPath, root: PurePosixPath) -> bool:
    return path == root or root in path.parents

def _require_inside_a_root(path: PurePosixPath, allowed_roots: tuple[str, ...]) -> None:
    roots = tuple(PurePosixPath(root) for root in allowed_roots)
    if not roots or not any(_under_root(path, root) for root in roots):
        raise ValueError("path is outside every allowed root")

def restricted_relative_path(value: str, allowed_roots: tuple[str, ...]) -> PurePosixPath:
    _require_posix_relative_text(value)
    path = PurePosixPath(value)
    _require_normalized_relative(path, value)
    _require_inside_a_root(path, allowed_roots)
    return path

# Preserve the historical public pickle/import identity.
restricted_relative_path.__module__ = "reliable_memory"
