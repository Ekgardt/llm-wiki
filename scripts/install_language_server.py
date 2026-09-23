"""Install one profile's pinned language server. A separate, explicit action.

`knowledge/notes/read-only-lsp-navigation-engine-decision.md` makes installation
an operator action, never something a navigation query triggers. This module is
that action for any profile in `lsp_profiles.REGISTRY` other than Pyright, which
keeps its own installer (`scripts/install_pyright.py`) with its own
descriptor-relative containment and atomic no-replace publication.

It is smaller than that one and says where. What it does not compromise on is
what the bytes are: the archive must match the profile's pinned Subresource
Integrity hash before a single member is read, every member is bounded and
contained, and nothing but regular files and directories is written. Since audit
3 (finding B21) it also shares that installer's network posture -- the opener in
`scripts/pinned_download.py`, which carries no proxy table and refuses redirects
-- and has its own install lock, one absolute deadline, `fsync`, validation of an
install that is already there, and a sweep of scratch an abrupt death left
behind. Research:
`docs/research/2026-09-17-inst-the-second-installer-gets-the-first-ones-guarantees.md`.

Two artifacts, not one. `typescript-language-server` resolves its TypeScript at
runtime and a managed install carries no `node_modules`, so the profile names a
second pinned tarball on its `RuntimeOption` and this installer unpacks it as a
sibling. See `docs/research/2026-08-28-precise-navigation-beyond-python.md`,
Finding 3.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import platform
import secrets
import shutil
import sys
import tarfile
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath

from bounded_io import IO_CHUNK_BYTES, read_stable_bytes
from go_source_build import SourceBuildError
from lsp_identity import (
    INSTALL_MANIFEST_NAME,
    ManifestError,
    build_install_manifest,
    validate_install_manifest,
)
from lsp_profiles import REGISTRY
from lsp_server_profile import LanguageServerProfile, RuntimeOption, ServerComponent
from operational_ownership import (
    OperationalOwnershipError,
    ProcessIdentity,
    current_process_identity,
    process_identity_state,
)
from pinned_download import (
    MAX_COMPRESSED_BYTES,
    MAX_DECOMPRESSED_BYTES,
    MAX_MEMBER_BYTES,
    MAX_MEMBERS,
    MAX_PATH_COMPONENTS,
    PinnedDownloadError,
    download_pinned,
)
from reliable_memory import canonical_json_bytes

# One budget for the whole install, not one per socket. The Rust profile pulls
# about 160 MB across five archives and unpacks past a gigabyte; the Go profile
# downloads 70 MB and then compiles gopls.
DEFAULT_INSTALL_TIMEOUT_SECONDS = 1800.0
LOCK_POLL_SECONDS = 0.05
MAX_LOCK_BYTES = 1024
# A managed server's install receipt; doctor's archive manifests allow 256 KiB, a generation's 1 MiB.
MAX_MANIFEST_BYTES = 16 * 1024
COPY_CHUNK_BYTES = IO_CHUNK_BYTES

# One unpacked file, for a digest: `rust-analyzer` is ~90 MB and
# `librustc_driver` is larger, so this is the per-profile member ceiling, not
# the module-wide one.
MAX_INSTALLED_FILE_BYTES = 512 * 1024 * 1024

# Every npm tarball roots its payload at `package/`. The server profile's
# `server_relative` includes that component and is extracted as-is; the runtime
# artifact is re-rooted under the directory its `sibling_relative` names.
NPM_ROOT = "package"

# Scratch and the lock share this prefix so one sweep finds both; the lock is
# excluded from the sweep by its exact name.
SCRATCH_PREFIX = ".install-"


class InstallError(RuntimeError):
    """The install could not be completed as pinned."""


def _integrity_digest(integrity: str) -> bytes:
    return base64.b64decode(integrity.split("-", 1)[1], validate=True)


def _integrity_algorithm(integrity: str) -> str:
    algorithm = integrity.split("-", 1)[0]
    if algorithm not in ("sha256", "sha512"):
        raise InstallError(f"unsupported integrity algorithm: {algorithm}")
    return algorithm


def _file_digest(path: Path, algorithm: str, limit: int, label: str) -> bytes:
    """Hash one archive without holding it in memory, refusing an oversized one."""
    digest = hashlib.new(algorithm)
    total = 0
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(COPY_CHUNK_BYTES), b""):
            total += len(chunk)
            _require_within_bound(total, limit, label)
            digest.update(chunk)
    return digest.digest()


def _require_within_bound(total: int, limit: int, label: str) -> None:
    if total > limit:
        raise InstallError(f"{label} exceeds the compressed bound")


def _require_integrity(path: Path, integrity: str, label: str, limit: int) -> None:
    algorithm = _integrity_algorithm(integrity)
    if _file_digest(path, algorithm, limit, label) != _integrity_digest(integrity):
        raise InstallError(f"{label} does not match its pinned integrity hash")


def _fsync_file(handle: object) -> None:
    handle.flush()
    os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    """POSIX only: Windows cannot `os.open` a directory.

    There the no-replace rename is the durability point, which is the same
    position `install_pyright` takes for its own publication.
    """
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_tree(root: Path) -> None:
    """Every directory under a staged tree, so its entries survive a crash."""
    _fsync_directory(root)
    for parent, directories, _files in os.walk(root):
        for name in directories:
            _fsync_directory(Path(parent) / name)


def _downloaded(url: str, target: Path, deadline: float, limit: int) -> None:
    with open(target, "wb") as handle:
        _stream_pinned(url, handle, deadline, limit)
        _fsync_file(handle)


def _stream_pinned(url: str, handle: object, deadline: float, limit: int) -> None:
    try:
        download_pinned(url, handle.write, deadline=deadline, limit=limit)
    except PinnedDownloadError as error:
        raise InstallError(f"{url}: {error}") from error


def _artifact_file(
    url: str,
    integrity: str,
    local: Path | None,
    label: str,
    limit: int,
    scratch: Path,
    deadline: float,
) -> Path:
    """The pinned archive on disk: the operator's copy, or one streamed down."""
    _check_deadline(deadline)
    source = local if local is not None else scratch / f"archive-{secrets.token_hex(8)}"
    if local is None:
        _downloaded(url, source, deadline, limit)
    _require_integrity(source, integrity, label, limit)
    return source


