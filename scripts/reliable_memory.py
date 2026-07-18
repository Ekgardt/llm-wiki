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
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


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


def _sqlite_lock_probe(
    root: Path, *, deadline: float = float("inf")
) -> bool | None:
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


def open_operational_db(path: Path, *, busy_ms: int) -> sqlite3.Connection:
    """Open an owner-restricted rollback-journal operational database."""
    if busy_ms < 0:
        raise ValueError("busy_ms must be non-negative")
    path = Path(path)
    validate_state_root(path.parent)
    expected: os.stat_result | None = None
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except FileExistsError:
        expected = validate_runtime_file(
            path,
            path.parent,
            max_bytes=1 << 50,
        )
    else:
        os.close(descriptor)
        expected = path.stat(follow_symlinks=False)
    connection = sqlite3.connect(path, timeout=busy_ms / 1_000)
    try:
        current = path.stat(follow_symlinks=False)
        if expected is None or not os.path.samestat(expected, current):
            raise PermissionError("operational database identity changed while opening")
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={busy_ms:d}")
        mode = connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
        if str(mode).casefold() != "delete":
            raise sqlite3.OperationalError(f"SQLite refused journal_mode=DELETE: {mode}")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        _set_owner_only(path, 0o600)
        return connection
    except Exception:
        connection.close()
        raise


def validate_runtime_file(
    path: Path,
    state_root: Path,
    *,
    max_bytes: int,
    owner_only: bool = False,
) -> os.stat_result:
    """Validate one bounded regular runtime file without following links."""
    if max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")
    path = Path(path)
    root = Path(state_root).resolve(strict=True)
    try:
        path.parent.resolve(strict=True).relative_to(root)
        metadata = path.lstat()
    except (OSError, ValueError) as exc:
        raise PermissionError("runtime file is outside the configured state root") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or _windows_reparse_point(path)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size > max_bytes
    ):
        raise PermissionError("runtime file must be a bounded regular file")
    if owner_only:
        if os.name == "nt":
            from memory_queue import _is_owner_only

            if not _is_owner_only(path):
                raise PermissionError("runtime file must be owner-only")
        else:
            mode = stat.S_IMODE(metadata.st_mode)
            if mode & 0o077 or mode & 0o600 != 0o600:
                raise PermissionError("runtime file must be owner-only")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(metadata, opened):
            raise PermissionError("runtime file identity changed during validation")
    finally:
        os.close(descriptor)
    return metadata


def open_readonly_operational_db(
    path: Path,
    state_root: Path,
    *,
    max_bytes: int,
    owner_only: bool = False,
) -> sqlite3.Connection:
    """Open a validated runtime SQLite database read-only and fail on path races."""
    expected = validate_runtime_file(
        path, state_root, max_bytes=max_bytes, owner_only=owner_only
    )
    database = sqlite3.connect(
        f"{Path(path).resolve(strict=True).as_uri()}?mode=ro", uri=True, timeout=0
    )
    try:
        current = Path(path).stat(follow_symlinks=False)
        if not os.path.samestat(expected, current):
            raise PermissionError("runtime database identity changed while opening")
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA query_only=ON")
        return database
    except Exception:
        database.close()
        raise


def read_runtime_bytes(
    path: Path,
    state_root: Path,
    *,
    max_bytes: int,
    owner_only: bool = False,
) -> bytes:
    """Read stable bounded runtime bytes through a no-follow descriptor."""
    expected = validate_runtime_file(
        path, state_root, max_bytes=max_bytes, owner_only=owner_only
    )
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
            or (opened.st_size, opened.st_mtime_ns)
            != (after.st_size, after.st_mtime_ns)
            or len(data) > max_bytes
        ):
            raise PermissionError("runtime file changed during bounded read")
        return data
    finally:
        os.close(descriptor)


@contextlib.contextmanager
def begin_immediate(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def fsync_file(path: Path) -> None:
    with Path(path).open("rb+") as handle:
        os.fsync(handle.fileno())


def fsync_directory(path: Path) -> None:
    """Sync directory metadata where the platform exposes directory handles."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(Path(path), flags)
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EINVAL, errno.ENOTSUP, errno.EPERM}:
            return
        raise
    try:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EBADF, errno.EINVAL, errno.ENOTSUP, errno.EPERM}:
                raise
    finally:
        os.close(descriptor)


def restricted_relative_path(value: str, allowed_roots: tuple[str, ...]) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or "//" in value:
        raise ValueError("path must be a non-empty normalized POSIX relative path")
    if re.match(r"^[A-Za-z]:", value):
        raise ValueError("drive-qualified paths are forbidden")
    path = PurePosixPath(value)
    if path.is_absolute() or str(path) != value or any(part in {"", ".", ".."} for part in path.parts):
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
    _validate_rule(instance, schema, "$")


def _validate_rule(instance: object, rule: dict[str, Any], location: str) -> None:
    if "oneOf" in rule:
        matches = 0
        for option in rule["oneOf"]:
            try:
                _validate_rule(instance, option, location)
            except SchemaValidationError:
                continue
            matches += 1
        if matches != 1:
            raise SchemaValidationError(f"{location}: expected exactly one oneOf match, got {matches}")
    if "const" in rule and not _json_equal(instance, rule["const"]):
        raise SchemaValidationError(f"{location}: expected const {rule['const']!r}")
    if "enum" in rule and not any(_json_equal(instance, candidate) for candidate in rule["enum"]):
        raise SchemaValidationError(f"{location}: value is not in enum")

    expected = rule.get("type")
    if expected is not None and not _matches_type(instance, expected):
        raise SchemaValidationError(f"{location}: expected {expected}")

    if expected == "object":
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
                _validate_rule(value, properties[key], f"{location}.{key}")
    elif expected == "array":
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
                _validate_rule(item, rule["items"], f"{location}[{index}]")
    elif expected == "string":
        assert isinstance(instance, str)
        _check_bound(len(instance), rule, "minLength", "maxLength", location)
        if "pattern" in rule and re.search(rule["pattern"], instance) is None:
            raise SchemaValidationError(f"{location}: string does not match pattern")
    elif expected in {"integer", "number"}:
        assert isinstance(instance, (int, float)) and not isinstance(instance, bool)
        _check_bound(instance, rule, "minimum", "maximum", location)


def _matches_type(instance: object, expected: str) -> bool:
    checks = {
        "object": lambda: isinstance(instance, dict),
        "array": lambda: isinstance(instance, list),
        "string": lambda: isinstance(instance, str),
        "integer": lambda: isinstance(instance, int) and not isinstance(instance, bool),
        "number": lambda: (
            isinstance(instance, (int, float))
            and not isinstance(instance, bool)
            and (not isinstance(instance, float) or math.isfinite(instance))
        ),
        "boolean": lambda: isinstance(instance, bool),
        "null": lambda: instance is None,
    }
    if expected not in checks:
        raise SchemaValidationError(f"unsupported schema type: {expected}")
    return checks[expected]()


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
