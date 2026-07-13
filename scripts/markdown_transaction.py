"""Recoverable, hash-checked transactions for authoritative Markdown files."""

from __future__ import annotations

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
        return open_operational_db(self.database_path, busy_ms=DEFAULTS.markdown_busy_ms)

    def _initialize_database(self) -> None:
        with self._connect() as database:
            with begin_immediate(database):
                database.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS "transaction" (
                        id TEXT PRIMARY KEY,
                        operation_id TEXT NOT NULL UNIQUE,
                        request_hash TEXT NOT NULL,
                        state TEXT NOT NULL CHECK (state IN ('prepared', 'applying', 'committed')),
                        preconditions_json TEXT NOT NULL,
                        plan_hash TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
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

    def prepare(
        self,
        changes: Sequence[MarkdownChange],
        *,
        operation_id: str,
        preconditions: Mapping[str, object] | None = None,
        validators: Sequence[Validator] = (),
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
            for directory in (before_root, after_root, artifact_root, self.transaction_root):
                fsync_directory(directory)

            timestamp = _now()
            try:
                with self._connect() as database, begin_immediate(database):
                    database.execute(
                        'INSERT INTO "transaction" '
                        "(id, operation_id, request_hash, state, preconditions_json, plan_hash, "
                        "created_at, updated_at) VALUES (?, ?, ?, 'prepared', ?, ?, ?, ?)",
                        (
                            transaction_id,
                            operation_id,
                            request_hash,
                            canonical_json_bytes(persisted_preconditions).decode("utf-8"),
                            sha256_bytes(plan_bytes),
                            timestamp,
                            timestamp,
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
                existing = self._record_for_operation_id(operation_id)
                if existing is None or self._request_hash_for_operation_id(operation_id) != request_hash:
                    raise ValueError("operation_id is already bound to a different request") from None
                self._remove_artifacts(artifact_root)
                return existing
            return self._record(transaction_id)
        except BaseException:
            if self._record_if_present(transaction_id) is None:
                self._remove_artifacts(artifact_root)
            raise

    def apply(self, transaction_id: str) -> TransactionRecord:
        with self.writer_gate():
            record = self._record(transaction_id)
            if record.state == "committed":
                return record
            if record.state not in {"prepared", "applying"}:
                raise RuntimeError(f"transaction cannot be applied from state {record.state}")
            self._check_preconditions(record.preconditions)
            rows = self._operation_rows(transaction_id)
            self._reconcile_operation_states(transaction_id, rows)
            rows = self._operation_rows(transaction_id)
            with self._connect() as database, begin_immediate(database):
                database.execute(
                    'UPDATE "transaction" SET state = \'applying\', updated_at = ? WHERE id = ?',
                    (_now(), transaction_id),
                )

            plan = self._load_verified_plan(record)
            for row, operation_plan in zip(rows, plan["operations"], strict=True):
                if row["applied"]:
                    self._require_operation_state(row, row["after_hash"], "after state")
                    continue
                self._apply_operation(row, operation_plan)
                self._require_operation_state(row, row["after_hash"], "after state")
                self._mark_operation_applied(transaction_id, row["position"])

            for row in self._operation_rows(transaction_id):
                self._require_operation_state(row, row["after_hash"], "after state")
            with self._connect() as database, begin_immediate(database):
                database.execute(
                    'UPDATE "transaction" SET state = \'committed\', updated_at = ? WHERE id = ?',
                    (_now(), transaction_id),
                )
            return self._record(transaction_id)

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

    def _check_preconditions(self, preconditions: Mapping[str, object]) -> None:
        for path, expected in preconditions.items():
            if self._current_hash(path) != expected:
                raise RuntimeError(f"persisted precondition failed for {path}")

    def _reconcile_operation_states(
        self, transaction_id: str, rows: Sequence[sqlite3.Row]
    ) -> None:
        for row in rows:
            current = self._operation_hash(row)
            if row["applied"]:
                if current != row["after_hash"]:
                    raise RuntimeError(f"after state mismatch for {row['path']}")
            elif current == row["after_hash"]:
                self._mark_operation_applied(transaction_id, row["position"])
            elif current != row["before_hash"]:
                raise RuntimeError(f"before state mismatch for {row['path']}")

    def _mark_operation_applied(self, transaction_id: str, position: int) -> None:
        with self._connect() as database, begin_immediate(database):
            database.execute(
                'UPDATE "operation" SET applied = 1 '
                "WHERE transaction_id = ? AND position = ?",
                (transaction_id, position),
            )

    def _require_operation_state(self, row: sqlite3.Row, expected: str, label: str) -> None:
        if self._operation_hash(row) != expected:
            raise RuntimeError(f"{label} mismatch for {row['path']}")

    def _apply_operation(self, row: sqlite3.Row, operation_plan: Mapping[str, object]) -> None:
        with self._stable_parent(row) as (target, parent_descriptor):
            current = self._read_operation_target(target, parent_descriptor)
            current_hash = ABSENT if current is None else sha256_bytes(current)
            if current_hash != row["before_hash"]:
                raise RuntimeError(f"before state mismatch for {row['path']}")
            self._before_target_mutation(target)
            if parent_descriptor is None and self._parent_identity(target.parent) != (
                row["parent_device"],
                row["parent_inode"],
            ):
                raise RuntimeError(f"parent identity mismatch for {row['path']}")

            if row["kind"] == "delete":
                if parent_descriptor is None:
                    target.unlink()
                    fsync_directory(target.parent)
                else:
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
            temporary = target.parent / temporary_name
            try:
                if parent_descriptor is None:
                    self._write_new_file(temporary, content, owner_only=False)
                else:
                    self._write_new_file_at(parent_descriptor, temporary_name, content)
                current = self._read_operation_target(target, parent_descriptor)
                current_hash = ABSENT if current is None else sha256_bytes(current)
                if current_hash != row["before_hash"]:
                    raise RuntimeError(f"before state mismatch for {row['path']}")
                if row["kind"] == "create":
                    try:
                        if parent_descriptor is None:
                            os.link(temporary, target)
                        else:
                            os.link(
                                temporary_name,
                                target.name,
                                src_dir_fd=parent_descriptor,
                                dst_dir_fd=parent_descriptor,
                                follow_symlinks=False,
                            )
                    except FileExistsError as exc:
                        raise RuntimeError(f"before state mismatch for {row['path']}") from exc
                    if parent_descriptor is None:
                        temporary.unlink()
                    else:
                        os.unlink(temporary_name, dir_fd=parent_descriptor)
                elif parent_descriptor is None:
                    os.replace(temporary, target)
                else:
                    os.replace(
                        temporary_name,
                        target.name,
                        src_dir_fd=parent_descriptor,
                        dst_dir_fd=parent_descriptor,
                    )
                if parent_descriptor is None:
                    fsync_directory(target.parent)
                else:
                    os.fsync(parent_descriptor)
                actual = self._read_operation_target(target, parent_descriptor)
                actual_hash = ABSENT if actual is None else sha256_bytes(actual)
                if actual_hash != row["after_hash"]:
                    raise RuntimeError(f"after state mismatch for {row['path']}")
            finally:
                if parent_descriptor is None:
                    with contextlib.suppress(FileNotFoundError):
                        temporary.unlink()
                else:
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(temporary_name, dir_fd=parent_descriptor)

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
        plan_bytes = plan_path.read_bytes()
        with self._connect() as database:
            expected = database.execute(
                'SELECT plan_hash FROM "transaction" WHERE id = ?', (record.id,)
            ).fetchone()["plan_hash"]
        if sha256_bytes(plan_bytes) != expected:
            raise RuntimeError("transaction plan hash mismatch")
        plan = json.loads(plan_bytes)
        validate_schema(plan, _SCHEMA)
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