def _check_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise TimeoutError("language server installation deadline expired")


def _install_deadline(deadline: float | None) -> float:
    if deadline is None:
        return time.monotonic() + DEFAULT_INSTALL_TIMEOUT_SECONDS
    return deadline


def _bounds(profile: LanguageServerProfile) -> tuple[int, int]:
    """This profile's archive bounds.

    A Go toolchain is ~67 MB compressed and ~250 MB unpacked, far past the npm
    bounds; the ceilings are per profile so the npm pins keep theirs.
    """
    compressed = profile.max_compressed_bytes or MAX_COMPRESSED_BYTES
    decompressed = profile.max_decompressed_bytes or MAX_DECOMPRESSED_BYTES
    return (compressed, decompressed)


def _anchored_or_climbing(path: PurePath) -> bool:
    return bool(path.anchor) or ".." in path.parts


def _member_path_escapes(name: str) -> bool:
    """Whether the entry's own name leaves the archive, by either system's rules.

    The host's `Path` gave a different answer on each system: `/absolute` has
    no drive, so Windows did not call it absolute and unpacked it, while
    `C:/x` and `go\\..\\..\\x` are plain names to POSIX. Research:
    `docs/research/2026-09-17-five-failures-only-the-other-systems-showed.md`.
    """
    return _anchored_or_climbing(PurePosixPath(name)) or _anchored_or_climbing(
        PureWindowsPath(name)
    )


def _relative_member_path(name: str) -> Path:
    """One archive entry's path, tar or zip: bounded, relative, with no `..`."""
    parts = PurePosixPath(name).parts
    if not parts or len(parts) > MAX_PATH_COMPONENTS:
        raise InstallError(f"member path is unusable: {name!r}")
    if _member_path_escapes(name):
        raise InstallError(f"member path escapes the archive: {name!r}")
    return Path(*parts)


def _member_relative(member: tarfile.TarInfo) -> Path:
    return _relative_member_path(member.name)


