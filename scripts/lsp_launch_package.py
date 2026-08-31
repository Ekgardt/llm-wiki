"""Executing a verified language server that must run from inside a package root.

The descriptor launch in `pyright_session._LaunchServerGuard` closes the
verify-then-execute race completely: it unlinks its verified copy and hands
`node` a file descriptor, so no path is left for anything to substitute. That
strategy cannot execute `typescript-language-server`. Measured on node v22.23.2:

    Module._compile(<cli.mjs>)                    -> ERR_REQUIRE_ASYNC_MODULE
    node /proc/self/fd/3 --stdio                  -> ENOENT open '/package.json'
    node --preserve-symlinks-main /proc/self/fd/3 -> ENOENT open '/proc/self/package.json'

`cli.mjs` is an ES module that reads `new URL('../package.json', import.meta.url)`.
`import.meta.url` is not settable and has no descriptor form, so an ESM entry
point must be given a real path inside a real directory.

What this module builds is the smallest such directory that keeps the property
worth keeping. The entry is the byte-for-byte digest-verified copy the guard
already made, moved -- not re-read from the install root -- into a freshly named
tree under the owner's private scratch root. Its `package.json` is **authored**
from a constant on the profile rather than copied, so no unverified byte of the
operator-writable install root is read at exec time. The tree is then sealed
read-only and re-verified through the sealed path, so what is verified is what
`node` will open, not a different copy of it.

The window this reopens, and the one it keeps closed, are written down in
`docs/research/2026-08-29-launching-a-verified-server-without-a-toctou-window.md`.
Short version: another user and the operator's own tools are still out of the
exec path; a process running as this same uid can `chmod` the tree and
substitute the entry, and nothing unprivileged prevents that.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from lsp_server_profile import PackageLaunch

# The sealed modes. `0o500` on a directory is what stops an entry being created,
# renamed or unlinked inside it without a `chmod` first; `0o400` on a file is
# what stops it being opened for writing.
SEALED_FILE_MODE = 0o400
SEALED_DIRECTORY_MODE = 0o500
PRIVATE_DIRECTORY_MODE = 0o700

MANIFEST_NAME = "package.json"
LAUNCH_PREFIX = "launch-"
MAX_LAUNCH_ENTRY_BYTES = 64 * 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024


class LaunchTreeError(RuntimeError):
    """The sealed launch tree is not the verified artifact it must be."""


@dataclass(frozen=True, slots=True)
class LaunchTree:
    """One sealed, owner-private directory holding exactly one server to launch."""

    root: Path
    entry: Path
    files: tuple[Path, ...]
    directories: tuple[Path, ...]


def _open_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)


def _require_owner_root(owner_root: object) -> Path:
    if not isinstance(owner_root, Path):
        raise TypeError("owner_root must be a Path")
    return owner_root


def _require_launch(launch: object) -> PackageLaunch:
    if not isinstance(launch, PackageLaunch):
        raise TypeError("launch must be a PackageLaunch")
    return launch


def _manifest_bytes(launch: PackageLaunch) -> bytes:
    """The `package.json` this build authors, rendered deterministically."""
    plain = {str(key): value for key, value in launch.manifest.items()}
    return json.dumps(plain, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _created_directories(root: Path, entry_relative: Path) -> tuple[Path, ...]:
    """Make the tree writable-by-owner-only, returning it deepest first."""
    made: list[Path] = [root]
    os.mkdir(root, PRIVATE_DIRECTORY_MODE)
    current = root
    for part in entry_relative.parts[:-1]:
        current = current / part
        os.mkdir(current, PRIVATE_DIRECTORY_MODE)
        made.append(current)
    return tuple(reversed(made))


def _write_sealed_file(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, SEALED_FILE_MODE)
    try:
        os.write(descriptor, payload)
    finally:
        os.close(descriptor)


def _identity(info: os.stat_result) -> tuple[int, int]:
    return (info.st_dev, info.st_ino)


def _place_entry(entry: Path, snapshot_path: Path, snapshot_descriptor: int) -> tuple[int, int]:
    """Move the verified copy into place, remembering the inode that moved."""
    identity = _identity(os.fstat(snapshot_descriptor))
    os.replace(snapshot_path, entry)
    os.chmod(entry, SEALED_FILE_MODE)
    return identity


def _seal(tree: LaunchTree) -> None:
    for directory in tree.directories:
        os.chmod(directory, SEALED_DIRECTORY_MODE)


def _unseal_one(directory: Path) -> None:
    _suppressed(lambda: os.chmod(directory, PRIVATE_DIRECTORY_MODE))


def _unseal(tree: LaunchTree) -> None:
    for directory in reversed(tree.directories):
        _unseal_one(directory)


def _digest_descriptor(descriptor: int, checkpoint: Callable[[], None]) -> str:
    digest = hashlib.sha256()
    total = 0
    while True:
        checkpoint()
        chunk = os.read(descriptor, _READ_CHUNK_BYTES)
        if not chunk:
            return digest.hexdigest()
        total += len(chunk)
        _require_entry_size(total)
        digest.update(chunk)


def _require_entry_size(total: int) -> None:
    if total > MAX_LAUNCH_ENTRY_BYTES:
        raise LaunchTreeError("sealed launch entry exceeds the server ceiling")


def _require_sealed_identity(descriptor: int, expected: tuple[int, int]) -> None:
    """The sealed path has to name the very inode the verified copy moved to."""
    info = os.fstat(descriptor)
    sealed = stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == SEALED_FILE_MODE
    if not sealed or _identity(info) != expected:
        raise LaunchTreeError("sealed launch entry is not the verified artifact")


def _require_sealed_digest(actual: str, expected_sha256: str) -> None:
    if actual != expected_sha256:
        raise LaunchTreeError("sealed launch entry digest does not match the pin")


def verify_sealed_entry(
    tree: LaunchTree,
    identity: tuple[int, int],
    expected_sha256: str,
    checkpoint: Callable[[], None],
) -> None:
    """Read back through the sealed path and prove it is the verified artifact.

    Opening by path is the point: the guard has already verified a descriptor,
    and what has to be established here is that the *path* `node` is about to
    open reaches those same bytes.
    """
    checkpoint()
    descriptor = os.open(tree.entry, _open_flags())
    try:
        _require_sealed_identity(descriptor, identity)
        actual = _digest_descriptor(descriptor, checkpoint)
    finally:
        os.close(descriptor)
    _require_sealed_digest(actual, expected_sha256)


def create_launch_tree(
    owner_root: Path,
    launch: PackageLaunch,
    *,
    snapshot_path: Path,
    snapshot_descriptor: int,
    expected_sha256: str,
    checkpoint: Callable[[], None],
) -> LaunchTree:
    """Seal the verified copy into an owner-private package root and prove it.

    Raises `LaunchTreeError` if what ends up at the sealed path is not the
    artifact whose digest was pinned. The caller owns removing the tree.
    """
    tree = _empty_tree(_require_owner_root(owner_root), _require_launch(launch))
    try:
        identity = _populate(tree, launch, snapshot_path, snapshot_descriptor)
        verify_sealed_entry(tree, identity, expected_sha256, checkpoint)
    except BaseException:
        remove_launch_tree(tree)
        raise
    return tree


def _empty_tree(owner_root: Path, launch: PackageLaunch) -> LaunchTree:
    """The directories, made owner-private, and the two paths that will fill them."""
    root = owner_root / f"{LAUNCH_PREFIX}{secrets.token_hex(16)}"
    entry_relative = launch.entry_relative
    directories = _created_directories(root, entry_relative)
    entry = root / entry_relative
    manifest = root / MANIFEST_NAME
    return LaunchTree(root, entry, (entry, manifest), directories)


def _populate(
    tree: LaunchTree,
    launch: PackageLaunch,
    snapshot_path: Path,
    snapshot_descriptor: int,
) -> tuple[int, int]:
    """Fill the tree and seal it, returning the inode the verified copy moved to."""
    _write_sealed_file(tree.files[1], _manifest_bytes(launch))
    identity = _place_entry(tree.entry, snapshot_path, snapshot_descriptor)
    _seal(tree)
    return identity


def _suppressed(action: Callable[[], object]) -> BaseException | None:
    try:
        action()
    except OSError as error:
        return error
    return None


def _unlinked(path: Path) -> BaseException | None:
    return _suppressed(lambda: os.unlink(path))


def _removed_directory(path: Path) -> BaseException | None:
    return _suppressed(lambda: os.rmdir(path))


def _teardown_errors(tree: LaunchTree) -> list[BaseException]:
    """Every removal is attempted; the failures are collected, not raised."""
    errors = list(map(_unlinked, tree.files))
    errors.extend(map(_removed_directory, tree.directories))
    return [error for error in errors if error is not None]


def remove_launch_tree(tree: object) -> BaseException | None:
    """Take the tree back down, first error reported and the rest still tried."""
    if not isinstance(tree, LaunchTree):
        return None
    _unseal(tree)
    named = _teardown_errors(tree)
    return _first_error(named)


def _first_error(errors: list[BaseException]) -> BaseException | None:
    if not errors:
        return None
    return errors[0]
