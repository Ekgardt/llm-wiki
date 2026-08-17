"""Shared durability and validation primitives for reliable memory operations."""

from __future__ import annotations

import contextlib
import ctypes
import errno
import hashlib
import json
import math
import os
import platform
import re
import secrets
import sqlite3
import stat
import subprocess
import time
import unicodedata
import warnings
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Literal


@dataclass(frozen=True)
class ReliableMemoryDefaults:
    markdown_busy_ms: int = 10_000
    queue_busy_ms: int = 5_000
    transaction_retention_days: int = 30
    artifact_retention_days: int = 30
    archive_hot_days: int = 90
    project_lease_seconds: int = 30
    project_heartbeat_seconds: int = 10
    checkpoint_debounce_seconds: int = 30
    checkpoint_fallback_events: int = 20
    queue_lease_seconds: int = 120
    queue_heartbeat_seconds: int = 40
    queue_max_attempts: int = 8
    retry_base_seconds: int = 30
    retry_cap_seconds: int = 3_600
    worker_max_tasks: int = 20
    worker_max_seconds: int = 600
    worker_idle_seconds: int = 2
    priority_min: int = -100
    priority_max: int = 100
    queue_result_retention_days: int = 30
    dead_task_retention_days: int | None = None


DEFAULTS = ReliableMemoryDefaults()


class UnsafeStateRoot(ValueError):
    """Raised when runtime state cannot safely use local SQLite locking."""


class SchemaValidationError(ValueError):
    """Raised when an instance does not satisfy a committed schema."""


class MetadataDurabilityUnavailable(OSError):
    """Raised when the platform cannot prove the requested metadata boundary."""

    code = "metadata_durability_unavailable"


class OperationalDatabaseContractError(sqlite3.DatabaseError):
    """Raised when an operational database or migration violates its contract."""

    code = "operational_database_contract_mismatch"


@dataclass(frozen=True)
class OperationalDatabaseContract:
    application_id: int
    user_version: int = 3

    def __post_init__(self) -> None:
        for name, value in (
            ("application_id", self.application_id),
            ("user_version", self.user_version),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True)
class MigrationStatement:
    name: str
    sql: str
    completed: Callable[[sqlite3.Connection], bool] = field(compare=False, repr=False)
    parameters: Sequence[object] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("migration statement name must be non-empty")
        if not isinstance(self.sql, str) or not self.sql.strip():
            raise ValueError("migration statement SQL must be non-empty")
        if not callable(self.completed):
            raise TypeError("migration statement completed invariant must be callable")


@dataclass(frozen=True)
class RuntimeFileIdentity:
    platform: str
    volume: str
    file_id: str
    size: int
    mtime_ns: int


def _canonical_value(value: object) -> object:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, float):
        raise TypeError("canonical JSON does not permit float values")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, list):
        return [_canonical_value(item) for item in value]
    if isinstance(value, dict):
        normalized: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("canonical JSON object keys must be strings")
            normalized_key = unicodedata.normalize("NFC", key)
            if normalized_key in normalized:
                raise ValueError(f"normalized object-key collision: {normalized_key!r}")
            normalized[normalized_key] = _canonical_value(item)
        return normalized
    raise TypeError(f"canonical JSON does not permit {type(value).__name__} values")


