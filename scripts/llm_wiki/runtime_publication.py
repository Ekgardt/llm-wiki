"""Extracted runtime filesystem owner; public compatibility remains in reliable_memory."""
from __future__ import annotations

import contextlib
import os
import re
import secrets
from pathlib import Path
from typing import Literal

from llm_wiki.formats.canonical import sha256_bytes
from llm_wiki.runtime_contracts import RuntimeFileIdentity, UnsafeStateRoot
from llm_wiki.runtime_environment import (
    harden_runtime_owner_only,
    known_network_path,
    require_applied_mode,
    windows_reparse_point,
)
from llm_wiki.runtime_records import capture_runtime_file_identity, read_runtime_bytes
from llm_wiki.runtime_sync import fsync_directory, fsync_file, metadata_durability_error
from llm_wiki.sqlite_contracts import require_positive_int


def publication_digest(path: Path, parent: Path, *, max_bytes: int) -> str | None:
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    return sha256_bytes(read_runtime_bytes(path, parent, max_bytes=max_bytes))


def durable_publish_file(
    staged: Path,
    destination: Path,
    *,
    replace: bool,
    expected_sha256: str,
    max_bytes: int,
) -> Literal["published", "adopted", "duplicate"]:
    """Publish one sibling file and return published, adopted, or duplicate."""
    require_publication_arguments(replace, expected_sha256, max_bytes)
    staged, destination, parent = resolved_sibling_pair(staged, destination)
    settled = settled_publication(staged, destination, parent, expected_sha256, max_bytes)
    if settled is not None:
        return settled
    move_into_place(staged, destination, parent, replace=replace)
    if publication_digest(destination, parent, max_bytes=max_bytes) != expected_sha256:
        raise RuntimeError("durable publication conflict: destination read-back failed")
    return "published"


def require_publication_arguments(
    replace: object, expected_sha256: str, max_bytes: object
) -> None:
    if not isinstance(replace, bool):
        raise TypeError("replace must be a boolean")
    if re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
        raise ValueError("expected_sha256 must be lowercase 64-hex")
    require_positive_int("max_bytes", max_bytes)


def resolved_parents(staged: Path, destination: Path) -> tuple[Path, Path]:
    try:
        return staged.parent.resolve(strict=True), destination.parent.resolve(strict=True)
    except OSError as exc:
        raise PermissionError("publication parent must exist") from exc


def require_distinct_siblings(
    staged: Path, destination: Path, staged_parent: Path, destination_parent: Path
) -> None:
    if staged_parent != destination_parent:
        raise ValueError("staged and destination must be sibling paths")
    if os.path.normcase(staged.name) == os.path.normcase(destination.name):
        raise ValueError("staged and destination must be distinct names")


def resolved_sibling_pair(staged: Path, destination: Path) -> tuple[Path, Path, Path]:
    """Both paths under one resolved local parent, or a refusal naming why not."""
    staged = Path(staged)
    destination = Path(destination)
    staged_parent, destination_parent = resolved_parents(staged, destination)
    require_distinct_siblings(staged, destination, staged_parent, destination_parent)
    if known_network_path(staged_parent) or windows_reparse_point(staged_parent):
        raise UnsafeStateRoot("durable publication requires one local non-reparse parent")
    return staged_parent / staged.name, staged_parent / destination.name, staged_parent


def adopted_or_duplicate(staged_digest: object, expected_sha256: str, parent: Path):
    """The destination already holds the expected bytes; what the staged file adds."""
    if staged_digest is None:
        fsync_parent_on_posix(parent)
        return "adopted"
    if staged_digest == expected_sha256:
        return "duplicate"
    raise RuntimeError("durable publication conflict: staged bytes do not match")


def fsync_parent_on_posix(parent: Path) -> None:
    if os.name == "nt":
        return
    fsync_directory(parent)


def settled_publication(
    staged: Path, destination: Path, parent: Path, expected_sha256: str, max_bytes: int
):
    """An outcome reached without moving anything, or None when a move is due."""
    staged_digest = publication_digest(staged, parent, max_bytes=max_bytes)
    destination_digest = publication_digest(destination, parent, max_bytes=max_bytes)
    if destination_digest == expected_sha256:
        return adopted_or_duplicate(staged_digest, expected_sha256, parent)
    if staged_digest != expected_sha256:
        raise RuntimeError("durable publication conflict: expected bytes are unavailable")
    return None


def move_windows(staged: Path, destination: Path, *, replace: bool) -> None:
    import windows_workspace

    try:
        windows_workspace.flush_file_path(staged)
        windows_workspace.move_file_write_through(staged, destination, replace=replace)
    except FileExistsError:
        raise
    except (OSError, RuntimeError) as exc:
        raise metadata_durability_error("Windows metadata publication failed", exc) from exc


def replace_in_place(staged: Path, destination: Path) -> None:
    try:
        os.replace(staged, destination)
    except OSError as exc:
        raise metadata_durability_error("metadata replacement failed", exc) from exc


def unlink_staged(staged: Path, parent: Path) -> None:
    try:
        os.unlink(staged)
    except OSError:
        fsync_directory(parent)
        raise


def link_then_unlink(staged: Path, destination: Path, parent: Path) -> None:
    try:
        os.link(staged, destination, follow_symlinks=False)
    except FileExistsError:
        raise
    except OSError as exc:
        raise metadata_durability_error("metadata publication link failed", exc) from exc
    unlink_staged(staged, parent)


