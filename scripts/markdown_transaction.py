"""Recoverable, hash-checked transactions for authoritative Markdown files."""

from __future__ import annotations

import argparse
import contextlib
import copy
import ctypes
import getpass
import json
import os
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from reliable_memory import (
    DEFAULTS,
    _set_owner_only,
    begin_immediate,
    canonical_json_bytes,
    fsync_directory,
    fsync_file,
    open_operational_db,
    restricted_relative_path,
    sha256_bytes,
    validate_schema,
    validate_state_root,
)

ChangeKind = Literal["create", "replace", "delete"]
Validator = Callable[[Mapping[str, object]], object]
ABSENT = "absent"
_ALLOWED_DIRECTORIES = (
    "knowledge/daily",
    "knowledge/notes",
    "knowledge/projects",
    "knowledge/inbox/claims",
)
_ALLOWED_FILES = {"knowledge/index.md", "knowledge/log.md"}
_SCHEMA = Path(__file__).with_name("schemas") / "markdown-transaction-v1.json"
_WRITER_LEASE_SECONDS = 2.0
_WRITER_HEARTBEAT_SECONDS = 0.5
_WRITER_WAIT_SECONDS = DEFAULTS.markdown_busy_ms / 1_000


@dataclass(frozen=True)
class MarkdownChange:
    kind: ChangeKind
    path: str
    content: bytes | None

    @classmethod
    def create(cls, path: str, content: bytes) -> MarkdownChange:
        return cls("create", path, _require_bytes(content))

    @classmethod
    def replace(cls, path: str, content: bytes) -> MarkdownChange:
        return cls("replace", path, _require_bytes(content))

    @classmethod
    def delete(cls, path: str) -> MarkdownChange:
        return cls("delete", path, None)


@dataclass(frozen=True)
class MarkdownOperation:
    kind: ChangeKind
    path: str
    before_hash: str
    after_hash: str


@dataclass(frozen=True)
class TransactionRecord:
    id: str
    operation_id: str
    state: str
    operations: tuple[MarkdownOperation, ...]
    preconditions: Mapping[str, object]
    created_at: str
    updated_at: str
    parent_transaction_id: str | None = None
    error_code: str | None = None


class TransactionFailure(RuntimeError):
    """An apply failure with a stable machine-readable disposition."""

    def __init__(self, message: str, code: str, state: str):
        super().__init__(message)
        self.code = code
        self.state = state


def _require_bytes(content: bytes) -> bytes:
    if not isinstance(content, bytes):
        raise TypeError("Markdown content must be bytes")
    return content


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _future_timestamp(seconds: float) -> str:
    value = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return value.isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        process_query_limited_information = 0x1000
        still_active = 259
        handle = ctypes.windll.kernel32.OpenProcess(
            process_query_limited_information, False, pid
        )
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == still_active
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except (OSError, OverflowError, ValueError):
        return False
    return True


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
    except FileNotFoundError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _use_posix_dir_fd() -> bool:
    return os.name == "posix"


def _windows_acl_identity() -> str:
    username = os.environ.get("USERNAME") or getpass.getuser()
    domain = os.environ.get("USERDOMAIN")
    return f"{domain}\\{username}" if domain else username


def _run_acl_command(command: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        timeout=5,
    )


def _harden_windows_acl(path: Path) -> None:
    identity = _windows_acl_identity()
    permission = f"{identity}:(OI)(CI)(F)" if path.is_dir() else f"{identity}:(F)"
    try:
        changed = _run_acl_command(
            ["icacls", str(path), "/inheritance:r", "/grant:r", permission]
        )
        verified = _run_acl_command(["icacls", str(path)]) if changed.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PermissionError(f"could not apply owner-only ACL to {path}: {exc}") from exc
    if changed.returncode != 0 or verified is None or verified.returncode != 0:
        detail = _acl_output_text(changed.stderr).strip() or "icacls failed"
        raise PermissionError(f"could not apply owner-only ACL to {path}: {detail}")
    output = _acl_output_text(verified.stdout)
    acl_lines = [line.strip() for line in output.splitlines() if ":(" in line]
    owner_lines = [line for line in acl_lines if identity.casefold() in line.casefold()]
    if (
        len(owner_lines) != 1
        or "(F)" not in owner_lines[0]
        or "(I)" in owner_lines[0]
        or any(identity.casefold() not in line.casefold() for line in acl_lines)
    ):
        raise PermissionError(f"owner-only ACL verification failed for {path}")


def _acl_output_text(value: bytes | str | None) -> str:
    if isinstance(value, bytes):
        return value.decode("ascii", errors="ignore")
    return value or ""


def _harden_owner_only(path: Path, mode: int) -> None:
    if os.name == "nt":
        _harden_windows_acl(path)
    else:
        _set_owner_only(path, mode)


class _WindowsFileInformation(ctypes.Structure):
    _fields_ = [
        ("file_attributes", wintypes.DWORD),
        ("creation_time", wintypes.FILETIME),
        ("last_access_time", wintypes.FILETIME),
        ("last_write_time", wintypes.FILETIME),
        ("volume_serial_number", wintypes.DWORD),
        ("file_size_high", wintypes.DWORD),
        ("file_size_low", wintypes.DWORD),
        ("number_of_links", wintypes.DWORD),
        ("file_index_high", wintypes.DWORD),
        ("file_index_low", wintypes.DWORD),
    ]


class _WindowsRenameInformation(ctypes.Structure):
    _fields_ = [
        ("flags", wintypes.DWORD),
        ("root_directory", wintypes.HANDLE),
        ("file_name_length", wintypes.DWORD),
        ("file_name", wintypes.WCHAR * 1),
    ]


class _WindowsDispositionInformation(ctypes.Structure):
    _fields_ = [("delete_file", wintypes.BOOL)]


class _WindowsIoStatusBlock(ctypes.Structure):
    _fields_ = [("status", ctypes.c_ssize_t), ("information", ctypes.c_size_t)]


def _open_windows_directory(path: Path) -> int:
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    kernel32.CreateFileW.restype = wintypes.HANDLE
    file_read_attributes = 0x80
    file_share_read = 0x1
    file_share_write = 0x2
    open_existing = 3
    file_flag_backup_semantics = 0x02000000
    file_flag_open_reparse_point = 0x00200000
    handle = kernel32.CreateFileW(
        str(path),
        file_read_attributes,
        file_share_read | file_share_write,
        None,
        open_existing,
        file_flag_backup_semantics | file_flag_open_reparse_point,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        raise OSError(ctypes.get_last_error(), f"cannot lock Windows directory: {path}")
    information = _WindowsFileInformation()
    if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(information)):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(handle)
        raise OSError(error, f"cannot identify Windows directory: {path}")
    if information.file_attributes & 0x400:
        kernel32.CloseHandle(handle)
        raise RuntimeError(f"Windows directory handle resolves to a reparse point: {path}")
    return handle


def _close_windows_handle(handle: int) -> None:
    if not ctypes.windll.kernel32.CloseHandle(handle):
        raise OSError(ctypes.get_last_error(), "cannot close Windows directory handle")


def _open_windows_file_for_mutation(path: Path) -> int:
    kernel32 = ctypes.windll.kernel32
    generic_read = 0x80000000
    delete_access = 0x00010000
    share_read = 0x1
    share_delete = 0x4
    open_existing = 3
    open_reparse_point = 0x00200000
    handle = kernel32.CreateFileW(
        str(path),
        generic_read | delete_access,
        share_read | share_delete,
        None,
        open_existing,
        open_reparse_point,
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        raise OSError(ctypes.get_last_error(), f"cannot open Windows mutation handle: {path}")
    return handle


def _rename_windows_handle(
    file_handle: int, parent_handle: int, target_name: str, *, replace: bool
) -> None:
    encoded_name = target_name.encode("utf-16-le")
    name_offset = _WindowsRenameInformation.file_name.offset
    buffer = ctypes.create_string_buffer(ctypes.sizeof(_WindowsRenameInformation) + len(encoded_name))
    information = _WindowsRenameInformation.from_buffer(buffer)
    information.flags = int(replace)
    information.root_directory = parent_handle
    information.file_name_length = len(encoded_name)
    ctypes.memmove(ctypes.addressof(buffer) + name_offset, encoded_name, len(encoded_name))
    file_rename_information = 10
    io_status = _WindowsIoStatusBlock()
    ntdll = ctypes.windll.ntdll
    ntdll.NtSetInformationFile.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(_WindowsIoStatusBlock),
        wintypes.LPVOID,
        wintypes.ULONG,
        ctypes.c_int,
    )
    ntdll.NtSetInformationFile.restype = ctypes.c_long
    status = ntdll.NtSetInformationFile(
        file_handle,
        ctypes.byref(io_status),
        buffer,
        len(buffer),
        file_rename_information,
    )
    if status < 0:
        error = ntdll.RtlNtStatusToDosError(status)
        raise OSError(error, f"cannot publish Windows target: {target_name}")