def canonical_json_bytes(value: object) -> bytes:
    """Encode the restricted JSON domain deterministically as UTF-8."""
    normalized = _canonical_value(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _known_network_path(path: Path) -> bool:
    raw = str(path)
    if raw.startswith(("\\\\", "//")):
        return True
    if _platform_system() == "Windows":
        anchor = path.resolve(strict=False).anchor
        if not anchor:
            return False
        drive_type_remote = 4
        return ctypes.windll.kernel32.GetDriveTypeW(anchor) == drive_type_remote
    system = _platform_system()
    mount_data, is_mountinfo = _read_posix_mount_data()
    mounts = _parse_posix_mounts(mount_data, is_mountinfo=is_mountinfo)
    if system == "Darwin" and not mounts:
        mounts = _parse_darwin_mounts(_query_darwin_mounts())
    target = str(path.resolve(strict=False)).replace("\\", "/")
    matching = [entry for entry in mounts if _path_is_under(target, entry[0])]
    if not matching:
        return False
    _mount_point, filesystem = max(matching, key=lambda entry: len(entry[0]))
    return _is_network_filesystem(filesystem)


def _platform_system() -> str:
    return platform.system()


def _read_posix_mount_data() -> tuple[str, bool]:
    for path, is_mountinfo in (
        (Path("/proc/self/mountinfo"), True),
        (Path("/proc/mounts"), False),
    ):
        try:
            return path.read_text(encoding="utf-8"), is_mountinfo
        except OSError:
            continue
    return "", True


def _query_darwin_mounts() -> str:
    try:
        result = subprocess.run(
            ["/sbin/mount"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout[:1_048_576]


def _parse_posix_mounts(data: str, *, is_mountinfo: bool) -> list[tuple[str, str]]:
    mounts: list[tuple[str, str]] = []
    for line in data.splitlines():
        try:
            if is_mountinfo:
                left, right = line.split(" - ", 1)
                mount_point = left.split()[4]
                filesystem = right.split()[0]
            else:
                _source, mount_point, filesystem, *_rest = line.split()
        except (IndexError, ValueError):
            continue
        mounts.append((_decode_mount_path(mount_point), filesystem.casefold()))
    return mounts


def _parse_darwin_mounts(data: str) -> list[tuple[str, str]]:
    mounts: list[tuple[str, str]] = []
    for line in data.splitlines():
        match = re.match(r"^.* on (.+) \(([^,()]+)(?:,|\))", line)
        if match is None:
            continue
        mount_point, filesystem = match.groups()
        mounts.append((_decode_mount_path(mount_point), filesystem.casefold()))
    return mounts


def _is_network_filesystem(filesystem: str) -> bool:
    return filesystem in {
        "9p",
        "afpfs",
        "ceph",
        "cifs",
        "nfs",
        "nfs4",
        "smbfs",
        "sshfs",
        "webdav",
    } or filesystem.endswith(".sshfs")


def _decode_mount_path(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)


def _path_is_under(path: str, mount_point: str) -> bool:
    mount_point = mount_point.rstrip("/") or "/"
    return mount_point == "/" or path == mount_point or path.startswith(f"{mount_point}/")


def _windows_reparse_point(path: Path) -> bool:
    if os.name != "nt":
        return False
    current = path.absolute()
    candidates = [current, *current.parents]
    for candidate in candidates:
        if not candidate.exists():
            continue
        attributes = getattr(candidate.stat(follow_symlinks=False), "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if attributes & reparse_flag:
            return True
    return False


def _sqlite_lock_probe(root: Path, *, deadline: float = float("inf")) -> bool | None:
    """Return lock support, or ``None`` when a bounded probe cannot complete."""
    probe = root / f".llm-wiki-lock-probe-{secrets.token_hex(16)}.sqlite3"
    first: sqlite3.Connection | None = None
    second: sqlite3.Connection | None = None
    try:
        if time.monotonic() >= deadline:
            return None
        first = sqlite3.connect(probe, timeout=0)
        if time.monotonic() >= deadline:
            return None
        second = sqlite3.connect(probe, timeout=0)
        first.execute("PRAGMA journal_mode=DELETE")
        first.execute("BEGIN IMMEDIATE")
        if time.monotonic() >= deadline:
            return None
        try:
            second.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            return "locked" in str(exc).lower() or "busy" in str(exc).lower()
        else:
            second.rollback()
            return False
    except (OSError, sqlite3.Error):
        return None
    finally:
        if first is not None:
            with contextlib.suppress(sqlite3.Error):
                first.rollback()
            with contextlib.suppress(sqlite3.Error):
                first.close()
        if second is not None:
            with contextlib.suppress(sqlite3.Error):
                second.close()
        for suffix in ("", "-journal", "-shm", "-wal"):
            with contextlib.suppress(OSError):
                Path(f"{probe}{suffix}").unlink()


def validate_state_root(path: Path) -> None:
    """Fail closed when a runtime root lacks known-safe local lock semantics."""
    path = Path(path)
    if _windows_reparse_point(path):
        raise UnsafeStateRoot(f"state root must not traverse a Windows reparse point: {path}")
    if _known_network_path(path):
        raise UnsafeStateRoot(f"state root must use a local filesystem: {path}")
    cloud_names = {"dropbox", "googledrive", "google drive", "iclouddrive", "onedrive"}
    if any(part.casefold() in cloud_names for part in path.parts):
        warnings.warn(
            f"state root appears to be cloud-synchronized; local locking is only probed: {path}",
            RuntimeWarning,
            stacklevel=2,
        )
    path.mkdir(parents=True, exist_ok=True)
    _set_owner_only(path, 0o700)
    if _sqlite_lock_probe(path) is not True:
        raise UnsafeStateRoot(f"state root failed the SQLite two-connection locking probe: {path}")


def _owner_permissions_supported(path: Path) -> bool:
    return _platform_system() != "Windows" and os.name == "posix"


def _set_owner_only(path: Path, mode: int) -> bool:
    if not _owner_permissions_supported(path):
        return False
    try:
        path.chmod(mode)
    except OSError as exc:
        unsupported = {errno.ENOSYS, errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)}
        if exc.errno not in unsupported:
            raise
        warnings.warn(
            f"owner-only permission bits are unsupported for {path}",
            RuntimeWarning,
            stacklevel=2,
        )
        return False
    actual = stat.S_IMODE(path.stat().st_mode)
    if actual != mode:
        raise PermissionError(f"owner-only mode {mode:o} was not applied to {path}: got {actual:o}")
    return True


def _harden_runtime_owner_only(path: Path, mode: int) -> None:
    if os.name == "nt":
        from memory_queue import _harden_owner_only

        _harden_owner_only(Path(path), mode)
        return
    _set_owner_only(Path(path), mode)


def open_operational_db(
    path: Path,
    *,
    busy_ms: int,
    contract: OperationalDatabaseContract | None = None,
    initialize_contract: bool = False,
) -> sqlite3.Connection:
    """Open an owner-restricted rollback-journal operational database."""
    _require_operational_open_arguments(busy_ms, contract, initialize_contract)
    path = Path(path)
    validate_state_root(path.parent)
    expected = _operational_db_identity(path)
    connection = sqlite3.connect(
        path,
        timeout=busy_ms / 1_000,
        isolation_level=None,
    )
    try:
        _configure_operational_connection(
            connection,
            path,
            expected,
            busy_ms=busy_ms,
            contract=contract,
            initialize_contract=initialize_contract,
        )
        return connection
    except Exception:
        connection.close()
        raise


def _require_operational_open_arguments(
    busy_ms: int,
    contract: OperationalDatabaseContract | None,
    initialize_contract: bool,
) -> None:
    if busy_ms < 0:
        raise ValueError("busy_ms must be non-negative")
    if initialize_contract and contract is None:
        raise ValueError("initialize_contract requires an operational database contract")


def _operational_db_identity(path: Path) -> os.stat_result:
    """Create the database file, or validate an existing one without a probe."""
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        return validate_operational_db_file(path, path.parent, max_bytes=1 << 50)
    # O_EXCL proves this inode was just created here, so no connection in this
    # process can hold locks on it and closing the descriptor strips nothing.
    os.close(descriptor)
    return path.stat(follow_symlinks=False)


def _apply_operational_pragmas(connection: sqlite3.Connection, busy_ms: int) -> None:
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout={busy_ms:d}")
    mode = connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
    if str(mode).casefold() != "delete":
        raise sqlite3.OperationalError(f"SQLite refused journal_mode=DELETE: {mode}")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA trusted_schema=OFF")
    _require_pragma(connection, "synchronous", 2)
    _require_pragma(connection, "foreign_keys", 1)
    _require_pragma(connection, "trusted_schema", 0)
    _require_pragma(connection, "busy_timeout", busy_ms)


def _configure_operational_connection(
    connection: sqlite3.Connection,
    path: Path,
    expected: os.stat_result,
    *,
    busy_ms: int,
    contract: OperationalDatabaseContract | None,
    initialize_contract: bool,
) -> None:
    current = path.stat(follow_symlinks=False)
    if not os.path.samestat(expected, current):
        raise PermissionError("operational database identity changed while opening")
    _apply_operational_pragmas(connection, busy_ms)
    if contract is not None:
        _validate_or_initialize_operational_contract(
            connection,
            contract,
            initialize=initialize_contract,
        )
    _set_owner_only(path, 0o600)


def _pragma_integer(connection: sqlite3.Connection, name: str) -> int:
    row = connection.execute(f"PRAGMA {name}").fetchone()
    if row is None or isinstance(row[0], bool) or not isinstance(row[0], int):
        raise OperationalDatabaseContractError(
            f"operational database PRAGMA {name} returned an invalid value"
        )
    return row[0]


def _require_pragma(
    connection: sqlite3.Connection,
    name: str,
    expected: int,
) -> None:
    actual = _pragma_integer(connection, name)
    if actual != expected:
        raise OperationalDatabaseContractError(
            f"operational database PRAGMA {name} mismatch: expected {expected}, got {actual}"
        )


def _validate_or_initialize_operational_contract(
    connection: sqlite3.Connection,
    contract: OperationalDatabaseContract,
    *,
    initialize: bool,
) -> None:
    application_id = _pragma_integer(connection, "application_id")
    user_version = _pragma_integer(connection, "user_version")
    if initialize:
        if (application_id, user_version) == (0, 0):
            connection.execute(f"PRAGMA application_id={contract.application_id:d}")
            connection.execute(f"PRAGMA user_version={contract.user_version:d}")
            application_id = _pragma_integer(connection, "application_id")
            user_version = _pragma_integer(connection, "user_version")
        elif (application_id, user_version) != (
            contract.application_id,
            contract.user_version,
        ):
            raise OperationalDatabaseContractError(
                "cannot initialize a conflicting operational database contract"
            )
    if (application_id, user_version) != (
        contract.application_id,
        contract.user_version,
    ):
        raise OperationalDatabaseContractError(
            "operational database application_id or user_version mismatch"
        )


def run_resumable_migration(
    connection: sqlite3.Connection,
    statements: Sequence[MigrationStatement],
    *,
    final_invariant: Callable[[sqlite3.Connection], bool],
    killpoint: Callable[[str], None] | None = None,
) -> None:
    """Apply incomplete migration statements one transaction at a time."""
    names = [statement.name for statement in statements]
    if any(not name for name in names):
        raise ValueError("migration statement name must be non-empty")
    if len(names) != len(set(names)):
        raise ValueError("migration statement names must be unique")

    emit = killpoint or (lambda _event: None)
    for statement in statements:
        if statement.completed(connection):
            continue
        emit(f"before:{statement.name}")
        with begin_immediate(connection):
            connection.execute(statement.sql, tuple(statement.parameters))
            if not statement.completed(connection):
                error = OperationalDatabaseContractError(
                    f"migration statement invariant is incomplete: {statement.name}"
                )
                error.code = "operational_migration_incomplete"
                raise error
            emit(f"after_execute:{statement.name}")
        emit(f"after_commit:{statement.name}")
    if not final_invariant(connection):
        error = OperationalDatabaseContractError("operational migration final invariant is incomplete")
        error.code = "operational_migration_incomplete"
        raise error


def _contained_runtime_metadata(path: Path, state_root: Path) -> os.stat_result:
    root = Path(state_root).resolve(strict=True)
    try:
        path.parent.resolve(strict=True).relative_to(root)
        return path.lstat()
    except (OSError, ValueError) as exc:
        raise PermissionError("runtime file is outside the configured state root") from exc


def _require_bounded_regular_file(
    path: Path, metadata: os.stat_result, max_bytes: int
) -> None:
    if (
        stat.S_ISLNK(metadata.st_mode)
        or _windows_reparse_point(path)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size > max_bytes
    ):
        raise PermissionError("runtime file must be a bounded regular file")


def _require_owner_only_file(path: Path, metadata: os.stat_result) -> None:
    if os.name == "nt":
        from memory_queue import _is_owner_only

        if not _is_owner_only(path):
            raise PermissionError("runtime file must be owner-only")
        return
    mode = stat.S_IMODE(metadata.st_mode)
    if mode & 0o077 or mode & 0o600 != 0o600:
        raise PermissionError("runtime file must be owner-only")


def _validated_runtime_metadata(
    path: Path,
    state_root: Path,
    *,
    max_bytes: int,
    owner_only: bool,
) -> os.stat_result:
    """Validate a bounded regular runtime file from metadata alone."""
    if max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")
    metadata = _contained_runtime_metadata(path, state_root)
    _require_bounded_regular_file(path, metadata, max_bytes)
    if owner_only:
        _require_owner_only_file(path, metadata)
    return metadata


def _confirm_runtime_identity(path: Path, metadata: os.stat_result) -> None:
    """Prove the validated metadata still describes the file behind the path.

    Never call this for a file that may already have an open SQLite connection
    in this process. Closing any descriptor releases every POSIX advisory lock
    the process holds on that inode, whichever descriptor took them, so this
    probe would silently strip the locks a live connection depends on.
    """
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(metadata, opened):
            raise PermissionError("runtime file identity changed during validation")
    finally:
        os.close(descriptor)


def validate_runtime_file(
    path: Path,
    state_root: Path,
    *,
    max_bytes: int,
    owner_only: bool = False,
) -> os.stat_result:
    """Validate one bounded regular runtime file without following links."""
    path = Path(path)
    metadata = _validated_runtime_metadata(
        path, state_root, max_bytes=max_bytes, owner_only=owner_only
    )
    _confirm_runtime_identity(path, metadata)
    return metadata


def validate_operational_db_file(
    path: Path,
    state_root: Path,
    *,
    max_bytes: int,
    owner_only: bool = False,
) -> os.stat_result:
    """Validate an operational database without opening a second descriptor.

    `validate_runtime_file` proves identity by opening and closing its own
    descriptor. On POSIX that close drops every advisory lock this process holds
    on the inode, including the locks an already-open SQLite connection relies
    on, so concurrent writers stop excluding each other and one of them deletes
    the rollback journal underneath a live transaction. The database openers
    bracket `sqlite3.connect` with `lstat` and `samestat` instead, which proves
    the same identity across the connect without touching a descriptor.
    """
    return _validated_runtime_metadata(
        Path(path), state_root, max_bytes=max_bytes, owner_only=owner_only
    )


def open_readonly_operational_db(
    path: Path,
    state_root: Path,
    *,
    max_bytes: int,
    owner_only: bool = False,
    busy_ms: int = 0,
    contract: OperationalDatabaseContract | None = None,
) -> sqlite3.Connection:
    """Open a validated runtime SQLite database read-only and fail on path races."""
    if busy_ms < 0:
        raise ValueError("busy_ms must be non-negative")
    expected = validate_operational_db_file(
        path, state_root, max_bytes=max_bytes, owner_only=owner_only
    )
    database = sqlite3.connect(
        f"{Path(path).resolve(strict=True).as_uri()}?mode=ro",
        uri=True,
        timeout=busy_ms / 1_000,
        isolation_level=None,
    )
    try:
        current = Path(path).stat(follow_symlinks=False)
        if not os.path.samestat(expected, current):
            raise PermissionError("runtime database identity changed while opening")
        _apply_readonly_operational_pragmas(database, busy_ms)
        if contract is not None:
            _validate_or_initialize_operational_contract(
                database, contract, initialize=False
            )
        return database
    except Exception:
        database.close()
        raise


def _apply_readonly_operational_pragmas(
    database: sqlite3.Connection, busy_ms: int
) -> None:
    database.row_factory = sqlite3.Row
    database.execute(f"PRAGMA busy_timeout={busy_ms:d}")
    database.execute("PRAGMA foreign_keys=ON")
    database.execute("PRAGMA trusted_schema=OFF")
    database.execute("PRAGMA query_only=ON")
    mode = database.execute("PRAGMA journal_mode").fetchone()[0]
    if str(mode).casefold() != "delete":
        raise OperationalDatabaseContractError(
            f"operational database journal_mode mismatch: expected delete, got {mode}"
        )
    _require_pragma(database, "synchronous", 2)
    _require_pragma(database, "foreign_keys", 1)
    _require_pragma(database, "trusted_schema", 0)
    _require_pragma(database, "busy_timeout", busy_ms)


def read_runtime_bytes(
    path: Path,
    state_root: Path,
    *,
    max_bytes: int,
    owner_only: bool = False,
) -> bytes:
    """Read stable bounded runtime bytes through a no-follow descriptor."""
    expected = validate_runtime_file(path, state_root, max_bytes=max_bytes, owner_only=owner_only)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not os.path.samestat(expected, opened):
            raise PermissionError("runtime file identity changed before read")
        data = os.read(descriptor, max_bytes + 1)
        after = os.fstat(descriptor)
        if (
            not os.path.samestat(opened, after)
            or (opened.st_size, opened.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
            or len(data) > max_bytes
        ):
            raise PermissionError("runtime file changed during bounded read")
        return data
    finally:
        os.close(descriptor)


def capture_runtime_file_identity(
    path: Path, *, state_root: Path
) -> RuntimeFileIdentity:
    """Capture stable platform file identity for one runtime artifact."""
    target = Path(path)
    root = Path(state_root).resolve(strict=True)
    metadata = validate_runtime_file(target, root, max_bytes=1 << 50)
    if os.name == "nt":
        import windows_workspace

        handle = windows_workspace.open_exclusive_readonly_source_file(target)
        try:
            volume, file_id, is_directory = windows_workspace.identity(
                handle, directory=False
            )
            if is_directory:
                raise PermissionError("runtime artifact must be a regular file")
        finally:
            windows_workspace.close_handle(handle)
        current = target.stat(follow_symlinks=False)
        if not os.path.samestat(metadata, current):
            raise PermissionError("runtime file identity changed during capture")
        platform_name = "windows"
        volume_name = f"volume:{volume}"
        file_name = f"file-id:{file_id.hex()}"
    else:
        current = target.stat(follow_symlinks=False)
        if not os.path.samestat(metadata, current):
            raise PermissionError("runtime file identity changed during capture")
        platform_name = "posix"
        volume_name = f"dev:{current.st_dev}"
        file_name = f"ino:{current.st_ino}"
    return RuntimeFileIdentity(
        platform=platform_name,
        volume=volume_name,
        file_id=file_name,
        size=current.st_size,
        mtime_ns=current.st_mtime_ns,
    )


@contextlib.contextmanager
def begin_immediate(
    connection: sqlite3.Connection,
    *,
    before_commit: Callable[[], None] | None = None,
) -> Iterator[sqlite3.Connection]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
        if before_commit is not None:
            before_commit()
        connection.commit()
    except BaseException as original:
        try:
            connection.rollback()
        except BaseException as rollback_error:
            original.__context__ = rollback_error
        raise


def fsync_file(path: Path) -> None:
    with Path(path).open("rb+") as handle:
        os.fsync(handle.fileno())


def fsync_directory(path: Path) -> None:
    """Sync directory metadata or fail when the platform cannot prove it."""
    if os.name == "nt":
        import windows_workspace

        handle: int | None = None
        try:
            handle = windows_workspace.open_writable_directory_path(Path(path))
            if not windows_workspace.flush_directory(handle):
                raise ctypes.WinError(ctypes.get_last_error())
        except (OSError, RuntimeError) as exc:
            raise _metadata_durability_error("Windows directory flush failed", exc) from exc
        finally:
            if handle is not None:
                windows_workspace.close_handle(handle)
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(Path(path), flags)
    except OSError as exc:
        raise _metadata_durability_error("directory open for fsync failed", exc) from exc
    try:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            raise _metadata_durability_error("directory fsync failed", exc) from exc
    finally:
        os.close(descriptor)


def _metadata_durability_error(
    message: str,
    error: BaseException,
) -> MetadataDurabilityUnavailable:
    error_number = getattr(error, "errno", None)
    if error_number is None:
        result = MetadataDurabilityUnavailable(f"{message}: {error}")
    else:
        result = MetadataDurabilityUnavailable(error_number, f"{message}: {error}")
    if getattr(error, "winerror", None) is not None:
        result.winerror = error.winerror
    return result


def _publication_digest(path: Path, parent: Path, *, max_bytes: int) -> str | None:
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
    if not isinstance(replace, bool):
        raise TypeError("replace must be a boolean")
    if re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
        raise ValueError("expected_sha256 must be lowercase 64-hex")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")

    staged = Path(staged)
    destination = Path(destination)
    try:
        staged_parent = staged.parent.resolve(strict=True)
        destination_parent = destination.parent.resolve(strict=True)
    except OSError as exc:
        raise PermissionError("publication parent must exist") from exc
    if staged_parent != destination_parent:
        raise ValueError("staged and destination must be sibling paths")
    if os.path.normcase(staged.name) == os.path.normcase(destination.name):
        raise ValueError("staged and destination must be distinct names")
    if _known_network_path(staged_parent) or _windows_reparse_point(staged_parent):
        raise UnsafeStateRoot("durable publication requires one local non-reparse parent")
    staged = staged_parent / staged.name
    destination = destination_parent / destination.name

    staged_digest = _publication_digest(staged, staged_parent, max_bytes=max_bytes)
    destination_digest = _publication_digest(
        destination, staged_parent, max_bytes=max_bytes
    )
    if destination_digest == expected_sha256:
        if staged_digest is None:
            if os.name != "nt":
                fsync_directory(staged_parent)
            return "adopted"
        if staged_digest == expected_sha256:
            return "duplicate"
        raise RuntimeError("durable publication conflict: staged bytes do not match")
    if staged_digest != expected_sha256:
        raise RuntimeError("durable publication conflict: expected bytes are unavailable")

    if os.name == "nt":
        import windows_workspace

        try:
            windows_workspace.flush_file_path(staged)
            windows_workspace.move_file_write_through(
                staged,
                destination,
                replace=replace,
            )
        except FileExistsError:
            raise
        except (OSError, RuntimeError) as exc:
            raise _metadata_durability_error("Windows metadata publication failed", exc) from exc
    else:
        try:
            fsync_file(staged)
        except OSError as exc:
            raise _metadata_durability_error("staged file fsync failed", exc) from exc
        if replace:
            try:
                os.replace(staged, destination)
            except OSError as exc:
                raise _metadata_durability_error("metadata replacement failed", exc) from exc
        else:
            try:
                os.link(staged, destination, follow_symlinks=False)
            except FileExistsError:
                raise
            except OSError as exc:
                raise _metadata_durability_error("metadata publication link failed", exc) from exc
            try:
                os.unlink(staged)
            except OSError:
                fsync_directory(staged_parent)
                raise
        fsync_directory(staged_parent)

    if _publication_digest(destination, staged_parent, max_bytes=max_bytes) != expected_sha256:
        raise RuntimeError("durable publication conflict: destination read-back failed")
    return "published"


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
    if not isinstance(data, bytes):
        raise TypeError("runtime file data must be bytes")
    if not isinstance(create_only, bool):
        raise TypeError("create_only must be a boolean")
    if isinstance(mode, bool) or not isinstance(mode, int) or not 0 <= mode <= 0o777:
        raise ValueError("mode must be a valid permission mode")
    if create_only and (expected is not None or expected_sha256 is not None):
        raise ValueError("create-only publication does not accept replacement evidence")
    if not create_only and (expected is None or expected_sha256 is None):
        raise ValueError("replacement requires expected identity and SHA-256")
    if expected_sha256 is not None and re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
        raise ValueError("expected_sha256 must be lowercase 64-hex")

    root = Path(state_root).resolve(strict=True)
    destination = Path(path)
    try:
        parent = destination.parent.resolve(strict=True)
        parent.relative_to(root)
    except (OSError, ValueError) as exc:
        raise PermissionError("runtime publication path is outside the state root") from exc
    destination = parent / destination.name
    if not create_only:
        current = capture_runtime_file_identity(destination, state_root=root)
        if current != expected:
            raise PermissionError("runtime file identity changed before publication")
        current_bytes = read_runtime_bytes(
            destination, root, max_bytes=max(1, current.size)
        )
        if sha256_bytes(current_bytes) != expected_sha256:
            raise PermissionError("runtime file bytes changed before publication")

    staged = parent / f".{destination.name}.{secrets.token_hex(16)}.tmp"
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
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("runtime staging write made no progress")
            view = view[written:]
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        with contextlib.suppress(OSError):
            staged.unlink()
        raise
    else:
        os.close(descriptor)
    _harden_runtime_owner_only(staged, mode)

    digest = sha256_bytes(data)
    try:
        outcome = durable_publish_file(
            staged,
            destination,
            replace=not create_only,
            expected_sha256=digest,
            max_bytes=max(1, len(data), 0 if expected is None else expected.size),
        )
    except BaseException:
        with contextlib.suppress(OSError):
            staged.unlink()
        raise
    if outcome == "duplicate":
        staged.unlink()
        fsync_directory(parent)
    _harden_runtime_owner_only(destination, mode)
    published = capture_runtime_file_identity(destination, state_root=root)
    if published.size != len(data):
        raise RuntimeError("published runtime file size changed during read-back")
    return published


def sync_runtime_directory(path: Path) -> None:
    """Expose the shared checked metadata sync boundary to runtime publishers."""
    fsync_directory(Path(path))


def restricted_relative_path(value: str, allowed_roots: tuple[str, ...]) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or "//" in value:
        raise ValueError("path must be a non-empty normalized POSIX relative path")
    if re.match(r"^[A-Za-z]:", value):
        raise ValueError("drive-qualified paths are forbidden")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or str(path) != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("path must be normalized and relative")
    roots = tuple(PurePosixPath(root) for root in allowed_roots)
    if not roots or not any(path == root or root in path.parents for root in roots):
        raise ValueError("path is outside every allowed root")
    return path


def validate_schema(instance: object, schema_path: Path) -> None:
    """Validate the closed JSON Schema subset used by committed contracts."""
    try:
        schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SchemaValidationError(f"cannot load schema {schema_path}: {exc}") from exc
    validate_schema_object(instance, schema)


def validate_schema_object(instance: object, schema: object) -> None:
    """Validate against an already captured and parsed closed schema."""
    if not isinstance(schema, dict):
        raise SchemaValidationError("schema root must be an object")
    _validate_rule(instance, schema, "$", root=schema)


def _validate_rule(
    instance: object,
    rule: dict[str, Any],
    location: str,
    *,
    root: dict[str, Any] | None = None,
) -> None:
    if root is None:
        root = rule
    reference = rule.get("$ref")
    if reference is not None:
        if not isinstance(reference, str) or not reference.startswith("#/"):
            raise SchemaValidationError(
                f"{location}: only internal JSON Pointer refs are supported"
            )
        target: object = root
        for raw_part in reference[2:].split("/"):
            part = raw_part.replace("~1", "/").replace("~0", "~")
            if not isinstance(target, dict) or part not in target:
                raise SchemaValidationError(f"{location}: unresolved schema ref {reference}")
            target = target[part]
        if not isinstance(target, dict):
            raise SchemaValidationError(f"{location}: schema ref {reference} is not an object")
        _validate_rule(instance, target, location, root=root)
        return
    if "oneOf" in rule:
        matches = 0
        for option in rule["oneOf"]:
            try:
                _validate_rule(instance, option, location, root=root)
            except SchemaValidationError:
                continue
            matches += 1
        if matches != 1:
            raise SchemaValidationError(
                f"{location}: expected exactly one oneOf match, got {matches}"
            )
    if "const" in rule and not _json_equal(instance, rule["const"]):
        raise SchemaValidationError(f"{location}: expected const {rule['const']!r}")
    if "enum" in rule and not any(_json_equal(instance, candidate) for candidate in rule["enum"]):
        raise SchemaValidationError(f"{location}: value is not in enum")

    expected = rule.get("type")
    expected_types: tuple[str, ...] = ()
    if expected is not None:
        expected_types = _schema_types(expected)
        if not _matches_type(instance, expected):
            raise SchemaValidationError(f"{location}: expected {expected}")

    if "object" in expected_types and isinstance(instance, dict):
        assert isinstance(instance, dict)
        required = rule.get("required", [])
        missing = [key for key in required if key not in instance]
        if missing:
            raise SchemaValidationError(f"{location}: missing required properties {missing}")
        properties = rule.get("properties", {})
        if rule.get("additionalProperties") is False:
            unknown = sorted(set(instance) - set(properties))
            if unknown:
                raise SchemaValidationError(f"{location}: unknown properties {unknown}")
        for key, value in instance.items():
            if key in properties:
                _validate_rule(value, properties[key], f"{location}.{key}", root=root)
    elif "array" in expected_types and isinstance(instance, list):
        assert isinstance(instance, list)
        _check_bound(len(instance), rule, "minItems", "maxItems", location)
        if rule.get("uniqueItems") is True:
            for index, item in enumerate(instance):
                if any(_json_equal(item, previous) for previous in instance[:index]):
                    raise SchemaValidationError(
                        f"{location}: expected uniqueItems, duplicate at index {index}"
                    )
        if "items" in rule:
            for index, item in enumerate(instance):
                _validate_rule(item, rule["items"], f"{location}[{index}]", root=root)
    elif "string" in expected_types and isinstance(instance, str):
        assert isinstance(instance, str)
        _check_bound(len(instance), rule, "minLength", "maxLength", location)
        if "pattern" in rule and re.search(rule["pattern"], instance) is None:
            raise SchemaValidationError(f"{location}: string does not match pattern")
    elif (
        {"integer", "number"}.intersection(expected_types)
        and isinstance(instance, (int, float))
        and not isinstance(instance, bool)
    ):
        assert isinstance(instance, (int, float)) and not isinstance(instance, bool)
        _check_bound(instance, rule, "minimum", "maximum", location)
        if "exclusiveMinimum" in rule and instance <= rule["exclusiveMinimum"]:
            raise SchemaValidationError(f"{location}: below exclusiveMinimum")
        if "exclusiveMaximum" in rule and instance >= rule["exclusiveMaximum"]:
            raise SchemaValidationError(f"{location}: above exclusiveMaximum")


def _schema_types(expected: object) -> tuple[str, ...]:
    if isinstance(expected, str):
        values = (expected,)
    elif isinstance(expected, list):
        if not expected:
            raise SchemaValidationError("schema type array must not be empty")
        if not all(isinstance(value, str) for value in expected):
            raise SchemaValidationError("schema type array must contain only strings")
        if len(set(expected)) != len(expected):
            raise SchemaValidationError("schema type array must contain unique values")
        values = tuple(expected)
    else:
        raise SchemaValidationError("schema type must be a string or array of strings")

    unknown = [value for value in values if value not in _SCHEMA_TYPE_CHECKS]
    if unknown:
        raise SchemaValidationError(f"unsupported schema type: {unknown[0]}")
    return values


def _matches_type(instance: object, expected: object) -> bool:
    return _matches_types(instance, _schema_types(expected))


def _matches_types(instance: object, expected: tuple[str, ...]) -> bool:
    return any(_SCHEMA_TYPE_CHECKS[value](instance) for value in expected)


_SCHEMA_TYPE_CHECKS = {
    "object": lambda value: isinstance(value, dict),
    "array": lambda value: isinstance(value, list),
    "string": lambda value: isinstance(value, str),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "number": lambda value: (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and (not isinstance(value, float) or math.isfinite(value))
    ),
    "boolean": lambda value: isinstance(value, bool),
    "null": lambda value: value is None,
}


def _json_equal(left: object, right: object) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    return left == right


def _check_bound(
    value: int | float,
    rule: dict[str, Any],
    minimum_name: str,
    maximum_name: str,
    location: str,
) -> None:
    if minimum_name in rule and value < rule[minimum_name]:
        raise SchemaValidationError(f"{location}: below {minimum_name}")
    if maximum_name in rule and value > rule[maximum_name]:
        raise SchemaValidationError(f"{location}: above {maximum_name}")