def _require_plain_member(member: tarfile.TarInfo, limit: int = MAX_MEMBER_BYTES) -> None:
    """One member, bounded. A language server binary can be the whole archive.

    The npm pins are small JavaScript files; `rust-analyzer` is a 90 MB
    executable and `librustc_driver` is larger still, so the per-member bound
    is a profile's own, like the compressed and decompressed ones.
    """
    if not (member.isfile() or member.isdir()):
        raise InstallError(f"member is not a regular file or directory: {member.name!r}")
    if member.size > limit:
        raise InstallError(f"member exceeds its bound: {member.name!r}")


@dataclass(frozen=True, slots=True)
class _Placement:
    """Where an archive's members land inside the staging directory.

    `subdirectory` is the npm shape: re-root `package/` under a sibling
    directory. `strip` and `prefix` are the rust-installer shape: drop the
    archive root and the component name, and write the rest under one prefix.
    See `docs/research/2026-09-12-installing-rust-for-precise-navigation.md`.
    """

    subdirectory: str | None = None
    strip: int = 0
    prefix: Path | None = None

    def target(self, relative: Path) -> Path | None:
        """The member's place, or None when it is outside the payload."""
        if self.subdirectory is not None:
            return _npm_rerooted(relative, self.subdirectory)
        parts = relative.parts[self.strip :]
        if not parts:
            return None
        return Path(*parts) if self.prefix is None else self.prefix / Path(*parts)


def _npm_rerooted(relative: Path, subdirectory: str) -> Path | None:
    if relative.parts[0] != NPM_ROOT:
        return None
    return Path(subdirectory, *relative.parts[1:])