def _delete_windows_handle(file_handle: int) -> None:
    information = _WindowsDispositionInformation(True)
    file_disposition_info = 4
    kernel32 = ctypes.windll.kernel32
    if not kernel32.SetFileInformationByHandle(
        file_handle,
        file_disposition_info,
        ctypes.byref(information),
        ctypes.sizeof(information),
    ):
        raise OSError(kernel32.GetLastError(), "cannot delete Windows target")


class MarkdownCoordinator:
    """Prepare and apply durable multi-file Markdown transactions."""

    def __init__(self, vault: Path, state_root: Path):
        self.vault = Path(vault).resolve(strict=True)
        if not self.vault.is_dir():
            raise ValueError(f"vault is not a directory: {self.vault}")
        self.state_root = Path(state_root)
        validate_state_root(self.state_root)
        self.run_root = self.state_root / "run"
        self.transaction_root = self.run_root / "transactions"
        self.run_root.mkdir(parents=True, exist_ok=True)
        self.transaction_root.mkdir(parents=True, exist_ok=True)
        _set_owner_only(self.run_root, 0o700)
        _set_owner_only(self.transaction_root, 0o700)
        self.database_path = self.run_root / "markdown-transactions.sqlite3"
        self._local = threading.local()
        self._initialize_database()

    def _connect(self) -> sqlite3.Connection:
        database = open_operational_db(
            self.database_path, busy_ms=DEFAULTS.markdown_busy_ms
        )
        schema = database.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'transaction'"
        ).fetchone()
        if schema is not None and "conflicted" not in (schema["sql"] or ""):
            # Stage 2 Task 2 shipped a narrower state constraint. Preserve those
            # databases while allowing their rows to enter recovery-only states.
            database.execute("PRAGMA ignore_check_constraints = ON")
        return database

    def _initialize_database(self) -> None:
        with self._connect() as database:
            with begin_immediate(database):
                database.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS "transaction" (
                        id TEXT PRIMARY KEY,
                        operation_id TEXT NOT NULL UNIQUE,
                        request_hash TEXT NOT NULL,
                        state TEXT NOT NULL CHECK (state IN (
                            'preparing', 'prepared', 'applying', 'committed',
                            'discarded', 'conflicted', 'quarantined'
                        )),
                        preconditions_json TEXT NOT NULL,
                        plan_hash TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        parent_transaction_id TEXT,
                        error_code TEXT,
                        artifacts_pruned_at TEXT,
                        owner_pid INTEGER
                    );
                    CREATE TABLE IF NOT EXISTS "operation" (
                        transaction_id TEXT NOT NULL REFERENCES "transaction"(id) ON DELETE CASCADE,
                        position INTEGER NOT NULL,
                        kind TEXT NOT NULL CHECK (kind IN ('create', 'replace', 'delete')),
                        path TEXT NOT NULL,
                        before_hash TEXT NOT NULL,
                        after_hash TEXT NOT NULL,
                        parent_device INTEGER NOT NULL,
                        parent_inode INTEGER NOT NULL,
                        applied INTEGER NOT NULL DEFAULT 0 CHECK (applied IN (0, 1)),
                        PRIMARY KEY (transaction_id, position),
                        UNIQUE (transaction_id, path)
                    );
                    CREATE TABLE IF NOT EXISTS project_leases (
                        project TEXT PRIMARY KEY,
                        lease_token TEXT NOT NULL,
                        fencing_epoch INTEGER NOT NULL,
                        owner TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        heartbeat_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS writer_owners (
                        gate_name TEXT PRIMARY KEY,
                        owner_token TEXT NOT NULL,
                        process_id INTEGER NOT NULL,
                        thread_id INTEGER NOT NULL,
                        acquired_at TEXT NOT NULL,
                        heartbeat_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        fencing_epoch INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS writer_fences (
                        gate_name TEXT PRIMARY KEY,
                        last_epoch INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS maintenance_owners (
                        owner_name TEXT PRIMARY KEY,
                        owner_token TEXT NOT NULL,
                        process_id INTEGER NOT NULL,
                        acquired_at TEXT NOT NULL
                    );
                    """
                )
                columns = {
                    row["name"] for row in database.execute("PRAGMA table_info(writer_owners)")
                }
                for name, declaration in (
                    ("heartbeat_at", "TEXT"),
                    ("expires_at", "TEXT"),
                    ("fencing_epoch", "INTEGER"),
                ):
                    if name not in columns:
                        database.execute(
                            f"ALTER TABLE writer_owners ADD COLUMN {name} {declaration}"
                        )
                operation_columns = {
                    row["name"] for row in database.execute('PRAGMA table_info("operation")')
                }
                for name in ("parent_device", "parent_inode"):
                    if name not in operation_columns:
                        database.execute(
                            f'ALTER TABLE "operation" ADD COLUMN {name} INTEGER'
                        )
                transaction_columns = {
                    row["name"]
                    for row in database.execute('PRAGMA table_info("transaction")')
                }
                for name in (
                    "parent_transaction_id",
                    "error_code",
                    "artifacts_pruned_at",
                ):
                    if name not in transaction_columns:
                        database.execute(
                            f'ALTER TABLE "transaction" ADD COLUMN {name} TEXT'
                        )
                if "owner_pid" not in transaction_columns:
                    database.execute(
                        'ALTER TABLE "transaction" ADD COLUMN owner_pid INTEGER'
                    )
                self._backfill_parent_identities(database)

    def _backfill_parent_identities(self, database: sqlite3.Connection) -> None:
        rows = database.execute(
            'SELECT operation.*, "transaction".state AS transaction_state '
            'FROM "operation" JOIN "transaction" '
            'ON "transaction".id = operation.transaction_id '
            "WHERE (operation.parent_device IS NULL OR operation.parent_inode IS NULL) "
            "AND \"transaction\".state IN ('prepared', 'applying')"
        ).fetchall()
        for row in rows:
            target = self._target(row["path"])
            content, identity = self._capture_target(target)
            current_hash = ABSENT if content is None else sha256_bytes(content)
            expected_hashes = {row["after_hash"]} if row["applied"] else {
                row["before_hash"],
                row["after_hash"],
            }
            if current_hash not in expected_hashes:
                raise RuntimeError(
                    f"cannot migrate parent identity with unknown target bytes: {row['path']}"
                )
            database.execute(
                'UPDATE "operation" SET parent_device = ?, parent_inode = ? '
                "WHERE transaction_id = ? AND position = ?",
                (identity[0], identity[1], row["transaction_id"], row["position"]),
            )

    def prepare(
        self,
        changes: Sequence[MarkdownChange],
        *,
        operation_id: str,
        preconditions: Mapping[str, object] | None = None,
        validators: Sequence[Validator] = (),
        _parent_transaction_id: str | None = None,
    ) -> TransactionRecord:
        if not isinstance(operation_id, str) or not operation_id:
            raise ValueError("operation_id must be a non-empty string")
        if not changes:
            raise ValueError("a transaction requires at least one change")
        normalized = tuple(self._validate_change(change) for change in changes)
        paths = [unicodedata.normalize("NFC", change.path).casefold() for change in normalized]
        if len(paths) != len(set(paths)):
            raise ValueError("duplicate transaction target")
        persisted_preconditions = self._validate_preconditions(preconditions or {})
        request_hash = self._request_hash(normalized, persisted_preconditions)
        self.recover()
        existing = self._record_for_operation_id(operation_id)
        if existing is not None:
            if self._request_hash_for_operation_id(operation_id) != request_hash:
                raise ValueError("operation_id is already bound to a different request")
            return existing

        transaction_id = uuid.uuid4().hex
        artifact_root = self.transaction_root / transaction_id
        before_root = artifact_root / "before"
        after_root = artifact_root / "after"
        try:
            artifact_root.mkdir(parents=True)
            _harden_owner_only(artifact_root, 0o700)
            before_root.mkdir()
            after_root.mkdir()
            if os.name != "nt":
                for directory in (before_root, after_root):
                    _harden_owner_only(directory, 0o700)
        except BaseException:
            self._remove_artifacts(artifact_root)
            raise

        timestamp = _now()
        try:
            with self._connect() as database, begin_immediate(database):
                database.execute(
                    'INSERT INTO "transaction" '
                    "(id, operation_id, request_hash, state, preconditions_json, plan_hash, "
                    "created_at, updated_at, parent_transaction_id, owner_pid) "
                    "VALUES (?, ?, ?, 'preparing', ?, '', ?, ?, ?, ?)",
                    (
                        transaction_id,
                        operation_id,
                        request_hash,
                        canonical_json_bytes(persisted_preconditions).decode("utf-8"),
                        timestamp,
                        timestamp,
                        _parent_transaction_id,
                        os.getpid(),
                    ),
                )
        except sqlite3.IntegrityError:
            self._remove_artifacts(artifact_root)
            existing = self._record_for_operation_id(operation_id)
            if existing is None or self._request_hash_for_operation_id(operation_id) != request_hash:
                raise ValueError("operation_id is already bound to a different request") from None
            return existing
        self._killpoint("after_preparing", _parent_transaction_id)

        operations: list[MarkdownOperation] = []
        parent_identities: list[tuple[int, int]] = []
        plan_operations: list[dict[str, object]] = []
        try:
            for position, change in enumerate(normalized):
                target = self._target(change.path)
                before, parent_identity = self._capture_target(target)
                if change.kind == "create" and before is not None:
                    raise FileExistsError(change.path)
                if change.kind in {"replace", "delete"} and before is None:
                    raise FileNotFoundError(change.path)
                after = change.content
                before_description = self._stage_state(before_root, position, before)
                after_description = self._stage_state(after_root, position, after)
                before_hash = ABSENT if before is None else sha256_bytes(before)
                after_hash = ABSENT if after is None else sha256_bytes(after)
                operations.append(
                    MarkdownOperation(change.kind, change.path, before_hash, after_hash)
                )
                parent_identities.append(parent_identity)
                plan_operations.append(
                    {
                        "kind": change.kind,
                        "path": change.path,
                        "before": before_description,
                        "after": after_description,
                    }
                )

            self._killpoint("after_images_fsynced", _parent_transaction_id)

            plan: dict[str, object] = {
                "schema_version": "markdown-transaction/v1",
                "transaction_id": transaction_id,
                "operations": plan_operations,
            }
            validate_schema(plan, _SCHEMA)
            for validator in validators:
                if not callable(validator):
                    raise TypeError("validators must be callable")
                result = validator(copy.deepcopy(plan))
                if result is False:
                    raise ValueError("transaction validator rejected the plan")
            validate_schema(plan, _SCHEMA)
            self._verify_plan_artifacts(plan, artifact_root)
            plan_bytes = canonical_json_bytes(plan)
            plan_path = artifact_root / "plan.json"
            self._write_new_file(plan_path, plan_bytes)
            manifest = {
                "schema_version": "markdown-transaction-recovery/v1",
                "transaction_id": transaction_id,
                "request_hash": request_hash,
                "plan_hash": sha256_bytes(plan_bytes),
                "operations": [
                    {
                        "position": position,
                        "before_hash": operation.before_hash,
                        "after_hash": operation.after_hash,
                        "parent_device": parent_identities[position][0],
                        "parent_inode": parent_identities[position][1],
                    }
                    for position, operation in enumerate(operations)
                ],
            }
            self._write_new_file(
                artifact_root / "manifest.json", canonical_json_bytes(manifest)
            )
            for directory in (before_root, after_root, artifact_root, self.transaction_root):
                fsync_directory(directory)
            self._killpoint("after_plan_fsynced", _parent_transaction_id)

            try:
                with self._connect() as database, begin_immediate(database):
                    database.execute(
                        'UPDATE "transaction" SET state = \'prepared\', plan_hash = ?, '
                        "updated_at = ?, owner_pid = NULL "
                        "WHERE id = ? AND state = 'preparing'",
                        (
                            sha256_bytes(plan_bytes),
                            timestamp,
                            transaction_id,
                        ),
                    )
                    database.executemany(
                        'INSERT INTO "operation" '
                        "(transaction_id, position, kind, path, before_hash, after_hash, "
                        "parent_device, parent_inode) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        [
                            (
                                transaction_id,
                                position,
                                operation.kind,
                                operation.path,
                                operation.before_hash,
                                operation.after_hash,
                                parent_identities[position][0],
                                parent_identities[position][1],
                            )
                            for position, operation in enumerate(operations)
                        ],
                    )
            except sqlite3.IntegrityError:
                raise RuntimeError("transaction operations could not be persisted") from None
            self._killpoint("after_prepared", _parent_transaction_id)
            return self._record(transaction_id)
        except BaseException:
            record = self._record_if_present(transaction_id)
            if record is None or record.state == "preparing":
                with self._connect() as database, begin_immediate(database):
                    database.execute(
                        'DELETE FROM "transaction" WHERE id = ? AND state = \'preparing\'',
                        (transaction_id,),
                    )
                self._remove_artifacts(artifact_root)
            raise

    def apply(self, transaction_id: str) -> TransactionRecord:
        with self.writer_gate():
            try:
                return self._apply_locked(transaction_id)
            except TransactionFailure as exc:
                if exc.code == "precondition_failed":
                    self._rollback_for_quarantine(transaction_id, exc.code)
                else:
                    self._set_transaction_state(
                        transaction_id, exc.state, error_code=exc.code
                    )
                raise
            except RuntimeError as exc:
                message = str(exc)
                if "after-image is corrupt" in message or "plan hash mismatch" in message:
                    recovered = self._recover_corrupt_after_image(transaction_id)
                    raise TransactionFailure(
                        message, "after_image_corrupt", recovered.state
                    ) from exc
                elif "after state mismatch" in message:
                    failure = TransactionFailure(
                        message, "unknown_target_bytes", "conflicted"
                    )
                elif "before state mismatch" in message:
                    failure = TransactionFailure(
                        message, "before_hash_mismatch", "conflicted"
                    )
                else:
                    raise
                self._set_transaction_state(
                    transaction_id, failure.state, error_code=failure.code
                )
                raise failure from exc

    def _apply_locked(self, transaction_id: str) -> TransactionRecord:
        record = self._record(transaction_id)
        if record.state == "committed":
            return record
        if record.state not in {"prepared", "applying"}:
            raise RuntimeError(f"transaction cannot be applied from state {record.state}")
        plan = self._load_verified_plan(record)
        rows = self._operation_rows(transaction_id)
        if "project_lease" in record.preconditions:
            self._check_preconditions(
                {"project_lease": record.preconditions["project_lease"]}, set()
            )
        reconciled_after = self._reconcile_operation_states(transaction_id, rows)
        self._check_preconditions(record.preconditions, reconciled_after)
        rows = self._operation_rows(transaction_id)
        with self._connect() as database, begin_immediate(database):
            database.execute(
                'UPDATE "transaction" SET state = \'applying\', updated_at = ? WHERE id = ?',
                (_now(), transaction_id),
            )
        self._killpoint("after_applying", record.parent_transaction_id)

        for row, operation_plan in zip(rows, plan["operations"], strict=True):
            self._check_preconditions(record.preconditions, reconciled_after)
            if row["applied"]:
                self._require_operation_state(row, row["after_hash"], "after state")
                reconciled_after.add(row["path"])
                continue
            self._mutate_and_mark(transaction_id, row, operation_plan)
            reconciled_after.add(row["path"])
            self._killpoint("after_each_target", record.parent_transaction_id)

        self._check_preconditions(record.preconditions, reconciled_after)
        for row in self._operation_rows(transaction_id):
            self._require_operation_state(row, row["after_hash"], "after state")
        self._killpoint("before_commit", record.parent_transaction_id)
        with self._connect() as database, begin_immediate(database):
            database.execute(
                'UPDATE "transaction" SET state = \'committed\', updated_at = ? WHERE id = ?',
                (_now(), transaction_id),
            )
        result = self._record(transaction_id)
        self._killpoint("after_commit", record.parent_transaction_id)
        return result

    def recover(self) -> list[TransactionRecord]:
        """Converge every incomplete transaction without overwriting unknown bytes."""
        recovered: list[TransactionRecord] = []
        with self.writer_gate():
            with self._connect() as database:
                rows = [
                    (row["id"], row["state"], row["owner_pid"])
                    for row in database.execute(
                        'SELECT id, state, owner_pid FROM "transaction" '
                        "WHERE state IN ('preparing', 'prepared', 'applying') "
                        "ORDER BY created_at, id"
                    )
                ]
            for transaction_id, selected_state, owner_pid in rows:
                if (
                    selected_state == "preparing"
                    and owner_pid is not None
                    and _pid_alive(owner_pid)
                ):
                    continue
                record = self._record(transaction_id)
                if record.state == "preparing":
                    promotion = self._promote_preparing(record)
                    if promotion == "invalid":
                        self._set_transaction_state(transaction_id, "discarded")
                        self._remove_artifacts(self.transaction_root / transaction_id)
                        recovered.append(self._record(transaction_id))
                        continue
                    if promotion == "quarantined":
                        recovered.append(self._record(transaction_id))
                        continue
                    record = self._record(transaction_id)
                try:
                    recovered.append(self._apply_locked(transaction_id))
                except TransactionFailure as exc:
                    if exc.code == "precondition_failed":
                        self._rollback_for_quarantine(transaction_id, exc.code)
                    else:
                        self._set_transaction_state(
                            transaction_id, exc.state, error_code=exc.code
                        )
                    recovered.append(self._record(transaction_id))
                except RuntimeError as exc:
                    message = str(exc)
                    if "after-image is corrupt" in message or "plan hash mismatch" in message:
                        recovered.append(self._recover_corrupt_after_image(transaction_id))
                    elif "before state mismatch" in message:
                        rows = self._operation_rows(transaction_id)
                        create_conflict = any(
                            row["kind"] == "create"
                            and self._operation_hash(row) != row["before_hash"]
                            for row in rows
                        )
                        code = (
                            "before_hash_mismatch"
                            if create_conflict
                            else "unknown_target_bytes"
                        )
                        self._set_transaction_state(
                            transaction_id, "conflicted", error_code=code
                        )
                        recovered.append(self._record(transaction_id))
                    elif "after state mismatch" in message:
                        self._set_transaction_state(
                            transaction_id,
                            "conflicted",
                            error_code="unknown_target_bytes",
                        )
                        recovered.append(self._record(transaction_id))
                    else:
                        raise
        return recovered

    def _promote_preparing(self, record: TransactionRecord) -> str:
        artifact_root = self.transaction_root / record.id
        try:
            plan_bytes = (artifact_root / "plan.json").read_bytes()
            plan = json.loads(plan_bytes)
            validate_schema(plan, _SCHEMA)
            if (
                plan_bytes != canonical_json_bytes(plan)
                or plan["transaction_id"] != record.id
            ):
                return "invalid"
            self._verify_plan_artifacts(plan, artifact_root)
            manifest_bytes = (artifact_root / "manifest.json").read_bytes()
            manifest = json.loads(manifest_bytes)
            if manifest_bytes != canonical_json_bytes(manifest):
                return "invalid"
            if set(manifest) != {
                "schema_version",
                "transaction_id",
                "request_hash",
                "plan_hash",
                "operations",
            }:
                return "invalid"
            if (
                manifest["schema_version"] != "markdown-transaction-recovery/v1"
                or manifest["transaction_id"] != record.id
                or manifest["plan_hash"] != sha256_bytes(plan_bytes)
            ):
                return "invalid"
            with self._connect() as database:
                row = database.execute(
                    'SELECT request_hash FROM "transaction" WHERE id = ?',
                    (record.id,),
                ).fetchone()
            if row is None or manifest["request_hash"] != row["request_hash"]:
                return "invalid"
            plan_operations = plan["operations"]
            manifest_operations = manifest["operations"]
            if (
                not isinstance(plan_operations, list)
                or not isinstance(manifest_operations, list)
                or len(plan_operations) != len(manifest_operations)
            ):
                return "invalid"

            operations: list[tuple[object, ...]] = []
            request_changes: list[dict[str, object]] = []
            seen_paths: set[str] = set()
            parent_mismatch = False
            for position, (operation, persisted) in enumerate(
                zip(plan_operations, manifest_operations, strict=True)
            ):
                if not isinstance(operation, dict) or not isinstance(persisted, dict):
                    return "invalid"
                if set(persisted) != {
                    "position",
                    "before_hash",
                    "after_hash",
                    "parent_device",
                    "parent_inode",
                } or persisted["position"] != position:
                    return "invalid"
                if (
                    type(persisted["position"]) is not int
                    or type(persisted["parent_device"]) is not int
                    or type(persisted["parent_inode"]) is not int
                    or persisted["parent_device"] < 0
                    or persisted["parent_inode"] < 0
                ):
                    return "invalid"
                path = str(operation["path"])
                self._target(path)
                normalized = unicodedata.normalize("NFC", path).casefold()
                if normalized in seen_paths:
                    return "invalid"
                seen_paths.add(normalized)
                before_hash = self._state_description_hash(operation["before"])
                after_hash = self._state_description_hash(operation["after"])
                kind = operation["kind"]
                if (
                    persisted["before_hash"] != before_hash
                    or persisted["after_hash"] != after_hash
                    or kind == "create"
                    and (before_hash != ABSENT or after_hash == ABSENT)
                    or kind == "replace"
                    and (before_hash == ABSENT or after_hash == ABSENT)
                    or kind == "delete"
                    and (before_hash == ABSENT or after_hash != ABSENT)
                ):
                    return "invalid"
                current_parent = self._parent_identity(self._target(path).parent)
                parent_identity = (
                    persisted["parent_device"],
                    persisted["parent_inode"],
                )
                parent_mismatch = parent_mismatch or current_parent != parent_identity
                current_hash = self._current_hash(path)
                operations.append(
                    (
                        record.id,
                        position,
                        kind,
                        path,
                        before_hash,
                        after_hash,
                        parent_identity[0],
                        parent_identity[1],
                        int(current_hash == after_hash),
                    )
                )
                request_changes.append(
                    {
                        "kind": kind,
                        "path": path,
                        "content_hash": after_hash,
                    }
                )
            request = {
                "changes": request_changes,
                "preconditions": dict(record.preconditions),
            }
            if sha256_bytes(canonical_json_bytes(request)) != manifest["request_hash"]:
                return "invalid"
        except (AssertionError, KeyError, OSError, TypeError, ValueError):
            return "invalid"

        with self._connect() as database, begin_immediate(database):
            cursor = database.execute(
                'UPDATE "transaction" SET state = \'prepared\', plan_hash = ?, '
                "updated_at = ?, owner_pid = NULL WHERE id = ? AND state = 'preparing'",
                (manifest["plan_hash"], _now(), record.id),
            )
            if cursor.rowcount != 1:
                return "invalid"
            database.executemany(
                'INSERT INTO "operation" '
                "(transaction_id, position, kind, path, before_hash, after_hash, "
                "parent_device, parent_inode, applied) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                operations,
            )
        if parent_mismatch:
            self._set_transaction_state(
                record.id, "quarantined", error_code="precondition_failed"
            )
            return "quarantined"
        return "promoted"

    def _state_description_hash(self, state: object) -> str:
        if state == ABSENT:
            return ABSENT
        if not isinstance(state, dict) or set(state) != {"sha256", "artifact"}:
            raise ValueError("invalid transaction state description")
        value = state["sha256"]
        if not isinstance(value, str):
            raise ValueError("invalid transaction state hash")
        return value

    def _rollback_for_quarantine(self, transaction_id: str, error_code: str) -> None:
        transaction_state = self._record(transaction_id).state
        for row in self._operation_rows(transaction_id):
            if not row["applied"] and transaction_state != "applying":
                continue
            current = self._operation_hash(row)
            if current != row["after_hash"]:
                continue
            before_state: object = ABSENT
            if row["before_hash"] != ABSENT:
                artifact = (
                    self.transaction_root
                    / transaction_id
                    / "before"
                    / f"{row['position']:06d}.bin"
                )
                try:
                    content = artifact.read_bytes()
                except OSError:
                    continue
                if sha256_bytes(content) != row["before_hash"]:
                    continue
                before_state = {
                    "sha256": row["before_hash"],
                    "artifact": f"before/{row['position']:06d}.bin",
                }
            inverse = {
                "transaction_id": transaction_id,
                "position": row["position"],
                "kind": "delete"
                if row["before_hash"] == ABSENT
                else "create"
                if row["after_hash"] == ABSENT
                else "replace",
                "path": row["path"],
                "before_hash": row["after_hash"],
                "after_hash": row["before_hash"],
                "parent_device": row["parent_device"],
                "parent_inode": row["parent_inode"],
            }
            try:
                self._apply_inverse_under_fence(inverse, before_state)
            except (OSError, RuntimeError):
                continue
            with self._connect() as database, begin_immediate(database):
                database.execute(
                    'UPDATE "operation" SET applied = 0 '
                    "WHERE transaction_id = ? AND position = ?",
                    (transaction_id, row["position"]),
                )
        self._set_transaction_state(
            transaction_id, "quarantined", error_code=error_code
        )

    def _apply_inverse_under_fence(
        self, inverse: Mapping[str, object], before_state: object
    ) -> None:
        with self._connect() as database, begin_immediate(database):
            self._assert_writer_ownership(database)
            self._apply_operation(inverse, {"after": before_state})

    def _recover_corrupt_after_image(self, transaction_id: str) -> TransactionRecord:
        rows = self._operation_rows(transaction_id)
        before_states: list[object] = []
        current_hashes: list[str] = []
        for row in rows:
            current = self._operation_hash(row)
            current_hashes.append(current)
            if current not in {row["before_hash"], row["after_hash"]}:
                self._set_transaction_state(
                    transaction_id,
                    "quarantined",
                    error_code="after_image_corrupt",
                )
                return self._record(transaction_id)
            if row["before_hash"] == ABSENT:
                before_states.append(ABSENT)
                continue
            artifact = (
                self.transaction_root
                / transaction_id
                / "before"
                / f"{row['position']:06d}.bin"
            )
            try:
                content = artifact.read_bytes()
            except OSError:
                content = b""
            if sha256_bytes(content) != row["before_hash"]:
                self._set_transaction_state(
                    transaction_id,
                    "quarantined",
                    error_code="after_image_corrupt",
                )
                return self._record(transaction_id)
            before_states.append(
                {
                    "sha256": row["before_hash"],
                    "artifact": f"before/{row['position']:06d}.bin",
                }
            )

        for row, current, before_state in zip(
            rows, current_hashes, before_states, strict=True
        ):
            if current == row["before_hash"]:
                continue
            inverse = {
                "transaction_id": transaction_id,
                "position": row["position"],
                "kind": "delete"
                if row["before_hash"] == ABSENT
                else "create"
                if row["after_hash"] == ABSENT
                else "replace",
                "path": row["path"],
                "before_hash": row["after_hash"],
                "after_hash": row["before_hash"],
                "parent_device": row["parent_device"],
                "parent_inode": row["parent_inode"],
            }
            try:
                self._apply_inverse_under_fence(inverse, before_state)
            except RuntimeError:
                self._set_transaction_state(
                    transaction_id,
                    "quarantined",
                    error_code="after_image_corrupt",
                )
                return self._record(transaction_id)
        self._set_transaction_state(
            transaction_id, "discarded", error_code="after_image_corrupt"
        )
        return self._record(transaction_id)

    def undo(self, transaction_id: str) -> TransactionRecord:
        with self.writer_gate():
            return self._prepare_undo(transaction_id)

    def _prepare_undo(self, transaction_id: str) -> TransactionRecord:
        original = self._record(transaction_id)
        if original.state != "committed":
            raise RuntimeError("only a committed transaction can be undone")
        if _parse_timestamp(original.updated_at) < datetime.now(timezone.utc) - timedelta(
            days=30
        ):
            raise RuntimeError("transaction is outside the 30-day undo window")
        if not (self.transaction_root / transaction_id).is_dir():
            raise RuntimeError("transaction undo images are no longer retained")
        rows = self._operation_rows(transaction_id)
        for row in rows:
            if self._operation_hash(row) != row["after_hash"]:
                raise RuntimeError("undo precondition failed: current target changed")

        changes: list[MarkdownChange] = []
        preconditions: dict[str, object] = {}
        for row in rows:
            preconditions[row["path"]] = row["after_hash"]
            if row["before_hash"] == ABSENT:
                changes.append(MarkdownChange.delete(row["path"]))
                continue
            before = (
                self.transaction_root
                / transaction_id
                / "before"
                / f"{row['position']:06d}.bin"
            ).read_bytes()
            if sha256_bytes(before) != row["before_hash"]:
                raise RuntimeError("transaction before-image is corrupt")
            if row["after_hash"] == ABSENT:
                changes.append(MarkdownChange.create(row["path"], before))
            else:
                changes.append(MarkdownChange.replace(row["path"], before))
        return self.prepare(
            changes,
            operation_id=f"undo:{transaction_id}:{uuid.uuid4().hex}",
            preconditions=preconditions,
            _parent_transaction_id=transaction_id,
        )

    def prune(
        self, *, retention_days: int = 30, now: datetime | None = None
    ) -> int:
        if retention_days < 30:
            raise ValueError("retention_days must be at least 30")
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        cutoff = current - timedelta(days=retention_days)
        pruned = 0
        with self.writer_gate():
            with self._connect() as database:
                rows = list(
                    database.execute(
                        'SELECT id, updated_at FROM "transaction" '
                        "WHERE state IN ('committed', 'discarded') "
                        "AND artifacts_pruned_at IS NULL"
                    )
                )
            for row in rows:
                if _parse_timestamp(row["updated_at"]) >= cutoff:
                    continue
                artifact_root = self.transaction_root / row["id"]
                if not artifact_root.exists():
                    continue
                self._remove_artifacts(artifact_root)
                with self._connect() as database, begin_immediate(database):
                    database.execute(
                        'UPDATE "transaction" SET artifacts_pruned_at = ? WHERE id = ?',
                        (_now(), row["id"]),
                    )
                pruned += 1
        return pruned

    def deletion_blockers(self) -> list[dict[str, str]]:
        blockers: list[dict[str, str]] = []
        now = datetime.now(timezone.utc)
        with self._connect() as database:
            rows = list(
                database.execute(
                    'SELECT id, state, error_code, updated_at, artifacts_pruned_at '
                    'FROM "transaction" ORDER BY created_at, id'
                )
            )
        for row in rows:
            code: str | None = None
            if row["state"] in {"preparing", "prepared", "applying"}:
                code = "nonterminal_transaction"
            elif row["state"] in {"conflicted", "quarantined"}:
                code = row["error_code"] or "transaction_requires_attention"
            elif (
                row["state"] == "committed"
                and row["artifacts_pruned_at"] is None
                and _parse_timestamp(row["updated_at"]) >= now - timedelta(days=30)
            ):
                code = "undo_retention"
            if code is not None:
                blockers.append(
                    {
                        "transaction_id": row["id"],
                        "state": row["state"],
                        "code": code,
                    }
                )
        return blockers

    def _set_transaction_state(
        self, transaction_id: str, state: str, *, error_code: str | None = None
    ) -> None:
        with self._connect() as database, begin_immediate(database):
            database.execute(
                'UPDATE "transaction" SET state = ?, error_code = ?, updated_at = ? '
                "WHERE id = ?",
                (state, error_code, _now(), transaction_id),
            )

    @contextlib.contextmanager
    def writer_gate(self) -> Iterator[None]:
        depth = getattr(self._local, "gate_depth", 0)
        if depth:
            self._local.gate_depth = depth + 1
            try:
                yield
            finally:
                self._local.gate_depth -= 1
            return

        owner_token = uuid.uuid4().hex
        deadline = time.monotonic() + _WRITER_WAIT_SECONDS
        fencing_epoch = 0
        while True:
            acquired = False
            with self._connect() as database, begin_immediate(database):
                row = database.execute(
                    "SELECT * FROM writer_owners WHERE gate_name = 'global'"
                ).fetchone()
                if row is None or self._writer_owner_reclaimable(row):
                    fence = database.execute(
                        "SELECT last_epoch FROM writer_fences WHERE gate_name = 'global'"
                    ).fetchone()
                    fencing_epoch = 1 if fence is None else fence["last_epoch"] + 1
                    database.execute(
                        "INSERT INTO writer_fences (gate_name, last_epoch) VALUES ('global', ?) "
                        "ON CONFLICT(gate_name) DO UPDATE SET last_epoch = excluded.last_epoch",
                        (fencing_epoch,),
                    )
                    heartbeat = _now()
                    expires = _future_timestamp(_WRITER_LEASE_SECONDS)
                    database.execute("DELETE FROM writer_owners WHERE gate_name = 'global'")
                    database.execute(
                        "INSERT INTO writer_owners "
                        "(gate_name, owner_token, process_id, thread_id, acquired_at, "
                        "heartbeat_at, expires_at, fencing_epoch) "
                        "VALUES ('global', ?, ?, ?, ?, ?, ?, ?)",
                        (
                            owner_token,
                            os.getpid(),
                            threading.get_ident(),
                            heartbeat,
                            heartbeat,
                            expires,
                            fencing_epoch,
                        ),
                    )
                    acquired = True
            if acquired:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("timed out waiting for the global Markdown writer gate")
            time.sleep(0.01)

        self._local.gate_depth = 1
        self._local.gate_token = owner_token
        self._local.gate_fence = fencing_epoch
        heartbeat_stop = threading.Event()
        heartbeat_lost = threading.Event()
        heartbeat_thread = threading.Thread(
            target=self._heartbeat_writer_gate,
            args=(owner_token, fencing_epoch, heartbeat_stop, heartbeat_lost),
            name="markdown-writer-heartbeat",
            daemon=True,
        )
        heartbeat_thread.start()
        try:
            yield
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=_WRITER_HEARTBEAT_SECONDS * 2)
            try:
                with self._connect() as database, begin_immediate(database):
                    cursor = database.execute(
                        "DELETE FROM writer_owners WHERE gate_name = 'global' "
                        "AND owner_token = ? AND fencing_epoch = ?",
                        (owner_token, fencing_epoch),
                    )
                    if cursor.rowcount != 1 or heartbeat_lost.is_set():
                        raise RuntimeError("Markdown writer gate ownership was lost")
            finally:
                self._local.gate_depth = 0
                self._local.gate_token = None
                self._local.gate_fence = None

    def _writer_owner_reclaimable(self, row: sqlite3.Row) -> bool:
        expires_at = row["expires_at"]
        expired = not expires_at or _parse_timestamp(expires_at) <= datetime.now(timezone.utc)
        return expired or not _pid_alive(row["process_id"])

    def _heartbeat_writer_gate(
        self,
        owner_token: str,
        fencing_epoch: int,
        stop: threading.Event,
        lost: threading.Event,
    ) -> None:
        while not stop.wait(_WRITER_HEARTBEAT_SECONDS):
            try:
                with self._connect() as database, begin_immediate(database):
                    cursor = database.execute(
                        "UPDATE writer_owners SET heartbeat_at = ?, expires_at = ? "
                        "WHERE gate_name = 'global' AND owner_token = ? AND fencing_epoch = ?",
                        (
                            _now(),
                            _future_timestamp(_WRITER_LEASE_SECONDS),
                            owner_token,
                            fencing_epoch,
                        ),
                    )
                    if cursor.rowcount != 1:
                        lost.set()
                        return
            except (OSError, sqlite3.Error):
                lost.set()
                return

    def writer_gate_held(self) -> bool:
        return bool(getattr(self._local, "gate_depth", 0))

    def assert_external_work_allowed(self) -> None:
        if self.writer_gate_held():
            raise RuntimeError("external LLM or Git work is forbidden under the writer gate")

    def coherent_read(self, paths: Sequence[Path]) -> dict[Path, bytes | None]:
        with self.writer_gate():
            return {Path(path): self._read_target(self._target(Path(path).as_posix())) for path in paths}

    def _validate_change(self, change: MarkdownChange) -> MarkdownChange:
        if not isinstance(change, MarkdownChange):
            raise TypeError("changes must contain MarkdownChange values")
        if change.kind not in {"create", "replace", "delete"}:
            raise ValueError(f"unsupported change kind: {change.kind}")
        if change.kind == "delete":
            if change.content is not None:
                raise ValueError("delete content must be absent")
        elif not isinstance(change.content, bytes):
            raise TypeError("create and replace content must be bytes")
        self._target(change.path)
        return change

    def _target(self, value: str) -> Path:
        relative = restricted_relative_path(
            value, (*_ALLOWED_DIRECTORIES, *_ALLOWED_FILES)
        )
        if unicodedata.normalize("NFC", value) != value:
            raise ValueError("path must use NFC Unicode normalization")
        reserved = {"con", "prn", "aux", "nul"} | {
            f"{prefix}{number}" for prefix in ("com", "lpt") for number in range(1, 10)
        }
        for part in relative.parts:
            if (
                part.endswith((" ", "."))
                or any(character in '<>:"|?*' or ord(character) < 32 for character in part)
                or part.rstrip(" .").split(".", 1)[0].casefold() in reserved
            ):
                raise ValueError("path contains a non-portable or reserved component")
        normalized = relative.as_posix()
        if normalized not in _ALLOWED_FILES and not any(
            normalized.startswith(f"{root}/") for root in _ALLOWED_DIRECTORIES
        ):
            raise ValueError("path is outside every allowed Markdown root")
        if relative.suffix.casefold() != ".md":
            raise ValueError("transaction targets must be Markdown files")
        target = self.vault.joinpath(*relative.parts)
        current = self.vault
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError(f"target traverses a symlink: {value}")
            if _is_reparse_point(current):
                raise ValueError(f"target traverses a Windows reparse point: {value}")
            if not current.exists():
                break
        if target.parent.resolve(strict=False) != target.parent:
            raise ValueError(f"target has a non-canonical parent: {value}")
        try:
            target.relative_to(self.vault)
        except ValueError as exc:
            raise ValueError("target escapes the vault") from exc
        if not target.parent.is_dir():
            raise ValueError(f"target parent does not exist: {value}")
        return target

    def _validate_preconditions(self, preconditions: Mapping[str, object]) -> dict[str, object]:
        if not isinstance(preconditions, Mapping):
            raise TypeError("preconditions must be a mapping")
        result: dict[str, object] = {}
        for path, expected in preconditions.items():
            if path == "project_lease":
                if not isinstance(expected, Mapping):
                    raise TypeError("project_lease precondition must be a mapping")
                required = {"project", "lease_token", "fencing_epoch", "expires_at"}
                if set(expected) != required:
                    raise ValueError("project_lease precondition has invalid fields")
                if (
                    not isinstance(expected["project"], str)
                    or not expected["project"]
                    or not isinstance(expected["lease_token"], str)
                    or not expected["lease_token"]
                    or not isinstance(expected["fencing_epoch"], int)
                    or expected["fencing_epoch"] < 1
                    or not isinstance(expected["expires_at"], str)
                ):
                    raise ValueError("project_lease precondition has invalid values")
                _parse_timestamp(expected["expires_at"])
                result[path] = dict(expected)
                continue
            self._target(path)
            if expected != ABSENT and (
                not isinstance(expected, str)
                or len(expected) != 64
                or any(character not in "0123456789abcdef" for character in expected)
            ):
                raise ValueError("precondition values must be 'absent' or SHA-256 hashes")
            result[path] = expected
        canonical_json_bytes(result)
        return result

    def _request_hash(
        self, changes: Sequence[MarkdownChange], preconditions: Mapping[str, object]
    ) -> str:
        request = {
            "changes": [
                {
                    "kind": change.kind,
                    "path": change.path,
                    "content_hash": ABSENT
                    if change.content is None
                    else sha256_bytes(change.content),
                }
                for change in changes
            ],
            "preconditions": dict(preconditions),
        }
        return sha256_bytes(canonical_json_bytes(request))

    def _stage_state(self, root: Path, position: int, content: bytes | None) -> object:
        if content is None:
            return ABSENT
        name = f"{position:06d}.bin"
        self._write_new_file(root / name, content)
        return {"sha256": sha256_bytes(content), "artifact": f"{root.name}/{name}"}

    def _verify_plan_artifacts(self, plan: Mapping[str, object], artifact_root: Path) -> None:
        operations = plan["operations"]
        assert isinstance(operations, list)
        for operation in operations:
            assert isinstance(operation, dict)
            for state_name in ("before", "after"):
                state = operation[state_name]
                if state == ABSENT:
                    continue
                assert isinstance(state, dict)
                relative = restricted_relative_path(str(state["artifact"]), (state_name,))
                artifact = artifact_root.joinpath(*relative.parts)
                if sha256_bytes(artifact.read_bytes()) != state["sha256"]:
                    raise RuntimeError(f"transaction artifact hash mismatch: {relative}")

    def _write_new_file(self, path: Path, content: bytes, *, owner_only: bool = True) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
        if owner_only:
            if os.name != "nt":
                _harden_owner_only(path, 0o600)
        fsync_file(path)

    def _read_target(self, target: Path) -> bytes | None:
        try:
            with target.open("rb") as handle:
                return handle.read()
        except FileNotFoundError:
            return None

    def _parent_identity(self, parent: Path) -> tuple[int, int]:
        metadata = parent.stat(follow_symlinks=False)
        if not stat.S_ISDIR(metadata.st_mode) or _is_reparse_point(parent):
            raise RuntimeError(f"parent identity is not a stable directory: {parent}")
        return metadata.st_dev, metadata.st_ino

    def _capture_target(self, target: Path) -> tuple[bytes | None, tuple[int, int]]:
        if not _use_posix_dir_fd():
            with self._hold_windows_parent(target.parent):
                before = self._parent_identity(target.parent)
                content = self._read_target(target)
                if self._parent_identity(target.parent) != before:
                    raise RuntimeError(f"parent identity changed while reading {target}")
                return content, before
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(target.parent, flags)
        try:
            metadata = os.fstat(descriptor)
            identity = (metadata.st_dev, metadata.st_ino)
            content = self._read_from_parent(descriptor, target.name)
            if self._parent_identity(target.parent) != identity:
                raise RuntimeError(f"parent identity changed while reading {target}")
            return content, identity
        finally:
            os.close(descriptor)

    @contextlib.contextmanager
    def _stable_parent(self, row: sqlite3.Row) -> Iterator[tuple[Path, int | None]]:
        target = self._target(row["path"])
        expected = (row["parent_device"], row["parent_inode"])
        if None in expected:
            raise RuntimeError(f"transaction lacks parent identity for {row['path']}")
        if not _use_posix_dir_fd():
            with self._hold_windows_parent(target.parent):
                if self._parent_identity(target.parent) != expected:
                    raise RuntimeError(f"parent identity mismatch for {row['path']}")
                try:
                    yield target, None
                finally:
                    if self._parent_identity(target.parent) != expected:
                        raise RuntimeError(f"parent identity mismatch for {row['path']}")
            return

        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(target.parent, flags)
        try:
            metadata = os.fstat(descriptor)
            if (metadata.st_dev, metadata.st_ino) != expected:
                raise RuntimeError(f"parent identity mismatch for {row['path']}")
            yield target, descriptor
            if self._parent_identity(target.parent) != expected:
                raise RuntimeError(f"parent identity mismatch for {row['path']}")
        finally:
            os.close(descriptor)

    @contextlib.contextmanager
    def _hold_windows_parent(self, parent: Path) -> Iterator[None]:
        if os.name != "nt":
            raise RuntimeError("safe non-POSIX mutation requires Windows directory handles")
        try:
            relative = parent.relative_to(self.vault)
        except ValueError as exc:
            raise RuntimeError("target parent is outside the vault") from exc
        paths = [self.vault]
        current = self.vault
        for part in relative.parts:
            current = current / part
            paths.append(current)
        handles: list[int] = []
        previous_parent_handle = getattr(self._local, "windows_parent_handle", None)
        try:
            for path in paths:
                handles.append(_open_windows_directory(path))
            self._local.windows_parent_handle = handles[-1]
            yield
        finally:
            self._local.windows_parent_handle = previous_parent_handle
            close_error: OSError | None = None
            for handle in reversed(handles):
                try:
                    _close_windows_handle(handle)
                except OSError as exc:
                    close_error = close_error or exc
            if close_error is not None:
                raise close_error

    def _read_from_parent(self, parent_descriptor: int, name: str) -> bytes | None:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(name, flags, dir_fd=parent_descriptor)
        except FileNotFoundError:
            return None
        with os.fdopen(descriptor, "rb") as handle:
            return handle.read()

    def _read_operation_target(self, target: Path, parent_descriptor: int | None) -> bytes | None:
        if parent_descriptor is None:
            return self._read_target(target)
        return self._read_from_parent(parent_descriptor, target.name)

    def _operation_hash(self, row: sqlite3.Row) -> str:
        with self._stable_parent(row) as (target, parent_descriptor):
            content = self._read_operation_target(target, parent_descriptor)
        return ABSENT if content is None else sha256_bytes(content)

    def _current_hash(self, path: str) -> str:
        content = self._read_target(self._target(path))
        return ABSENT if content is None else sha256_bytes(content)

    def _check_preconditions(
        self, preconditions: Mapping[str, object], reconciled_after: set[str]
    ) -> None:
        for path, expected in preconditions.items():
            if path == "project_lease":
                assert isinstance(expected, Mapping)
                with self._connect() as database:
                    row = database.execute(
                        "SELECT * FROM project_leases WHERE project = ?",
                        (expected["project"],),
                    ).fetchone()
                now = datetime.now(timezone.utc)
                if (
                    row is None
                    or row["lease_token"] != expected["lease_token"]
                    or row["fencing_epoch"] != expected["fencing_epoch"]
                    or _parse_timestamp(row["expires_at"]) <= now
                    or _parse_timestamp(str(expected["expires_at"])) <= now
                ):
                    raise TransactionFailure(
                        "persisted project lease precondition failed",
                        "precondition_failed",
                        "quarantined",
                    )
                continue
            if path in reconciled_after:
                continue
            if self._current_hash(path) != expected:
                raise TransactionFailure(
                    f"persisted precondition failed for {path}",
                    "precondition_failed",
                    "quarantined",
                )

    def _killpoint(self, name: str, parent_transaction_id: str | None = None) -> None:
        prefix = "undo_" if parent_transaction_id is not None else ""
        aliases = {name, f"{name[:6]}{prefix}{name[6:]}" if name.startswith("after_") else name}
        if name == "after_each_target" and parent_transaction_id is not None:
            aliases.add("after_each_undo_target")
        if name == "before_commit" and parent_transaction_id is not None:
            aliases.add("before_undo_commit")
        configured = os.environ.get("LLM_WIKI_TRANSACTION_KILLPOINT")
        if configured in aliases:
            os._exit(86)

    def _reconcile_operation_states(
        self, transaction_id: str, rows: Sequence[sqlite3.Row]
    ) -> set[str]:
        reconciled_after: set[str] = set()
        for row in rows:
            current = self._operation_hash(row)
            if row["applied"]:
                if current != row["after_hash"]:
                    raise RuntimeError(f"after state mismatch for {row['path']}")
                reconciled_after.add(row["path"])
            elif current == row["after_hash"]:
                self._mark_operation_applied(transaction_id, row["position"])
                reconciled_after.add(row["path"])
            elif current != row["before_hash"]:
                raise RuntimeError(f"before state mismatch for {row['path']}")
        return reconciled_after

    def _mark_operation_applied(self, transaction_id: str, position: int) -> None:
        active_database = getattr(self._local, "mutation_database", None)
        if active_database is not None:
            active_database.execute(
                'UPDATE "operation" SET applied = 1 '
                "WHERE transaction_id = ? AND position = ?",
                (transaction_id, position),
            )
            return
        with self._connect() as database, begin_immediate(database):
            self._assert_writer_ownership(database)
            database.execute(
                'UPDATE "operation" SET applied = 1 '
                "WHERE transaction_id = ? AND position = ?",
                (transaction_id, position),
            )

    def _mutate_and_mark(
        self,
        transaction_id: str,
        row: sqlite3.Row,
        operation_plan: Mapping[str, object],
    ) -> None:
        with self._connect() as database, begin_immediate(database):
            self._assert_writer_ownership(database)
            self._local.mutation_database = database
            try:
                self._apply_operation(row, operation_plan)
                self._require_operation_state(row, row["after_hash"], "after state")
                self._mark_operation_applied(transaction_id, row["position"])
            finally:
                self._local.mutation_database = None

    def _assert_writer_ownership(self, database: sqlite3.Connection) -> None:
        owner_token = getattr(self._local, "gate_token", None)
        fencing_epoch = getattr(self._local, "gate_fence", None)
        row = database.execute(
            "SELECT owner_token, fencing_epoch FROM writer_owners WHERE gate_name = 'global'"
        ).fetchone()
        if (
            row is None
            or row["owner_token"] != owner_token
            or row["fencing_epoch"] != fencing_epoch
        ):
            raise RuntimeError("Markdown writer gate ownership was lost before mutation")
        cursor = database.execute(
            "UPDATE writer_owners SET heartbeat_at = ?, expires_at = ? "
            "WHERE gate_name = 'global' AND owner_token = ? AND fencing_epoch = ?",
            (
                _now(),
                _future_timestamp(_WRITER_LEASE_SECONDS),
                owner_token,
                fencing_epoch,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("Markdown writer gate ownership was lost before mutation")

    def _require_operation_state(self, row: sqlite3.Row, expected: str, label: str) -> None:
        if self._operation_hash(row) != expected:
            raise RuntimeError(f"{label} mismatch for {row['path']}")

    def _apply_operation(self, row: sqlite3.Row, operation_plan: Mapping[str, object]) -> None:
        with self._stable_parent(row) as (target, parent_descriptor):
            current = self._read_operation_target(target, parent_descriptor)
            current_hash = ABSENT if current is None else sha256_bytes(current)
            if current_hash != row["before_hash"]:
                raise RuntimeError(f"before state mismatch for {row['path']}")
            if parent_descriptor is None:
                self._apply_windows_operation(row, operation_plan, target)
                return
            self._before_target_mutation(target)

            if row["kind"] == "delete":
                os.unlink(target.name, dir_fd=parent_descriptor)
                os.fsync(parent_descriptor)
                return

            after = operation_plan["after"]
            if not isinstance(after, dict):
                raise RuntimeError("transaction after-image is absent")
            artifact = self.transaction_root / row["transaction_id"] / str(after["artifact"])
            content = artifact.read_bytes()
            if sha256_bytes(content) != row["after_hash"]:
                raise RuntimeError(f"transaction after-image is corrupt for {row['path']}")
            temporary_name = f".{target.name}.{uuid.uuid4().hex}.tmp"
            try:
                self._write_new_file_at(parent_descriptor, temporary_name, content)
                current = self._read_operation_target(target, parent_descriptor)
                current_hash = ABSENT if current is None else sha256_bytes(current)
                if current_hash != row["before_hash"]:
                    raise RuntimeError(f"before state mismatch for {row['path']}")
                if row["kind"] == "create":
                    try:
                        os.link(
                            temporary_name,
                            target.name,
                            src_dir_fd=parent_descriptor,
                            dst_dir_fd=parent_descriptor,
                            follow_symlinks=False,
                        )
                    except FileExistsError as exc:
                        raise RuntimeError(f"before state mismatch for {row['path']}") from exc
                    os.unlink(temporary_name, dir_fd=parent_descriptor)
                else:
                    os.replace(
                        temporary_name,
                        target.name,
                        src_dir_fd=parent_descriptor,
                        dst_dir_fd=parent_descriptor,
                    )
                os.fsync(parent_descriptor)
                actual = self._read_operation_target(target, parent_descriptor)
                actual_hash = ABSENT if actual is None else sha256_bytes(actual)
                if actual_hash != row["after_hash"]:
                    raise RuntimeError(f"after state mismatch for {row['path']}")
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temporary_name, dir_fd=parent_descriptor)

    def _apply_windows_operation(
        self,
        row: sqlite3.Row,
        operation_plan: Mapping[str, object],
        target: Path,
    ) -> None:
        parent_handle = getattr(self._local, "windows_parent_handle", None)
        if parent_handle is None:
            raise RuntimeError("Windows parent handle is not held at mutation")
        if row["kind"] == "delete":
            target_handle = _open_windows_file_for_mutation(target)
            try:
                self._before_target_mutation(target)
                _delete_windows_handle(target_handle)
            finally:
                _close_windows_handle(target_handle)
            fsync_directory(target.parent)
            return

        after = operation_plan["after"]
        if not isinstance(after, dict):
            raise RuntimeError("transaction after-image is absent")
        artifact = self.transaction_root / row["transaction_id"] / str(after["artifact"])
        content = artifact.read_bytes()
        if sha256_bytes(content) != row["after_hash"]:
            raise RuntimeError(f"transaction after-image is corrupt for {row['path']}")
        temporary = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
        self._write_new_file(temporary, content, owner_only=False)
        try:
            temporary_handle = _open_windows_file_for_mutation(temporary)
        except BaseException:
            temporary.unlink()
            raise
        renamed = False
        try:
            self._before_target_mutation(target)
            _rename_windows_handle(
                temporary_handle,
                parent_handle,
                target.name,
                replace=row["kind"] == "replace",
            )
            renamed = True
            fsync_directory(target.parent)
        finally:
            if not renamed:
                with contextlib.suppress(OSError):
                    _delete_windows_handle(temporary_handle)
            _close_windows_handle(temporary_handle)

    def _before_target_mutation(self, target: Path) -> None:
        """Failure-injection boundary after parent binding and before mutation."""

    def _write_new_file_at(self, parent_descriptor: int, name: str, content: bytes) -> None:
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_BINARY", 0)
        )
        descriptor = os.open(name, flags, 0o600, dir_fd=parent_descriptor)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())

    def _load_verified_plan(self, record: TransactionRecord) -> dict[str, object]:
        plan_path = self.transaction_root / record.id / "plan.json"
        try:
            plan_bytes = plan_path.read_bytes()
            with self._connect() as database:
                expected = database.execute(
                    'SELECT plan_hash FROM "transaction" WHERE id = ?', (record.id,)
                ).fetchone()["plan_hash"]
            if sha256_bytes(plan_bytes) != expected:
                raise RuntimeError("transaction plan hash mismatch")
            plan = json.loads(plan_bytes)
            validate_schema(plan, _SCHEMA)
            self._verify_plan_artifacts(plan, self.transaction_root / record.id)
        except (AssertionError, KeyError, OSError, RuntimeError, ValueError) as exc:
            raise RuntimeError("transaction after-image is corrupt") from exc
        return plan

    def _operation_rows(self, transaction_id: str) -> list[sqlite3.Row]:
        with self._connect() as database:
            return list(
                database.execute(
                    'SELECT * FROM "operation" WHERE transaction_id = ? ORDER BY position',
                    (transaction_id,),
                )
            )

    def _record(self, transaction_id: str) -> TransactionRecord:
        record = self._record_if_present(transaction_id)
        if record is None:
            raise KeyError(f"unknown transaction: {transaction_id}")
        return record

    def _record_if_present(self, transaction_id: str) -> TransactionRecord | None:
        with self._connect() as database:
            row = database.execute(
                'SELECT * FROM "transaction" WHERE id = ?', (transaction_id,)
            ).fetchone()
            if row is None:
                return None
            operation_rows = list(
                database.execute(
                    'SELECT * FROM "operation" WHERE transaction_id = ? ORDER BY position',
                    (transaction_id,),
                )
            )
        return TransactionRecord(
            id=row["id"],
            operation_id=row["operation_id"],
            state=row["state"],
            operations=tuple(
                MarkdownOperation(
                    operation["kind"],
                    operation["path"],
                    operation["before_hash"],
                    operation["after_hash"],
                )
                for operation in operation_rows
            ),
            preconditions=json.loads(row["preconditions_json"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            parent_transaction_id=row["parent_transaction_id"],
            error_code=row["error_code"],
        )

    def _record_for_operation_id(self, operation_id: str) -> TransactionRecord | None:
        with self._connect() as database:
            row = database.execute(
                'SELECT id FROM "transaction" WHERE operation_id = ?', (operation_id,)
            ).fetchone()
        return None if row is None else self._record(row["id"])

    def _request_hash_for_operation_id(self, operation_id: str) -> str | None:
        with self._connect() as database:
            row = database.execute(
                'SELECT request_hash FROM "transaction" WHERE operation_id = ?', (operation_id,)
            ).fetchone()
        return None if row is None else row["request_hash"]

    def _remove_artifacts(self, root: Path) -> None:
        if not root.exists():
            return
        for path in sorted(root.rglob("*"), reverse=True):
            if path.is_dir():
                path.rmdir()
            else:
                path.unlink()
        root.rmdir()


def _redacted_record(record: TransactionRecord) -> dict[str, str | None]:
    return {
        "transaction_id": record.id,
        "state": record.state,
        "code": record.error_code,
    }


def _print_canonical_json(payload: object) -> None:
    sys.stdout.write(canonical_json_bytes(payload).decode("utf-8") + "\n")


def _bounded_transaction_id(value: object) -> str | None:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        return None
    if any(character not in "0123456789abcdefghijklmnopqrstuvwxyz-_" for character in value):
        return None
    return value


def _cli_error_code(error: Exception) -> str:
    if isinstance(error, TransactionFailure):
        return error.code
    if isinstance(error, KeyError):
        return "unknown_transaction"
    message = str(error)
    if "at least 30" in message:
        return "retention_too_short"
    if "undo precondition" in message:
        return "undo_precondition_failed"
    if "undo window" in message:
        return "undo_window_expired"
    if "only a committed transaction" in message:
        return "transaction_not_committed"
    if "before-image is corrupt" in message:
        return "before_image_corrupt"
    if isinstance(error, TimeoutError):
        return "writer_busy"
    if isinstance(error, ValueError):
        return "invalid_argument"
    return "operation_failed"


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("recover", help="recover incomplete transactions")
    undo_parser = subparsers.add_parser("undo", help="undo a committed transaction")
    undo_parser.add_argument("transaction_id")
    prune_parser = subparsers.add_parser("prune", help="prune expired transaction images")
    prune_parser.add_argument("--retention-days", type=int, default=30)
    args = parser.parse_args()

    coordinator: MarkdownCoordinator | None = None
    exit_code = 0
    try:
        vault = Path(
            os.environ.get("LLM_WIKI_ROOT", Path(__file__).resolve().parent.parent)
        ).resolve()
        state_root = Path(os.environ.get("LLM_WIKI_STATE_ROOT", vault)).resolve()
        coordinator = MarkdownCoordinator(vault, state_root)
        if args.command == "recover":
            records = coordinator.recover()
            payload: object = [_redacted_record(record) for record in records]
            if any(
                record.state in {"conflicted", "quarantined"} for record in records
            ):
                exit_code = 2
        elif args.command == "undo":
            undo = coordinator.undo(args.transaction_id)
            committed = coordinator.apply(undo.id)
            payload = {
                **_redacted_record(committed),
                "parent_transaction_id": committed.parent_transaction_id,
            }
        else:
            payload = {"pruned": coordinator.prune(retention_days=args.retention_days)}
    except Exception as error:
        transaction_id = _bounded_transaction_id(
            getattr(args, "transaction_id", None)
        )
        state: str | None = None
        if coordinator is not None and transaction_id is not None:
            try:
                record = coordinator._record_if_present(transaction_id)
            except Exception:
                record = None
            state = None if record is None else record.state
        _print_canonical_json(
            {
                "code": _cli_error_code(error),
                "state": state,
                "transaction_id": transaction_id,
            }
        )
        return 2
    _print_canonical_json(payload)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(_main())