def move_posix(staged: Path, destination: Path, parent: Path, *, replace: bool) -> None:
    try:
        fsync_file(staged)
    except OSError as exc:
        raise metadata_durability_error("staged file fsync failed", exc) from exc
    if replace:
        replace_in_place(staged, destination)
    else:
        link_then_unlink(staged, destination, parent)
    fsync_directory(parent)


def move_into_place(
    staged: Path, destination: Path, parent: Path, *, replace: bool
) -> None:
    if os.name == "nt":
        move_windows(staged, destination, replace=replace)
        return
    move_posix(staged, destination, parent, replace=replace)


def publish_runtime_file(
    path: Path,
    data: bytes,
    *,
    state_root: Path,
    create_only: bool,
    expected: RuntimeFileIdentity | None = None,
    expected_sha256: str | None = None,
    mode: int = 0o600,
) -> RuntimeFileIdentity:
    """Publish runtime bytes through the shared checked durability primitive."""
    require_runtime_arguments(data, create_only, mode, expected, expected_sha256)
    root = Path(state_root).resolve(strict=True)
    parent, destination = resolved_runtime_destination(path, root)
    require_replacement_evidence(
        destination, root, create_only, expected, expected_sha256
    )
    staged = parent / f".{destination.name}.{secrets.token_hex(16)}.tmp"
    write_staged_bytes(staged, data, mode)
    harden_runtime_owner_only(staged, mode)
    publish_staged(staged, destination, parent, data, create_only, expected)
    verify_published_runtime_permissions(destination, mode)
    published = capture_runtime_file_identity(destination, state_root=root)
    if published.size != len(data):
        raise RuntimeError("published runtime file size changed during read-back")
    return published


def verify_published_runtime_permissions(path: Path, mode: int) -> None:
    """Same-parent publication preserves permissions; any drift must remain a refusal."""
    if os.name == "nt":
        from llm_wiki.windows_acl import verify_windows_owner_acl  # noqa: E402

        verify_windows_owner_acl(path)
        return
    require_applied_mode(path, mode)


def require_runtime_mode(mode: object) -> None:
    if isinstance(mode, bool) or not isinstance(mode, int) or not 0 <= mode <= 0o777:
        raise ValueError("mode must be a valid permission mode")


def require_create_only_carries_no_evidence(
    create_only: bool, expected: object, expected_sha256: object
) -> None:
    if not create_only:
        return
    if expected is not None or expected_sha256 is not None:
        raise ValueError("create-only publication does not accept replacement evidence")


def require_replacement_carries_evidence(
    create_only: bool, expected: object, expected_sha256: object
) -> None:
    if create_only:
        return
    if expected is None or expected_sha256 is None:
        raise ValueError("replacement requires expected identity and SHA-256")


def require_replacement_shape(
    create_only: bool, expected: object, expected_sha256: object
) -> None:
    require_create_only_carries_no_evidence(create_only, expected, expected_sha256)
    require_replacement_carries_evidence(create_only, expected, expected_sha256)


def require_runtime_payload(data: object, create_only: object) -> None:
    if not isinstance(data, bytes):
        raise TypeError("runtime file data must be bytes")
    if not isinstance(create_only, bool):
        raise TypeError("create_only must be a boolean")


def require_runtime_arguments(
    data: object,
    create_only: object,
    mode: object,
    expected: object,
    expected_sha256: object,
) -> None:
    require_runtime_payload(data, create_only)
    require_runtime_mode(mode)
    require_replacement_shape(create_only, expected, expected_sha256)
    if expected_sha256 is not None and re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
        raise ValueError("expected_sha256 must be lowercase 64-hex")


def resolved_runtime_destination(path: Path, root: Path) -> tuple[Path, Path]:
    destination = Path(path)
    try:
        parent = destination.parent.resolve(strict=True)
        parent.relative_to(root)
    except (OSError, ValueError) as exc:
        raise PermissionError("runtime publication path is outside the state root") from exc
    return parent, parent / destination.name


def require_unchanged_identity(current: object, expected: object) -> None:
    if current != expected:
        raise PermissionError("runtime file identity changed before publication")


def require_replacement_evidence(
    destination: Path,
    root: Path,
    create_only: bool,
    expected: object,
    expected_sha256: object,
) -> None:
    """The file being replaced is still the one the caller measured."""
    if create_only:
        return
    current = capture_runtime_file_identity(destination, state_root=root)
    require_unchanged_identity(current, expected)
    current_bytes = read_runtime_bytes(destination, root, max_bytes=max(1, current.size))
    if sha256_bytes(current_bytes) != expected_sha256:
        raise PermissionError("runtime file bytes changed before publication")


def write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("runtime staging write made no progress")
        view = view[written:]



def write_staged_bytes(staged: Path, data: bytes, mode: int) -> None:
    descriptor = os.open(
        staged,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        write_all(descriptor, data)
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        discard_staged(staged)
        raise
    else:
        os.close(descriptor)


def discard_staged(staged: Path) -> None:
    with contextlib.suppress(OSError):
        staged.unlink()


def publish_staged(
    staged: Path,
    destination: Path,
    parent: Path,
    data: bytes,
    create_only: bool,
    expected: object,
) -> None:
    max_bytes = max(1, len(data), 0 if expected is None else expected.size)
    try:
        outcome = durable_publish_file(
            staged,
            destination,
            replace=not create_only,
            expected_sha256=sha256_bytes(data),
            max_bytes=max_bytes,
        )
    except BaseException:
        discard_staged(staged)
        raise
    if outcome != "duplicate":
        return
    staged.unlink()
    fsync_directory(parent)


# Keep existing serialized public callable/class paths.
durable_publish_file.__module__ = "reliable_memory"
publish_runtime_file.__module__ = "reliable_memory"