def _extract_file(archive: tarfile.TarFile, member: tarfile.TarInfo, target: Path) -> int:
    source = archive.extractfile(member)
    if source is None:
        raise InstallError(f"member could not be read: {member.name!r}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with source, open(target, "wb") as handle:
        shutil.copyfileobj(source, handle, length=COPY_CHUNK_BYTES)
        _fsync_file(handle)
    _apply_execute_bit(member, target)
    return member.size


def _apply_execute_bit(member: tarfile.TarInfo, target: Path) -> None:
    """A compiler in a tarball is only a compiler if it stays executable.

    The npm pins are JavaScript files run by Node, so this never mattered
    before; `go` and `gopls` are executables.
    """
    if not member.mode & 0o111:
        return
    target.chmod(target.stat().st_mode | 0o755)


def _extract_member(
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
    root: Path,
    placement: _Placement,
    member_limit: int = MAX_MEMBER_BYTES,
) -> int:
    _require_plain_member(member, member_limit)
    destination = placement.target(_member_relative(member))
    if destination is None:
        return 0
    target = root / destination
    if member.isdir():
        target.mkdir(parents=True, exist_ok=True)
        return 0
    return _extract_file(archive, member, target)


ZIP_MODE = "zip"
_ZIP_KIND_MASK = 0o170000
# 0: the archive was made where there are no Unix modes, which is how Go's own
# Windows zip is written; then `ZipInfo.is_dir()` tells the two apart.
_ZIP_PLAIN_KINDS = frozenset({0, 0o100000, 0o040000})


def _archive_mode(url: str) -> str:
    """Zip, xz or gzip, by the pinned URL's own suffix.

    npm ships gzip, Rust xz, and Go's Windows toolchain is a zip, which used to
    be opened as a tarball and always failed (audit 3, B18). Research:
    `docs/research/2026-09-17-a-pinned-zip-is-unpacked-as-a-zip.md`.
    """
    if url.endswith(".zip"):
        return ZIP_MODE
    return "r:xz" if url.endswith(".xz") else "r:gz"


def _zip_unix_mode(info: zipfile.ZipInfo) -> int:
    return info.external_attr >> 16


def _require_plain_zip_entry(info: zipfile.ZipInfo, limit: int) -> None:
    """The zip twin of `_require_plain_member`: a file or a directory, bounded."""
    if _zip_unix_mode(info) & _ZIP_KIND_MASK not in _ZIP_PLAIN_KINDS:
        raise InstallError(f"member is not a regular file or directory: {info.filename!r}")
    if info.file_size > limit:
        raise InstallError(f"member exceeds its bound: {info.filename!r}")


def _copy_declared_bytes(source, handle, declared: int, name: str) -> None:
    """Copy one entry and refuse it the moment it outgrows what it declared."""
    copied = 0
    while True:
        chunk = source.read(COPY_CHUNK_BYTES)
        if not chunk:
            return
        copied += len(chunk)
        if copied > declared:
            raise InstallError(f"member is larger than it declares: {name!r}")
        handle.write(chunk)


def _write_zip_entry(archive: zipfile.ZipFile, info: zipfile.ZipInfo, target: Path) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    with archive.open(info) as source, open(target, "wb") as handle:
        _copy_declared_bytes(source, handle, info.file_size, info.filename)
        _fsync_file(handle)
    if _zip_unix_mode(info) & 0o111:
        target.chmod(target.stat().st_mode | 0o755)
    return info.file_size


def _extract_zip_entry(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    root: Path,
    placement: _Placement,
    member_limit: int,
) -> int:
    _require_plain_zip_entry(info, member_limit)
    destination = placement.target(_relative_member_path(info.filename))
    if destination is None:
        return 0
    target = root / destination
    if info.is_dir():
        target.mkdir(parents=True, exist_ok=True)
        return 0
    return _write_zip_entry(archive, info, target)


def _unpack_zip(scratch, root: Path, placement: _Placement, limits: tuple[int, int, int]) -> None:
    total_limit, members_limit, member_limit = limits
    written = 0
    with zipfile.ZipFile(scratch) as archive:
        entries = archive.infolist()
        if len(entries) > members_limit:
            raise InstallError("archive exceeds the member bound")
        for info in entries:
            written += _extract_zip_entry(archive, info, root, placement, member_limit)
            _require_total(written, total_limit)


def _unpack_tar(
    scratch, mode: str, root: Path, placement: _Placement, limits: tuple[int, int, int]
) -> None:
    total_limit, members_limit, member_limit = limits
    written = 0
    with tarfile.open(fileobj=scratch, mode=mode) as archive:
        for member in _bounded_members(archive, members_limit):
            written += _extract_member(archive, member, root, placement, member_limit)
            _require_total(written, total_limit)


def _extract(
    source,
    root: Path,
    placement: _Placement,
    limit: int = MAX_DECOMPRESSED_BYTES,
    members_limit: int = MAX_MEMBERS,
    mode: str = "r:gz",
    member_limit: int = MAX_MEMBER_BYTES,
) -> None:
    """Unpack a bounded, contained archive under `root` from an open, seekable file.

    The archive is read from a handle the caller owns. It used to be held whole
    in memory and copied into a second temporary file; for the Rust pins that
    was 160 MB of process memory plus a copy (audit 3, B21).
    """
    limits = (limit, members_limit, member_limit)
    if mode == ZIP_MODE:
        _unpack_zip(source, root, placement, limits)
        return
    _unpack_tar(source, mode, root, placement, limits)


def _extract_path(
    source: Path,
    root: Path,
    placement: _Placement,
    limit: int = MAX_DECOMPRESSED_BYTES,
    members_limit: int = MAX_MEMBERS,
    mode: str = "r:gz",
    member_limit: int = MAX_MEMBER_BYTES,
) -> None:
    with open(source, "rb") as handle:
        _extract(handle, root, placement, limit, members_limit, mode, member_limit)


def _bounded_members(
    archive: tarfile.TarFile, limit: int = MAX_MEMBERS
) -> list[tarfile.TarInfo]:
    members = archive.getmembers()
    if len(members) > limit:
        raise InstallError("archive exceeds the member bound")
    return members


def _require_total(written: int, limit: int = MAX_DECOMPRESSED_BYTES) -> None:
    if written > limit:
        raise InstallError("archive exceeds the decompressed bound")


def _sha256_file(path: Path) -> str:
    return _file_digest(path, "sha256", MAX_INSTALLED_FILE_BYTES, str(path)).hex()


def _install_runtime(
    runtime: RuntimeOption | None,
    staging: Path,
    local: Path | None,
    scratch: Path,
    deadline: float,
) -> str:
    """Unpack the pinned engine beside the server; its digest, or empty."""
    if runtime is None:
        return ""
    if runtime.package_url is None or runtime.package_integrity is None:
        raise InstallError("profile names a runtime path with no pinned artifact")
    source = _artifact_file(
        runtime.package_url,
        runtime.package_integrity,
        local,
        "runtime artifact",
        MAX_COMPRESSED_BYTES,
        scratch,
        deadline,
    )
    _extract_path(
        source, staging, _Placement(subdirectory=runtime.install_subdirectory)
    )
    return _sha256_file(staging / runtime.sibling_relative)


def _lock_path(parent: Path, profile: LanguageServerProfile) -> Path:
    return parent / f"{SCRATCH_PREFIX}{profile.name}-lock"


def _lock_record_bytes(nonce: str) -> bytes:
    identity = current_process_identity()
    return canonical_json_bytes(
        {
            "acquired_at_unix_ns": time.time_ns(),
            "nonce": nonce,
            "pid": identity.pid,
            "process_start": identity.start_identity,
        }
    )


@dataclass(frozen=True, slots=True)
class _InstallLock:
    path: Path
    nonce: str


def _claimed_lock(path: Path, nonce: str) -> bool:
    """Binary: the lock record written is the record `_lock_record` reads back."""
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(_lock_record_bytes(nonce))
        _fsync_file(handle)
    _fsync_directory(path.parent)
    return True


def _lock_record(path: Path) -> dict | None:
    try:
        raw = read_stable_bytes(path, MAX_LOCK_BYTES, label="install lock")
    except (OSError, ValueError):
        return None
    return _parsed_lock_record(raw)


def _parsed_lock_record(raw: bytes) -> dict | None:
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    return value


def _owner_is_gone(record: dict) -> bool:
    """True only when the OS says that process is provably not running."""
    pid, start = record.get("pid"), record.get("process_start")
    if not isinstance(pid, int) or not isinstance(start, str):
        return True
    return _owner_state(pid, start) == "dead"


def _owner_state(pid: int, start: str) -> str:
    try:
        return process_identity_state(ProcessIdentity(pid=pid, start_identity=start))
    except OperationalOwnershipError:
        return "unknown"


def _release_dead_owner(path: Path) -> None:
    record = _lock_record(path)
    if record is None or not _owner_is_gone(record):
        return
    _unlink_matching_lock(path, record.get("nonce"))


def _unlink_matching_lock(path: Path, nonce: object) -> None:
    """Remove the lock only if it is still the one that was judged abandoned."""
    current = _lock_record(path)
    if current is None or current.get("nonce") != nonce:
        return
    path.unlink(missing_ok=True)


def _wait_for_lock(path: Path, deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise InstallError(f"another installer holds {path}")
    time.sleep(LOCK_POLL_SECONDS)


def _acquire_lock(path: Path, deadline: float) -> _InstallLock:
    """One installer per profile; an abandoned lock is reclaimed by name."""
    nonce = secrets.token_hex(16)
    while not _claimed_lock(path, nonce):
        _release_dead_owner(path)
        _wait_for_lock(path, deadline)
    return _InstallLock(path, nonce)


def _release_lock(lock: _InstallLock) -> None:
    _unlink_matching_lock(lock.path, lock.nonce)


def _sweep_abandoned_scratch(parent: Path, lock_name: str) -> None:
    """The lock is held, so every other `.install-*` entry here is abandoned.

    Staging used to survive a `SIGKILL`, a power cut or an `OOM` kill, and for
    the Rust profile that is about 3 GB of disposable cache (audit 3, B21).
    """
    with os.scandir(parent) as entries:
        names = [entry.name for entry in entries]
    for name in names:
        _remove_abandoned(parent, name, lock_name)


def _remove_abandoned(parent: Path, name: str, lock_name: str) -> None:
    if name == lock_name or not name.startswith(SCRATCH_PREFIX):
        return
    target = parent / name
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target)
        return
    target.unlink()


def _read_receipt(root: Path) -> object:
    try:
        raw = read_stable_bytes(
            root / INSTALL_MANIFEST_NAME, MAX_MANIFEST_BYTES, label="install manifest"
        )
        return json.loads(raw.decode("utf-8", errors="strict"))
    except (OSError, UnicodeError, ValueError) as error:
        raise InstallError(f"{root} carries no readable install receipt") from error


def _require_recorded_digest(path: Path, expected: str, label: str) -> None:
    if not path.is_file() or path.is_symlink():
        raise InstallError(f"the installed {label} is missing: {path}")
    if _sha256_file(path) != expected:
        raise InstallError(f"the installed {label} is not the one the receipt names")


def _require_recorded_runtime(
    profile: LanguageServerProfile, root: Path, receipt: dict
) -> None:
    runtime = profile.runtime_option
    if runtime is None:
        return
    _require_recorded_digest(
        root / runtime.sibling_relative, receipt["runtime_sha256"], "runtime"
    )


def _validated_existing_install(profile: LanguageServerProfile, root: Path) -> Path:
    """An install already at the managed root, re-derived rather than trusted.

    The root used to be refused outright, so a re-run after an interrupted
    install was an error instead of a no-op (audit 3, B21).
    """
    try:
        receipt = validate_install_manifest(profile, _read_receipt(root))
    except ManifestError as error:
        raise InstallError(f"{root} holds an install this build cannot use") from error
    _require_recorded_digest(
        root / profile.server_relative, receipt["server_sha256"], "server"
    )
    _require_recorded_runtime(profile, root, receipt)
    return root


def _staged(
    profile: LanguageServerProfile,
    staging: Path,
    scratch: Path,
    artifacts: _Artifacts,
    deadline: float,
) -> dict[str, str]:
    compressed, decompressed = _bounds(profile)
    source = _artifact_file(
        profile.package_url,
        profile.package_integrity,
        artifacts.server,
        "server artifact",
        compressed,
        scratch,
        deadline,
    )
    _extract_path(
        source,
        staging,
        _profile_placement(profile),
        decompressed,
        profile.max_members or MAX_MEMBERS,
        _archive_mode(profile.package_url),
        profile.max_member_bytes or MAX_MEMBER_BYTES,
    )
    _install_components(profile, staging, scratch, artifacts, deadline)
    _build_if_required(profile, staging)
    server_sha256 = _sha256_file(staging / profile.server_relative)
    runtime_sha256 = _install_runtime(
        profile.runtime_option, staging, artifacts.runtime, scratch, deadline
    )
    return build_install_manifest(
        profile, server_sha256=server_sha256, runtime_sha256=runtime_sha256
    )


def _profile_placement(profile: LanguageServerProfile) -> _Placement:
    return _Placement(strip=profile.strip_components, prefix=profile.install_prefix)


def _install_components(
    profile: LanguageServerProfile,
    staging: Path,
    scratch: Path,
    artifacts: _Artifacts,
    deadline: float,
) -> None:
    """Unpack every further archive this profile declares, in order."""
    compressed, decompressed = _bounds(profile)
    for component in profile.components:
        _install_component(
            component, staging, (compressed, decompressed), profile, scratch, artifacts, deadline
        )


def _install_component(
    component: ServerComponent,
    staging: Path,
    bounds: tuple[int, int],
    profile: LanguageServerProfile,
    scratch: Path,
    artifacts: _Artifacts,
    deadline: float,
) -> None:
    artifact = component.artifact_for_platform(platform.system(), platform.machine())
    if artifact is None:
        raise InstallError(f"{component.name} has no pinned archive for this platform")
    source = _artifact_file(
        artifact.url,
        artifact.integrity,
        artifacts.components.get(component.name),
        f"{component.name} artifact",
        bounds[0],
        scratch,
        deadline,
    )
    _extract_path(
        source,
        staging,
        _Placement(strip=component.strip_components, prefix=component.prefix),
        bounds[1],
        profile.max_members or MAX_MEMBERS,
        _archive_mode(artifact.url),
        profile.max_member_bytes or MAX_MEMBER_BYTES,
    )


def _build_if_required(profile: LanguageServerProfile, staging: Path) -> None:
    """Compile the server when the archive was a compiler rather than a server.

    The digest of what the build produced is recorded by the caller and is what
    gates every launch, exactly as an unpacked entry file's digest is. Research:
    `docs/research/2026-09-12-installing-go-and-building-gopls.md`.
    """
    build = profile.source_build
    if build is None:
        return
    from go_source_build import build_source_server

    build_source_server(staging, build)


def _require_pinned_platform(profile: LanguageServerProfile) -> None:
    """Refuse, before any download, a platform this profile pins nothing for.

    The registry falls back to the 64-bit Linux archive so that it stays
    importable everywhere; installing that archive on another platform used to
    download and verify it and fail only at the build or at first launch.
    Research: `docs/research/2026-09-17-inst-a-platform-without-a-pin-is-refused-by-name.md`.
    """
    if not profile.platform_artifacts:
        return
    system, machine = platform.system(), platform.machine()
    if profile.artifact_for_platform(system, machine) is None:
        raise InstallError(
            f"{profile.name} has no pinned artifact for this platform: {system} {machine}"
        )


@dataclass(frozen=True, slots=True)
class _Artifacts:
    """Archives the operator supplied instead of the network, if any."""

    server: Path | None
    runtime: Path | None
    components: dict[str, Path]


def _new_scratch(parent: Path, profile: LanguageServerProfile) -> Path:
    return Path(
        tempfile.mkdtemp(prefix=f"{SCRATCH_PREFIX}{profile.name}-", dir=parent)
    )


def _published(
    profile: LanguageServerProfile,
    root: Path,
    staging: Path,
    scratch: Path,
    artifacts: _Artifacts,
    deadline: float,
) -> Path:
    manifest = _staged(profile, staging, scratch, artifacts, deadline)
    (staging / INSTALL_MANIFEST_NAME).write_bytes(canonical_json_bytes(manifest))
    _fsync_tree(staging)
    staging.rename(root)
    _fsync_directory(root.parent)
    return root


def _install_holding_lock(
    profile: LanguageServerProfile,
    root: Path,
    lock_name: str,
    artifacts: _Artifacts,
    deadline: float,
) -> Path:
    if root.exists():
        return _validated_existing_install(profile, root)
    _sweep_abandoned_scratch(root.parent, lock_name)
    staging = _new_scratch(root.parent, profile)
    scratch = _new_scratch(root.parent, profile)
    try:
        return _published(profile, root, staging, scratch, artifacts, deadline)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def install_language_server(
    profile: LanguageServerProfile,
    *,
    state_root: Path,
    server_artifact: Path | None = None,
    runtime_artifact: Path | None = None,
    component_artifacts: dict[str, Path] | None = None,
    deadline: float | None = None,
) -> Path:
    """Install the pinned artifacts and write the receipt; return the managed root."""
    _require_pinned_platform(profile)
    deadline = _install_deadline(deadline)
    root = profile.managed_root(state_root)
    root.parent.mkdir(parents=True, exist_ok=True)
    lock = _acquire_lock(_lock_path(root.parent, profile), deadline)
    artifacts = _Artifacts(
        server_artifact, runtime_artifact, dict(component_artifacts or {})
    )
    try:
        return _install_holding_lock(
            profile, root, lock.path.name, artifacts, deadline
        )
    finally:
        _release_lock(lock)


def _component_artifact(value: str) -> tuple[str, Path]:
    name, separator, path = value.partition("=")
    if not separator or not name or not path:
        raise argparse.ArgumentTypeError("expected NAME=PATH")
    return (name, Path(path))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", required=True, type=Path)
    parser.add_argument("--profile", required=True, choices=REGISTRY.names())
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--runtime-artifact", type=Path)
    parser.add_argument(
        "--component-artifact",
        type=_component_artifact,
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="an already downloaded component archive; repeat per component",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_INSTALL_TIMEOUT_SECONDS,
        help="seconds for the whole install, downloads and build included",
    )
    return parser.parse_args(argv)


# What an install can fail with and still end as `install failed: ...`, exit 1.
_INSTALL_FAILURES = (
    InstallError,
    SourceBuildError,
    OSError,
    tarfile.TarError,
    zipfile.BadZipFile,
    ValueError,
)


def main(argv: list[str] | None = None) -> int:
    arguments = parse_args(argv)
    profile = REGISTRY.get(arguments.profile)
    if profile.name == "pyright":
        print("use scripts/install_pyright.py for the Pyright profile", file=sys.stderr)
        return 2
    try:
        root = install_language_server(
            profile,
            state_root=arguments.state_root,
            server_artifact=arguments.artifact,
            runtime_artifact=arguments.runtime_artifact,
            component_artifacts=dict(arguments.component_artifact),
            deadline=time.monotonic() + arguments.timeout,
        )
    except _INSTALL_FAILURES as error:
        print(f"install failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"profile": profile.name, "root": str(root)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
