"""Coherent staging images for encrypted private-vault backups."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import markdown_transaction
import memory_queue
from installed_memory_repair import validate_reliability_v3_runtime
from operational_ownership import (
    OperationalOwnershipError,
    OwnerLease,
    OwnershipRegistry,
)
from reliable_memory import (
    _harden_runtime_owner_only,
    canonical_json_bytes,
    fsync_directory,
    fsync_file,
    sha256_bytes,
)

_MAX_ENTRIES = 1_000_000
_CHUNK_BYTES = 1024 * 1024
_MAX_REPOSITORY_FILE_BYTES = 16 * 1024
_RESTIC_VERSION = "0.19.1"
_ACTIVE_DATABASES = (
    "markdown-transactions-v3.sqlite3",
    "queue-v3.sqlite3",
)
_DATABASE_SIDECARS = frozenset(
    f"{name}{suffix}"
    for name in _ACTIVE_DATABASES
    for suffix in ("-journal", "-wal", "-shm")
)
_ALLOWED_RUNTIME_FINDINGS = frozenset(
    {
        "queue_task_retained",
        "queue_attempt_history_retained",
        "queue_source_failure_retained",
        "queue_source_link_retained",
        "capture_intent_retained",
        "capture_link_retained",
        "capture_link_resolution_retained",
        "capture_link_seal_retained",
        "capture_semantic_decision_retained",
        "queue_purge_authorization_retained",
        "queue_corrupt_export_retained",
        "queue_corrupt_disposition_retained",
        "queue_corrupt_package_retained",
        "queue_corrupt_purge_retained",
        "queue_result_retained",
        "queue_quarantine_retained",
        "capture_binding_projection_retained",
        "transaction_undo_retained",
        "transaction_artifact_retained",
    }
)


class BackupError(RuntimeError):
    """Stable fail-closed private-backup error."""

    def __init__(self, code: str, details: tuple[str, ...] = ()) -> None:
        self.code = code
        self.details = details
        super().__init__(code)


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


@dataclass(frozen=True)
class _Entry:
    source: Path
    path: str
    kind: str
    size: int = 0
    sha256: str | None = None
    link_target: str | None = None

    def manifest(self) -> dict[str, object]:
        value: dict[str, object] = {
            "path": self.path,
            "kind": self.kind,
        }
        if self.kind == "file":
            value.update(size=self.size, sha256=self.sha256)
        elif self.kind == "symlink":
            assert self.link_target is not None
            value["target_sha256"] = sha256_bytes(
                self.link_target.encode("utf-8", errors="surrogatepass")
            )
        return value


def _deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise TimeoutError("backup deadline reached")


def _validate_command(command: list[str]) -> None:
    if not command:
        raise ValueError("invalid bounded command")
    for item in command:
        if not isinstance(item, str):
            raise ValueError("invalid bounded command")
        if not item:
            raise ValueError("invalid bounded command")


def _validate_output_limit(max_output_bytes: int) -> None:
    if type(max_output_bytes) is not int:
        raise ValueError("invalid bounded command")
    if max_output_bytes < 1:
        raise ValueError("invalid bounded command")
    if max_output_bytes > 16 * 1024 * 1024:
        raise ValueError("invalid bounded command")


def _start_command(command: list[str], cwd: Path) -> subprocess.Popen[bytes]:
    try:
        return subprocess.Popen(
            command,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise BackupError("restic_unavailable") from exc


def _capture_chunk(
    chunk: bytes,
    output: bytearray,
    *,
    lock: threading.Lock,
    overflow: threading.Event,
    total: list[int],
    maximum: int,
) -> None:
    with lock:
        remaining = max(maximum - total[0], 0)
        output.extend(chunk[:remaining])
        total[0] += min(len(chunk), remaining)
        if len(chunk) > remaining:
            overflow.set()


def _read_command_stream(
    stream,
    output: bytearray,
    *,
    lock: threading.Lock,
    overflow: threading.Event,
    total: list[int],
    maximum: int,
) -> None:
    while True:
        chunk = stream.read(64 * 1024)
        if not chunk:
            return
        _capture_chunk(
            chunk,
            output,
            lock=lock,
            overflow=overflow,
            total=total,
            maximum=maximum,
        )


def _output_readers(
    process: subprocess.Popen[bytes],
    outputs: tuple[bytearray, bytearray],
    overflow: threading.Event,
    maximum: int,
) -> list[threading.Thread]:
    assert process.stdout is not None and process.stderr is not None
    lock = threading.Lock()
    total = [0]
    return [
        threading.Thread(
            target=_read_command_stream,
            kwargs={
                "stream": stream,
                "output": output,
                "lock": lock,
                "overflow": overflow,
                "total": total,
                "maximum": maximum,
            },
            name=f"restic-output-{index}",
            daemon=True,
        )
        for index, (stream, output) in enumerate(
            zip((process.stdout, process.stderr), outputs, strict=True)
        )
    ]


def _monitor_command(
    process: subprocess.Popen[bytes], overflow: threading.Event, deadline: float
) -> str:
    while process.poll() is None:
        if overflow.is_set():
            process.kill()
            return "overflow"
        if time.monotonic() >= deadline:
            process.kill()
            return "timeout"
        time.sleep(0.01)
    return "complete"


def _join_command(
    process: subprocess.Popen[bytes], readers: list[threading.Thread]
) -> None:
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        raise BackupError("restic_termination_failed") from exc
    for reader in readers:
        reader.join(timeout=5)
    if any(reader.is_alive() for reader in readers):
        raise BackupError("restic_output_reader_failed")


def _raise_command_status(status: str) -> None:
    if status == "overflow":
        raise BackupError("restic_output_limit")
    if status == "timeout":
        raise BackupError("restic_timeout")


def _run_bounded(
    command: list[str],
    *,
    cwd: Path,
    deadline: float,
    max_output_bytes: int = 1024 * 1024,
) -> CommandResult:
    _validate_command(command)
    _validate_output_limit(max_output_bytes)
    _deadline(deadline)
    process = _start_command(command, cwd)
    outputs = (bytearray(), bytearray())
    overflow = threading.Event()
    readers = _output_readers(process, outputs, overflow, max_output_bytes)
    for reader in readers:
        reader.start()
    status = _monitor_command(process, overflow, deadline)
    _join_command(process, readers)
    if overflow.is_set():
        status = "overflow"
    _raise_command_status(status)
    return CommandResult(process.returncode, bytes(outputs[0]), bytes(outputs[1]))


def _metadata(path: Path) -> tuple[int, int, int, int, int, int]:
    value = path.lstat()
    return (
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
        value.st_dev,
        value.st_ino,
    )


def _hash_file(path: Path, deadline: float) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            _deadline(deadline)
            chunk = stream.read(_CHUNK_BYTES)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)


def _directory_children(directory: Path) -> list[os.DirEntry[str]]:
    try:
        with os.scandir(directory) as scanned:
            return sorted(scanned, key=lambda item: item.name)
    except OSError as exc:
        raise BackupError("source_unreadable") from exc


def _skip_source_entry(
    relative: Path,
    name: str,
    excluded_top_level: frozenset[str],
    excluded_files: frozenset[str],
) -> bool:
    if not relative.parts and name in excluded_top_level:
        return True
    return name in excluded_files


def _stable_file_entry(
    path: Path, logical: str, before: tuple[int, int, int, int, int, int], deadline: float
) -> _Entry:
    digest = _hash_file(path, deadline)
    if before != _metadata(path):
        raise BackupError("source_changed")
    return _Entry(path, logical, "file", before[1], digest)


def _stable_symlink_entry(
    path: Path, logical: str, before: tuple[int, int, int, int, int, int]
) -> _Entry:
    target = os.readlink(path)
    if before != _metadata(path):
        raise BackupError("source_changed")
    return _Entry(path, logical, "symlink", link_target=target)


def _scanned_entry(path: Path, logical: str, deadline: float) -> tuple[_Entry, bool]:
    before = _metadata(path)
    mode = before[0]
    if stat.S_ISDIR(mode):
        return _Entry(path, logical, "directory"), True
    if stat.S_ISREG(mode):
        return _stable_file_entry(path, logical, before, deadline), False
    if stat.S_ISLNK(mode):
        return _stable_symlink_entry(path, logical, before), False
    raise BackupError("unsupported_source_type")


def _append_tree_entry(
    *,
    child_path: Path,
    child_relative: Path,
    logical: str,
    deadline: float,
    entries: list[_Entry],
    walk,
) -> None:
    try:
        entry, directory = _scanned_entry(child_path, logical, deadline)
    except BackupError:
        raise
    except OSError as exc:
        raise BackupError("source_unreadable") from exc
    entries.append(entry)
    if directory:
        walk(child_path, child_relative)
    if len(entries) > _MAX_ENTRIES:
        raise BackupError("source_entry_limit")


def _scan_tree(
    source_root: Path,
    *,
    prefix: str,
    deadline: float,
    excluded_top_level: frozenset[str] = frozenset(),
    excluded_files: frozenset[str] = frozenset(),
) -> tuple[_Entry, ...]:
    entries: list[_Entry] = []

    def walk(directory: Path, relative: Path) -> None:
        _deadline(deadline)
        for child in _directory_children(directory):
            _deadline(deadline)
            if _skip_source_entry(
                relative, child.name, excluded_top_level, excluded_files
            ):
                continue
            child_path = Path(child.path)
            child_relative = relative / child.name
            logical = f"{prefix}/{child_relative.as_posix()}"
            _append_tree_entry(
                child_path=child_path,
                child_relative=child_relative,
                logical=logical,
                deadline=deadline,
                entries=entries,
                walk=walk,
            )

    walk(source_root, Path())
    return tuple(entries)


def _copy_regular_file(source: Path, destination: Path) -> None:
    shutil.copyfile(source, destination, follow_symlinks=False)
    shutil.copystat(source, destination, follow_symlinks=False)


def _copy_file_entry(entry: _Entry, destination: Path, deadline: float) -> None:
    _copy_regular_file(entry.source, destination)
    _harden_runtime_owner_only(destination, 0o600)
    if not destination.is_file():
        raise BackupError("staging_copy_mismatch")
    if _hash_file(destination, deadline) != entry.sha256:
        raise BackupError("staging_copy_mismatch")


def _copy_entry(entry: _Entry, image: Path, deadline: float) -> None:
    destination = image / entry.path
    if entry.kind == "directory":
        destination.mkdir()
        _harden_runtime_owner_only(destination, 0o700)
        return
    if entry.kind == "file":
        _copy_file_entry(entry, destination, deadline)
        return
    assert entry.link_target is not None
    os.symlink(entry.link_target, destination, target_is_directory=False)


def _copy_entries(entries: tuple[_Entry, ...], image: Path, deadline: float) -> None:
    for entry in entries:
        _deadline(deadline)
        _copy_entry(entry, image, deadline)


def _sqlite_online_backup(source: Path, destination: Path, deadline: float) -> None:
    source_uri = f"{source.resolve(strict=True).as_uri()}?mode=ro"
    try:
        with contextlib.closing(
            sqlite3.connect(source_uri, uri=True, timeout=0, isolation_level=None)
        ) as source_database, contextlib.closing(
            sqlite3.connect(destination, timeout=0, isolation_level=None)
        ) as destination_database:
            source_database.execute("PRAGMA query_only=ON")

            def progress(_status: int, _remaining: int, _total: int) -> None:
                _deadline(deadline)

            source_database.backup(
                destination_database,
                pages=256,
                progress=progress,
                sleep=0.01,
            )
    except TimeoutError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise BackupError("database_backup_failed") from exc
    _harden_runtime_owner_only(destination, 0o600)
    fsync_file(destination)


def _remove_snapshot_owner(database_path: Path, lease: OwnerLease) -> None:
    with contextlib.closing(sqlite3.connect(database_path)) as database:
        result = database.execute(
            """DELETE FROM maintenance_owners
               WHERE role=? AND scope=? AND actor_id=? AND owner_token=?
                 AND fencing_epoch=? AND process_id=? AND process_start_identity=?""",
            (
                lease.role,
                lease.scope,
                lease.actor_id,
                lease.token,
                lease.epoch,
                lease.process.pid,
                lease.process.start_identity,
            ),
        )
        if result.rowcount != 1:
            raise BackupError("backup_owner_projection_invalid")
        if database.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise BackupError("database_integrity_failed")
        database.commit()
    fsync_file(database_path)


def _require_regular_database(path: Path) -> None:
    if path.is_symlink():
        raise BackupError("database_validation_failed")
    if not stat.S_ISREG(path.stat().st_mode):
        raise BackupError("database_validation_failed")


def _validated_databases(
    state_root: Path,
) -> tuple[dict[str, object], dict[str, object]]:
    run = state_root / "run"
    coordinator_path = run / _ACTIVE_DATABASES[0]
    queue_path = run / _ACTIVE_DATABASES[1]
    try:
        _require_regular_database(coordinator_path)
        _require_regular_database(queue_path)
        coordinator = markdown_transaction.validate_coordinator_v3_database(
            coordinator_path, state_root=state_root
        )
        queue = memory_queue.validate_queue_v3_database(
            queue_path, state_root=state_root
        )
    except BackupError:
        raise
    except (OSError, PermissionError, sqlite3.Error, ValueError) as exc:
        raise BackupError("database_validation_failed") from exc
    return coordinator, queue


def _database_manifest_entry(
    run: Path, name: str, validation: dict[str, object]
) -> dict[str, object]:
    return {
        "path": f"state/run/{name}",
        "sha256": _hash_file(run / name, float("inf")),
        "application_id": validation["application_id"],
        "user_version": validation["user_version"],
    }


def _database_manifest(image: Path) -> list[dict[str, object]]:
    state_root = image / "state"
    run = state_root / "run"
    coordinator, queue = _validated_databases(state_root)
    return [
        _database_manifest_entry(run, _ACTIVE_DATABASES[0], coordinator),
        _database_manifest_entry(run, _ACTIVE_DATABASES[1], queue),
    ]


def _maintain_heartbeat(
    registry: OwnershipRegistry,
    lease: OwnerLease,
    stop: threading.Event,
    failures: list[BaseException],
) -> None:
    current = lease
    while not stop.wait(current.heartbeat_seconds):
        try:
            current = registry.heartbeat(current)
        except BaseException as exc:
            failures.append(exc)
            return


def _verify_heartbeat(
    registry: OwnershipRegistry, lease: OwnerLease, failures: list[BaseException]
) -> None:
    if failures:
        raise BackupError("backup_owner_fence_lost") from failures[0]
    registry.heartbeat(lease)


def _stop_heartbeat(
    thread: threading.Thread,
    stop: threading.Event,
    lease: OwnerLease,
    body_failed: bool,
) -> None:
    stop.set()
    thread.join(timeout=lease.heartbeat_seconds * 2)
    if body_failed:
        return
    if thread.is_alive():
        raise BackupError("backup_heartbeat_stop_timeout")


@contextlib.contextmanager
def _heartbeat(registry: OwnershipRegistry, lease: OwnerLease) -> Iterator[None]:
    stop = threading.Event()
    failures: list[BaseException] = []
    thread = threading.Thread(
        target=_maintain_heartbeat,
        args=(registry, lease, stop, failures),
        name="backup-owner-heartbeat",
        daemon=True,
    )
    thread.start()
    body_failed = False
    try:
        yield
        _verify_heartbeat(registry, lease, failures)
    except BaseException:
        body_failed = True
        raise
    finally:
        _stop_heartbeat(thread, stop, lease, body_failed)


def _resolve_directory(path: Path, code: str) -> Path:
    try:
        resolved = Path(path).resolve(strict=True)
    except OSError as exc:
        raise BackupError(code) from exc
    if not resolved.is_dir():
        raise BackupError(code)
    return resolved


def _resolve_staging(path: Path) -> Path:
    selected = Path(path)
    if selected.is_symlink():
        raise BackupError("backup_path_invalid")
    return _resolve_directory(selected, "backup_path_invalid")


def _validate_root_locations(vault: Path, state: Path, staging: Path) -> None:
    if vault != state:
        if _paths_overlap(vault, state):
            raise BackupError("source_roots_overlap")
    for source in {vault, state}:
        if _paths_overlap(staging, source):
            raise BackupError("staging_overlaps_source")


def _paths_overlap(left: Path, right: Path) -> bool:
    return left.is_relative_to(right) or right.is_relative_to(left)


def _directory_occupied(path: Path, code: str) -> bool:
    try:
        with os.scandir(path) as entries:
            return next(entries, None) is not None
    except OSError as exc:
        raise BackupError(code) from exc


def _protect_empty_directory(path: Path, occupied_code: str, invalid_code: str) -> None:
    if _directory_occupied(path, invalid_code):
        raise BackupError(occupied_code)
    try:
        _harden_runtime_owner_only(path, 0o700)
    except (OSError, PermissionError) as exc:
        raise BackupError(invalid_code) from exc


def _resolve_roots(
    root: Path, state_root: Path, staging_parent: Path
) -> tuple[Path, Path, Path]:
    vault = _resolve_directory(root, "backup_path_invalid")
    state = _resolve_directory(state_root, "backup_path_invalid")
    staging = _resolve_staging(staging_parent)
    _validate_root_locations(vault, state, staging)
    _protect_empty_directory(staging, "staging_not_empty", "staging_not_protected")
    return vault, state, staging


def _acquire_backup_owner(state_root: Path) -> tuple[OwnershipRegistry, OwnerLease]:
    coordinator = state_root / "run" / "markdown-transactions-v3.sqlite3"
    try:
        registry = OwnershipRegistry._from_adopted_database(state_root, coordinator)
        lease = registry.acquire(
            "runtime-deletion-check",
            scope="backup-snapshot",
            actor_id="private-vault-backup",
        )
    except OperationalOwnershipError as exc:
        code = (
            "backup_requires_quiescence"
            if exc.code == "runtime_deletion_check_requires_quiescence"
            else "backup_owner_unavailable"
        )
        raise BackupError(code) from exc
    except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
        raise BackupError("reliability_v3_required") from exc
    return registry, lease


def _release_backup_owner(
    registry: OwnershipRegistry, lease: OwnerLease, body_error: BaseException | None
) -> None:
    try:
        registry.release(lease)
    except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
        if body_error is None:
            raise BackupError("backup_owner_release_failed") from exc


def _clear_staging(image: Path) -> None:
    try:
        for child in tuple(image.iterdir()):
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        fsync_directory(image)
    except OSError as exc:
        raise BackupError("staging_cleanup_failed") from exc


def _require_runtime_valid(
    *,
    root: Path,
    state_root: Path,
    now: datetime,
    deadline: float,
    excluded_owner: OwnerLease | None,
    code: str,
) -> None:
    findings = validate_reliability_v3_runtime(
        root=root,
        state_root=state_root,
        now=now,
        deadline=deadline,
        excluded_owner=excluded_owner,
    )
    invalid = tuple(sorted(set(findings) - _ALLOWED_RUNTIME_FINDINGS))
    if invalid:
        raise BackupError(code, invalid)


def _source_entries(
    root: Path, state_root: Path, deadline: float
) -> tuple[tuple[_Entry, ...], tuple[_Entry, ...]]:
    vault_entries = _scan_tree(
        root,
        prefix="vault",
        deadline=deadline,
        excluded_top_level=frozenset({"cache", "logs", "run"}),
    )
    runtime_entries = _scan_tree(
        state_root / "run",
        prefix="state/run",
        deadline=deadline,
        excluded_files=frozenset(_ACTIVE_DATABASES) | _DATABASE_SIDECARS,
    )
    return vault_entries, runtime_entries


def _create_image_structure(image: Path) -> None:
    directories = (image / "vault", image / "state", image / "state/run")
    for directory in directories:
        directory.mkdir()
        _harden_runtime_owner_only(directory, 0o700)


def _copy_active_databases(
    state_root: Path, image: Path, lease: OwnerLease, deadline: float
) -> None:
    run_source = state_root / "run"
    run_destination = image / "state/run"
    for name in _ACTIVE_DATABASES:
        _sqlite_online_backup(run_source / name, run_destination / name, deadline)
    _remove_snapshot_owner(run_destination / _ACTIVE_DATABASES[0], lease)


def _confirm_sources_unchanged(
    *,
    root: Path,
    state_root: Path,
    expected: tuple[tuple[_Entry, ...], tuple[_Entry, ...]],
    deadline: float,
) -> None:
    if _source_entries(root, state_root, deadline) != expected:
        raise BackupError("source_changed")


def _entry_manifest(entry: _Entry) -> dict[str, object]:
    value = entry.manifest()
    value["path"] = entry.path.lstrip("/")
    return value


def _write_image_manifest(
    *,
    image: Path,
    root: Path,
    state_root: Path,
    now: datetime,
    deadline: float,
) -> None:
    created_at = now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    manifest = {
        "schema_version": "private-vault-backup/v1",
        "created_at": created_at,
        "source_roots": {
            "vault_sha256": sha256_bytes(str(root).encode("utf-8")),
            "state_sha256": sha256_bytes(str(state_root).encode("utf-8")),
        },
        "entries": [
            _entry_manifest(entry)
            for entry in _scan_tree(image, prefix="", deadline=deadline)
        ],
        "databases": _database_manifest(image),
    }
    manifest_path = image / "manifest.json"
    with manifest_path.open("xb") as stream:
        stream.write(canonical_json_bytes(manifest))
        stream.flush()
        os.fsync(stream.fileno())
    _harden_runtime_owner_only(manifest_path, 0o600)
    fsync_directory(image)


def _build_image(
    *,
    root: Path,
    state_root: Path,
    image: Path,
    lease: OwnerLease,
    now: datetime,
    deadline: float,
) -> None:
    _require_runtime_valid(
        root=root,
        state_root=state_root,
        now=now,
        deadline=deadline,
        excluded_owner=lease,
        code="runtime_state_invalid",
    )
    vault_entries, runtime_entries = _source_entries(root, state_root, deadline)
    _create_image_structure(image)
    _copy_entries(vault_entries, image, deadline)
    _copy_entries(runtime_entries, image, deadline)
    _copy_active_databases(state_root, image, lease, deadline)
    _confirm_sources_unchanged(
        root=root,
        state_root=state_root,
        expected=(vault_entries, runtime_entries),
        deadline=deadline,
    )
    _write_image_manifest(
        image=image,
        root=root,
        state_root=state_root,
        now=now,
        deadline=deadline,
    )
    validate_backup_image(image)


@contextlib.contextmanager
def _held_backup_owner(state_root: Path) -> Iterator[tuple[OwnershipRegistry, OwnerLease]]:
    registry, lease = _acquire_backup_owner(state_root)
    body_error: BaseException | None = None
    try:
        yield registry, lease
    except BaseException as exc:
        body_error = exc
        raise
    finally:
        _release_backup_owner(registry, lease, body_error)


def _prepare_backup_image(
    *,
    root: Path,
    state_root: Path,
    image: Path,
    now: datetime,
    deadline: float,
) -> None:
    with _held_backup_owner(state_root) as (registry, lease):
        with _heartbeat(registry, lease):
            _build_image(
                root=root,
                state_root=state_root,
                image=image,
                lease=lease,
                now=now,
                deadline=deadline,
            )


@contextlib.contextmanager
def staged_backup_image(
    *,
    root: Path,
    state_root: Path,
    staging_parent: Path,
    now: datetime | None = None,
    deadline: float = float("inf"),
) -> Iterator[Path]:
    """Yield one validated image and remove its plaintext bytes on exit."""
    vault, state, parent = _resolve_roots(root, state_root, staging_parent)
    _deadline(deadline)
    try:
        _prepare_backup_image(
            root=vault,
            state_root=state,
            image=parent,
            now=now or datetime.now(timezone.utc),
            deadline=deadline,
        )
        yield parent
    finally:
        _clear_staging(parent)


def _require_regular_file(path: Path, maximum: int, code: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise BackupError(code) from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise BackupError(code)
    if metadata.st_size > maximum:
        raise BackupError(code)


def _read_manifest(root: Path) -> tuple[bytes, dict[str, object]]:
    manifest_path = root / "manifest.json"
    _require_regular_file(manifest_path, 16 * 1024 * 1024, "manifest_invalid")
    try:
        raw = manifest_path.read_bytes()
        manifest = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupError("manifest_invalid") from exc
    if not isinstance(manifest, dict):
        raise BackupError("manifest_invalid")
    return raw, manifest


def _validate_manifest_envelope(raw: bytes, manifest: dict[str, object]) -> list[object]:
    if raw != canonical_json_bytes(manifest):
        raise BackupError("manifest_invalid")
    if set(manifest) != {
        "schema_version",
        "created_at",
        "source_roots",
        "entries",
        "databases",
    }:
        raise BackupError("manifest_invalid")
    if manifest["schema_version"] != "private-vault-backup/v1":
        raise BackupError("manifest_invalid")
    entries = manifest["entries"]
    if not isinstance(entries, list):
        raise BackupError("manifest_invalid")
    return entries


def _actual_manifest_entries(root: Path) -> list[dict[str, object]]:
    actual = []
    for entry in _scan_tree(root, prefix="", deadline=float("inf")):
        if entry.path.lstrip("/") == "manifest.json":
            continue
        actual.append(_entry_manifest(entry))
    return actual


def validate_backup_image(image: Path) -> dict[str, int]:
    """Validate manifest membership, hashes, database schemas, and integrity."""
    root = Path(image).resolve(strict=True)
    raw, manifest = _read_manifest(root)
    expected = _validate_manifest_envelope(raw, manifest)
    actual = _actual_manifest_entries(root)
    if actual != expected:
        raise BackupError("manifest_content_mismatch")
    databases = _database_manifest(root)
    if databases != manifest["databases"]:
        raise BackupError("manifest_database_mismatch")
    return {"entry_count": len(actual), "database_count": len(databases)}


def _resolve_no_symlink(path: Path, code: str) -> Path:
    try:
        if path.is_symlink():
            raise BackupError(code)
        return path.resolve(strict=True)
    except BackupError:
        raise
    except OSError as exc:
        raise BackupError(code) from exc


def _resolve_external_file(path: Path, maximum: int, code: str) -> Path:
    selected = Path(path)
    if not selected.is_absolute():
        raise BackupError(code)
    resolved = _resolve_no_symlink(selected, code)
    _require_regular_file(resolved, maximum, code)
    return resolved


def _read_external_file(path: Path, maximum: int, code: str) -> bytes:
    try:
        with path.open("rb") as stream:
            content = stream.read(maximum + 1)
    except OSError as exc:
        raise BackupError(code) from exc
    if len(content) > maximum:
        raise BackupError(code)
    return content


def _external_file(
    path: Path,
    *,
    maximum: int,
    code: str,
    read_content: bool = True,
) -> tuple[Path, bytes]:
    resolved = _resolve_external_file(path, maximum, code)
    if not read_content:
        return resolved, b""
    return resolved, _read_external_file(resolved, maximum, code)


def _remote_repository(value: str) -> re.Match[str] | None:
    return re.match(
        r"^(sftp|rest|s3|swift|b2|azure|gs|rclone):",
        value,
        flags=re.IGNORECASE,
    )


def _validate_remote_repository(value: str, remote: re.Match[str]) -> None:
    from urllib.parse import urlsplit

    nested = value[len(remote.group(0)) :]
    if "://" in nested:
        parsed = urlsplit(nested)
    else:
        parsed = urlsplit(value)
    if parsed.password is not None:
        raise BackupError("repository_file_contains_credentials")


def _overlaps_any(path: Path, sources: set[Path]) -> bool:
    for source in sources:
        if _paths_overlap(path, source):
            return True
    return False


def _require_local_repository(path: Path) -> None:
    try:
        if path.is_symlink():
            raise BackupError("repository_file_invalid")
        if not path.is_dir():
            raise BackupError("repository_file_invalid")
    except OSError as exc:
        raise BackupError("repository_file_invalid") from exc


def _repository_location(value: str, sources: set[Path]) -> None:
    remote = _remote_repository(value)
    if remote is not None:
        _validate_remote_repository(value, remote)
        return
    local = Path(value)
    if not local.is_absolute():
        raise BackupError("repository_file_invalid")
    resolved = local.resolve(strict=False)
    if _overlaps_any(resolved, sources):
        raise BackupError("repository_overlaps_source")
    _require_local_repository(local)


def _repository_input(repository_file: Path, sources: set[Path]) -> Path:
    repository, raw = _external_file(
        repository_file,
        maximum=_MAX_REPOSITORY_FILE_BYTES,
        code="repository_file_invalid",
    )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BackupError("repository_file_invalid") from exc
    lines = text.splitlines()
    if len(lines) != 1 or not lines[0] or "\x00" in lines[0]:
        raise BackupError("repository_file_invalid")
    _repository_location(lines[0], sources)
    return repository


def _restic_inputs(
    *,
    root: Path,
    state_root: Path,
    staging_parent: Path,
    restic_binary: Path,
    repository_file: Path,
) -> tuple[Path, Path]:
    binary, _binary_content = _external_file(
        restic_binary,
        maximum=256 * 1024 * 1024,
        code="restic_binary_invalid",
        read_content=False,
    )
    sources = {
        Path(root).resolve(strict=True),
        Path(state_root).resolve(strict=True),
        Path(staging_parent).resolve(strict=True),
    }
    repository = _repository_input(repository_file, sources)
    for external in (binary, repository):
        if _overlaps_any(external, sources):
            raise BackupError("backup_external_path_overlap")
    return binary, repository


def _require_restic_version(binary: Path, *, cwd: Path, deadline: float) -> None:
    result = _run_bounded([str(binary), "version"], cwd=cwd, deadline=deadline)
    if result.returncode != 0 or re.match(
        rf"^restic {re.escape(_RESTIC_VERSION)}(?:\s|$)",
        result.stdout.decode("ascii", errors="ignore"),
    ) is None:
        raise BackupError("restic_version_mismatch")


def _json_messages(output: bytes, code: str) -> list[dict[str, object]]:
    messages = []
    try:
        for line in output.splitlines():
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("restic output entry is not an object")
            messages.append(value)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise BackupError(code) from exc
    return messages


def _single_summary(output: bytes, code: str) -> dict[str, object]:
    summaries = [
        value
        for value in _json_messages(output, code)
        if value.get("message_type") == "summary"
    ]
    if len(summaries) != 1:
        raise BackupError(code)
    return summaries[0]


def _snapshot_id(output: bytes) -> str:
    snapshot = _single_summary(output, "restic_output_invalid").get("snapshot_id")
    if not isinstance(snapshot, str):
        raise BackupError("restic_output_invalid")
    if re.fullmatch(r"[0-9a-f]{64}", snapshot) is None:
        raise BackupError("restic_output_invalid")
    return snapshot


def backup_private_vault(
    *,
    root: Path,
    state_root: Path,
    staging_parent: Path,
    restic_binary: Path,
    repository_file: Path,
    now: datetime | None = None,
    deadline: float = float("inf"),
) -> dict[str, str]:
    """Build, encrypt, and repository-check one coherent private-vault snapshot."""
    binary, repository = _restic_inputs(
        root=root,
        state_root=state_root,
        staging_parent=staging_parent,
        restic_binary=restic_binary,
        repository_file=repository_file,
    )
    staging = Path(staging_parent).resolve(strict=True)
    _require_restic_version(binary, cwd=staging, deadline=deadline)
    with staged_backup_image(
        root=root,
        state_root=state_root,
        staging_parent=staging,
        now=now,
        deadline=deadline,
    ) as image:
        manifest_path = image / "manifest.json"
        manifest_bytes = manifest_path.read_bytes()
        manifest_sha256 = sha256_bytes(manifest_bytes)
        manifest = json.loads(manifest_bytes)
        base = [str(binary), "--repository-file", str(repository)]
        result = _run_bounded(
            base
            + [
                "backup",
                "--json",
                "--tag",
                "llm-wiki-private-v1",
                ".",
            ],
            cwd=image,
            deadline=deadline,
        )
        if result.returncode == 3:
            raise BackupError("restic_backup_incomplete")
        if result.returncode != 0:
            raise BackupError("restic_backup_failed")
        snapshot = _snapshot_id(result.stdout)
        validate_backup_image(image)
        if sha256_bytes(manifest_path.read_bytes()) != manifest_sha256:
            raise BackupError("staging_changed_during_backup")
        checked = _run_bounded(base + ["check"], cwd=image, deadline=deadline)
        if checked.returncode != 0:
            raise BackupError("restic_check_failed")
        return {
            "schema_version": "private-vault-backup-receipt/v1",
            "snapshot_id": snapshot,
            "created_at": str(manifest["created_at"]),
            "manifest_sha256": manifest_sha256,
        }


def _restore_target(target: Path) -> Path:
    selected = Path(target)
    if not selected.is_absolute():
        raise BackupError("restore_target_invalid")
    resolved = _resolve_no_symlink(selected, "restore_target_invalid")
    if not resolved.is_dir():
        raise BackupError("restore_target_invalid")
    _protect_empty_directory(
        resolved, "restore_target_not_empty", "restore_target_invalid"
    )
    return resolved


def _restore_inputs(
    *, target: Path, restic_binary: Path, repository_file: Path
) -> tuple[Path, Path, Path]:
    restored = _restore_target(target)
    binary, _binary_content = _external_file(
        restic_binary,
        maximum=256 * 1024 * 1024,
        code="restic_binary_invalid",
        read_content=False,
    )
    repository = _repository_input(repository_file, {restored})
    for external in (binary, repository):
        if _paths_overlap(external, restored):
            raise BackupError("restore_external_path_overlap")
    return restored, binary, repository


def _validate_restore_counter(summary: dict[str, object], key: str) -> None:
    value = summary.get(key)
    if type(value) is not int:
        raise BackupError("restic_restore_output_invalid")
    if value < 0:
        raise BackupError("restic_restore_output_invalid")


def _validate_restore_counters(summary: dict[str, object]) -> None:
    for key in (
        "total_files",
        "files_restored",
        "files_skipped",
        "files_deleted",
        "total_bytes",
        "bytes_restored",
        "bytes_skipped",
    ):
        _validate_restore_counter(summary, key)


def _require_complete_restore(summary: dict[str, object]) -> None:
    if summary["files_skipped"] != 0:
        raise BackupError("restic_restore_incomplete")
    if summary["files_deleted"] != 0:
        raise BackupError("restic_restore_incomplete")
    if summary["bytes_skipped"] != 0:
        raise BackupError("restic_restore_incomplete")


def _restore_summary(output: bytes) -> None:
    summary = _single_summary(output, "restic_restore_output_invalid")
    _validate_restore_counters(summary)
    _require_complete_restore(summary)


def _validate_restore_receipt(snapshot_id: str, manifest_sha256: str) -> None:
    if re.fullmatch(r"[0-9a-f]{64}", snapshot_id) is None:
        raise BackupError("restore_receipt_invalid")
    if re.fullmatch(r"[0-9a-f]{64}", manifest_sha256) is None:
        raise BackupError("restore_receipt_invalid")


def _check_restic_repository(
    base: list[str], *, cwd: Path, deadline: float
) -> None:
    checked = _run_bounded(base + ["check"], cwd=cwd, deadline=deadline)
    if checked.returncode != 0:
        raise BackupError("restic_check_failed")


def _run_restore(
    *,
    base: list[str],
    target: Path,
    snapshot_id: str,
    deadline: float,
) -> None:
    result = _run_bounded(
        base
        + [
            "restore",
            snapshot_id,
            "--json",
            "--target",
            str(target),
        ],
        cwd=target,
        deadline=deadline,
    )
    if result.returncode != 0:
        raise BackupError("restic_restore_failed")
    _restore_summary(result.stdout)


def _validate_restored_image(
    target: Path, expected_manifest_sha256: str, deadline: float
) -> tuple[dict[str, int], dict[str, object]]:
    _harden_runtime_owner_only(target, 0o700)
    manifest_bytes, manifest = _read_manifest(target)
    if sha256_bytes(manifest_bytes) != expected_manifest_sha256:
        raise BackupError("restore_manifest_mismatch")
    validation = validate_backup_image(target)
    _require_runtime_valid(
        root=target / "vault",
        state_root=target / "state",
        now=datetime.now(timezone.utc),
        deadline=deadline,
        excluded_owner=None,
        code="restore_runtime_invalid",
    )
    return validation, manifest


def _restore_receipt(
    *,
    snapshot_id: str,
    manifest_sha256: str,
    validation: dict[str, int],
    manifest: dict[str, object],
) -> dict[str, object]:
    return {
        "schema_version": "private-vault-restore-receipt/v1",
        "snapshot_id": snapshot_id,
        "created_at": str(manifest["created_at"]),
        "manifest_sha256": manifest_sha256,
        **validation,
    }


def restore_private_vault(
    *,
    target: Path,
    restic_binary: Path,
    repository_file: Path,
    snapshot_id: str,
    expected_manifest_sha256: str,
    deadline: float = float("inf"),
) -> dict[str, object]:
    """Restore one receipt-bound snapshot to a clean target and validate it."""
    _validate_restore_receipt(snapshot_id, expected_manifest_sha256)
    restored, binary, repository = _restore_inputs(
        target=target,
        restic_binary=restic_binary,
        repository_file=repository_file,
    )
    _require_restic_version(binary, cwd=restored, deadline=deadline)
    base = [str(binary), "--repository-file", str(repository)]
    _check_restic_repository(base, cwd=restored, deadline=deadline)
    try:
        _run_restore(
            base=base,
            target=restored,
            snapshot_id=snapshot_id,
            deadline=deadline,
        )
        validation, manifest = _validate_restored_image(
            restored,
            expected_manifest_sha256,
            deadline=deadline,
        )
        return _restore_receipt(
            snapshot_id=snapshot_id,
            manifest_sha256=expected_manifest_sha256,
            validation=validation,
            manifest=manifest,
        )
    except BaseException:
        _clear_staging(restored)
        raise


def _timeout_seconds(value: str) -> float:
    try:
        timeout = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be a number") from exc
    if timeout <= 0:
        raise argparse.ArgumentTypeError("timeout must be positive")
    if timeout > 24 * 60 * 60:
        raise argparse.ArgumentTypeError("timeout must not exceed 86400 seconds")
    return timeout


def _add_restic_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--restic-binary", type=Path, required=True)
    parser.add_argument("--repository-file", type=Path, required=True)
    parser.add_argument(
        "--timeout-seconds",
        type=_timeout_seconds,
        default=3600.0,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup", help="create one encrypted snapshot")
    backup.add_argument("--staging", type=Path, required=True)
    _add_restic_arguments(backup)
    restore = commands.add_parser("restore", help="restore and validate one snapshot")
    restore.add_argument("--target", type=Path, required=True)
    restore.add_argument("--snapshot-id", required=True)
    restore.add_argument("--manifest-sha256", required=True)
    _add_restic_arguments(restore)
    return parser.parse_args(argv)


def _runtime_roots() -> tuple[Path, Path]:
    configured = os.environ.get("LLM_WIKI_ROOT")
    default = Path(__file__).resolve().parent.parent
    root = Path(configured) if configured else default
    state_root = Path(os.environ.get("LLM_WIKI_STATE_ROOT", root))
    try:
        return root.resolve(strict=True), state_root.resolve(strict=True)
    except OSError as exc:
        raise BackupError("runtime_root_invalid") from exc


def _run_cli_command(
    args: argparse.Namespace, root: Path, state_root: Path, deadline: float
) -> dict[str, object]:
    if args.command == "backup":
        return backup_private_vault(
            root=root,
            state_root=state_root,
            staging_parent=args.staging,
            restic_binary=args.restic_binary,
            repository_file=args.repository_file,
            deadline=deadline,
        )
    return restore_private_vault(
        target=args.target,
        restic_binary=args.restic_binary,
        repository_file=args.repository_file,
        snapshot_id=args.snapshot_id,
        expected_manifest_sha256=args.manifest_sha256,
        deadline=deadline,
    )


def _safe_error_details(details: tuple[str, ...]) -> list[str]:
    safe = []
    for detail in details:
        if re.fullmatch(r"[a-z0-9_:-]{1,128}", detail):
            safe.append(detail)
    return safe


def _print_json(value: dict[str, object]) -> None:
    print(canonical_json_bytes(value).decode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    deadline = time.monotonic() + args.timeout_seconds
    try:
        root, state_root = _runtime_roots()
        receipt = _run_cli_command(args, root, state_root, deadline)
    except BackupError as exc:
        _print_json(
            {"ok": False, "code": exc.code, "details": _safe_error_details(exc.details)}
        )
        return 2
    except TimeoutError:
        _print_json({"ok": False, "code": "backup_timeout", "details": []})
        return 2
    _print_json(receipt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
