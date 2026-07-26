"""Own one minimally privileged, bounded LSP child process."""

from __future__ import annotations

import atexit
import json
import math
import os
import queue
import re
import secrets
import shutil
import stat
import subprocess as _subprocess
import threading
import time
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import BinaryIO

import lsp_process_tree as _lsp_process_tree
import windows_workspace as _windows_workspace
from compile_cache import _acl_output_text, _acl_principal, _windows_acl_identity
from lsp_protocol import (
    CancellationToken,
    LspProtocol,
    ProtocolViolation,
    _LocalRequestViolation,
    _ProtocolStartupCleanupError,
)

ProcessTree = _lsp_process_tree.ProcessTree


class _SubprocessFacade:
    Popen = _subprocess.Popen
    PIPE = _subprocess.PIPE
    TimeoutExpired = _subprocess.TimeoutExpired


subprocess = _SubprocessFacade()
_lsp_process_tree.subprocess = subprocess

MAX_STDERR_BYTES = 4 * 1024 * 1024
LSP_ENV_ALLOWLIST = frozenset(
    {
        "COMSPEC",
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "WINDIR",
    }
)

_STDERR_CHUNK_BYTES = 65_537
_STARTUP_WAIT_SECONDS = 2.0
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_STARTUP_FAILED = "startup_failed"
_PROCESS_EXITED = "process_exited"
_MAX_EVIDENCE_BYTES = 4096
_MAX_ACL_OUTPUT_BYTES = 16 * 1024
_HEARTBEAT_SECONDS = 10.0
_LEASE_EXPIRY_SECONDS = 30.0
_IDLE_SECONDS = 300.0
_GRACEFUL_CLEANUP_SECONDS = 2.0
_RECOVERY_RETRY_SECONDS = 0.05
_MAX_PENDING_CHILD_HANDLES = 8
_MAX_PENDING_TEMP_NAMES = 1
_MAX_STARTUP_CLEANUP_OWNERS = 8
_WINDOWS_LEASE_RETRY_ERRORS = frozenset({5, 32, 33})
_WINDOWS_LEASE_RETRY_SECONDS = 0.01
_CLEANUP_STEPS = (
    "tree_termination",
    "tree_release",
    "protocol_stop",
    "generation_joins",
    "heartbeat_join",
    "recovery_join",
    "lease_removal",
    "evidence",
    "scratch_removal",
    "owner_handles",
)
_MAX_RETAINED_CLEANUP_ERRORS = len(_CLEANUP_STEPS)


class StartupCleanupError(RuntimeError):
    """Startup failed and owned cleanup remains incomplete or failed."""

    def __init__(
        self,
        message: str,
        *,
        coordinator: _LifecycleCoordinator | None = None,
    ) -> None:
        super().__init__(message)
        self.coordinator = coordinator

    def retry_cleanup(self, deadline: float) -> None:
        """Retry retained startup ownership within one caller deadline."""
        coordinator = self.coordinator
        if coordinator is None:
            return
        _retry_startup_cleanup(coordinator, deadline)


@dataclass(frozen=True, slots=True)
class _ObjectIdentity:
    device: int
    inode: int
    file_type: int
    reparse_attributes: int


@dataclass(slots=True)
class _OwnerDirectory:
    owner_root: Path
    parent_handle: int
    parent_identity: object
    owner_handle: int | None = None
    owner_identity: object | None = None
    owner_permissions_verified: bool = False
    _close_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _child_handle_lock: threading.RLock = field(
        default_factory=threading.RLock, repr=False
    )
    _pending_child_handles: list[int] = field(default_factory=list, repr=False)
    _pending_temp_names: list[str] = field(default_factory=list, repr=False)
    _lease_expires_monotonic: float | None = field(default=None, repr=False)
    _closed: bool = False

    def _remember_temp_name(self, name: str) -> None:
        if name in self._pending_temp_names:
            return
        if len(self._pending_temp_names) >= _MAX_PENDING_TEMP_NAMES:
            raise RuntimeError("LSP pending temporary name bound was exhausted")
        self._pending_temp_names.append(name)

    def _forget_temp_name(self, name: str) -> None:
        if name in self._pending_temp_names:
            self._pending_temp_names.remove(name)

    def _retry_pending_child_handles(self) -> None:
        first_error: BaseException | None = None
        for handle in tuple(self._pending_child_handles):
            try:
                _windows_workspace.close_handle(handle)
            except BaseException as error:
                if first_error is None:
                    first_error = error
            else:
                self._pending_child_handles.remove(handle)
        if first_error is not None:
            raise first_error

    def _retry_pending_temp_names(self) -> None:
        with self._child_handle_lock:
            if os.name == "nt":
                self._retry_pending_child_handles()
            if self._pending_temp_names and self.owner_handle is None:
                raise RuntimeError("LSP pending temporary owner is closed")
            for name in tuple(self._pending_temp_names):
                if os.name == "posix":
                    try:
                        os.unlink(name, dir_fd=self.owner_handle)
                    except FileNotFoundError:
                        pass
                    else:
                        self._forget_temp_name(name)
                        continue
                    self._forget_temp_name(name)
                    continue
                if os.name != "nt":
                    raise RuntimeError(
                        "LSP temporary cleanup is unsupported on this platform"
                    )
                try:
                    handle = _windows_workspace.open_deletable_file(
                        self.owner_handle, name
                    )
                except FileNotFoundError:
                    self._forget_temp_name(name)
                    continue
                delete_error: BaseException | None = None
                try:
                    _windows_workspace.delete_handle(handle)
                except BaseException as error:
                    delete_error = error
                try:
                    self._close_child_handle(handle)
                except BaseException as close_error:
                    if delete_error is not None:
                        raise delete_error from close_error
                    raise
                if delete_error is not None:
                    raise delete_error
                self._forget_temp_name(name)

    def _finish_windows_temporary(
        self,
        name: str,
        handle: int,
        *,
        moved: bool,
        operation_error: BaseException | None,
    ) -> None:
        cleanup_error: BaseException | None = None
        delete_marked = False
        if operation_error is not None and not moved:
            try:
                _windows_workspace.delete_handle(handle)
            except BaseException as error:
                cleanup_error = error
            else:
                delete_marked = True
        if moved:
            self._forget_temp_name(name)
        try:
            self._close_child_handle(handle)
        except BaseException as close_error:
            if cleanup_error is not None:
                raise close_error from cleanup_error
            if operation_error is not None:
                raise close_error from operation_error
            raise
        if delete_marked:
            self._forget_temp_name(name)
        if cleanup_error is not None:
            raise cleanup_error from operation_error
        if operation_error is not None:
            raise operation_error

    def _close_child_handle(self, handle: int) -> None:
        try:
            _windows_workspace.close_handle(handle)
        except BaseException:
            if handle not in self._pending_child_handles:
                if len(self._pending_child_handles) >= _MAX_PENDING_CHILD_HANDLES:
                    raise RuntimeError("LSP pending child handle bound was exhausted")
                self._pending_child_handles.append(handle)
            raise

    def _close_child_handles(self, *handles: int | None) -> None:
        first_error: BaseException | None = None
        for handle in handles:
            if handle is None:
                continue
            try:
                self._close_child_handle(handle)
            except BaseException as error:
                if first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error

    @classmethod
    def open(cls, owner_root: Path) -> _OwnerDirectory:
        if os.name == "posix":
            if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
                raise RuntimeError("LSP ownership requires POSIX no-follow directory APIs")
            try:
                descriptor = os.open(
                    owner_root.parent,
                    os.O_RDONLY
                    | os.O_DIRECTORY
                    | os.O_NOFOLLOW
                    | getattr(os, "O_CLOEXEC", 0),
                )
            except NotADirectoryError as exc:
                raise ValueError(
                    "owner_root parent must be a no-follow directory"
                ) from exc
            try:
                identity = _identity_from_stat(os.fstat(descriptor))
                _require_directory_identity(identity, "owner_root parent")
            except BaseException:
                os.close(descriptor)
                raise
            return cls(owner_root, descriptor, identity)
        if os.name == "nt":
            handle = _windows_workspace.open_directory_path(owner_root.parent)
            try:
                identity = _windows_workspace.identity(handle, directory=True)
            except BaseException:
                _windows_workspace.close_handle(handle)
                raise
            return cls(owner_root, handle, identity)
        raise RuntimeError("LSP owner directories are unsupported on this platform")

    def create(self, deadline: float) -> None:
        if os.name == "posix":
            os.mkdir(self.owner_root.name, 0o700, dir_fd=self.parent_handle)
            owner = os.open(
                self.owner_root.name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                dir_fd=self.parent_handle,
            )
            self.owner_handle = owner
            self.owner_identity = _identity_from_stat(os.fstat(owner))
            _require_directory_identity(self.owner_identity, "LSP owner root")
            os.fchmod(owner, 0o700)
            _verify_descriptor(owner, self.owner_identity, mode=0o700, directory=True)
            self.owner_permissions_verified = True
            os.mkdir("cancellation", 0o700, dir_fd=owner)
            cancellation = os.open(
                "cancellation",
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                dir_fd=owner,
            )
            try:
                cancellation_identity = _identity_from_stat(os.fstat(cancellation))
                _require_directory_identity(cancellation_identity, "LSP cancellation root")
                os.fchmod(cancellation, 0o700)
                _verify_descriptor(
                    cancellation, cancellation_identity, mode=0o700, directory=True
                )
                os.fsync(cancellation)
            finally:
                os.close(cancellation)
            os.fsync(owner)
            os.fsync(self.parent_handle)
            return

        owner = _windows_workspace.create_writable_directory(
            self.parent_handle, self.owner_root.name
        )
        self.owner_handle = owner
        self.owner_identity = _windows_workspace.identity(owner, directory=True)
        _secure_windows_owner_root(self.owner_root, deadline)
        if _windows_workspace.identity(owner, directory=True) != self.owner_identity:
            raise PermissionError("LSP owner root identity changed during ACL setup")
        self.owner_permissions_verified = True
        with self._child_handle_lock:
            self._retry_pending_child_handles()
            cancellation = _windows_workspace.create_directory(owner, "cancellation")
            try:
                cancellation_identity = _windows_workspace.identity(
                    cancellation, directory=True
                )
                if (
                    _windows_workspace.identity(cancellation, directory=True)
                    != cancellation_identity
                ):
                    raise PermissionError(
                        "LSP cancellation root identity changed during ACL setup"
                    )
            finally:
                self._close_child_handle(cancellation)

    def write_record(
        self,
        name: str,
        record: Mapping[str, object],
    ) -> None:
        if name not in {"owner.json", "failure.json"} or self.owner_handle is None:
            raise ValueError("LSP evidence name or owner handle is invalid")
        payload = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(payload) > _MAX_EVIDENCE_BYTES:
            raise ValueError("LSP evidence record exceeds its byte bound")
        with self._child_handle_lock:
            self._retry_pending_temp_names()
            temporary = f".evidence-{secrets.token_hex(8)}.tmp"
            if os.name == "posix":
                try:
                    descriptor = os.open(
                        temporary,
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | os.O_NOFOLLOW
                        | getattr(os, "O_CLOEXEC", 0),
                        0o600,
                        dir_fd=self.owner_handle,
                    )
                    self._remember_temp_name(temporary)
                    try:
                        identity = _identity_from_stat(os.fstat(descriptor))
                        _require_file_identity(identity, "LSP evidence record")
                        _write_all_descriptor(descriptor, payload)
                        os.fchmod(descriptor, 0o600)
                        os.fsync(descriptor)
                        _verify_descriptor(
                            descriptor, identity, mode=0o600, directory=False
                        )
                    finally:
                        os.close(descriptor)
                    os.link(
                        temporary,
                        name,
                        src_dir_fd=self.owner_handle,
                        dst_dir_fd=self.owner_handle,
                        follow_symlinks=False,
                    )
                    os.unlink(temporary, dir_fd=self.owner_handle)
                    self._forget_temp_name(temporary)
                    self.sync_directory()
                except BaseException as operation_error:
                    if temporary in self._pending_temp_names:
                        try:
                            self._retry_pending_temp_names()
                        except BaseException as cleanup_error:
                            raise cleanup_error from operation_error
                    raise
                return

            if not self.owner_permissions_verified:
                raise PermissionError(
                    "LSP owner ACL was not verified before evidence creation"
                )
            self._remember_temp_name(temporary)
            try:
                handle = _windows_workspace.create_file(self.owner_handle, temporary)
            except BaseException as operation_error:
                try:
                    self._retry_pending_temp_names()
                except BaseException as cleanup_error:
                    raise cleanup_error from operation_error
                raise
            published = False
            operation_error: BaseException | None = None
            try:
                identity = _windows_workspace.identity(handle, directory=False)
                _windows_workspace.write_all(
                    handle, payload, chunk_bytes=_MAX_EVIDENCE_BYTES
                )
                _windows_workspace.flush_file(handle)
                if _windows_workspace.identity(handle, directory=False) != identity:
                    raise PermissionError("LSP evidence identity changed during write")
                _windows_workspace.publish_file(handle, self.owner_handle, name)
                published = True
                self.sync_directory()
            except BaseException as error:
                operation_error = error
            self._finish_windows_temporary(
                temporary,
                handle,
                moved=published,
                operation_error=operation_error,
            )
        if not published:
            raise OSError("LSP evidence publication did not complete")

    def write_lease(
        self,
        record: Mapping[str, object],
        *,
        deadline: float | None = None,
        expires_monotonic: float | None = None,
        retry_stop: threading.Event | None = None,
    ) -> None:
        if self.owner_handle is None:
            raise RuntimeError("LSP owner directory is closed")
        retry_deadline = (
            time.monotonic() if deadline is None else _validated_deadline(deadline)
        )
        if self._lease_expires_monotonic is not None:
            retry_deadline = min(retry_deadline, self._lease_expires_monotonic)
        if expires_monotonic is not None:
            expires_monotonic = _validated_deadline(expires_monotonic)
        payload = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(payload) > _MAX_EVIDENCE_BYTES:
            raise ValueError("LSP lease exceeds its byte bound")
        with self._child_handle_lock:
            self._retry_pending_temp_names()
            temporary = f".lease-{secrets.token_hex(8)}.tmp"
            if os.name == "posix":
                try:
                    descriptor = os.open(
                        temporary,
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | os.O_NOFOLLOW
                        | getattr(os, "O_CLOEXEC", 0),
                        0o600,
                        dir_fd=self.owner_handle,
                    )
                    self._remember_temp_name(temporary)
                    try:
                        identity = _identity_from_stat(os.fstat(descriptor))
                        _require_file_identity(identity, "LSP lease")
                        _write_all_descriptor(descriptor, payload)
                        os.fchmod(descriptor, 0o600)
                        os.fsync(descriptor)
                        _verify_descriptor(
                            descriptor, identity, mode=0o600, directory=False
                        )
                    finally:
                        os.close(descriptor)
                    os.replace(
                        temporary,
                        "lease.json",
                        src_dir_fd=self.owner_handle,
                        dst_dir_fd=self.owner_handle,
                    )
                    self._forget_temp_name(temporary)
                    self.sync_directory()
                    self._lease_expires_monotonic = expires_monotonic
                except BaseException as operation_error:
                    if temporary in self._pending_temp_names:
                        try:
                            self._retry_pending_temp_names()
                        except BaseException as cleanup_error:
                            raise cleanup_error from operation_error
                    raise
                return

            self._remember_temp_name(temporary)
            try:
                handle = _windows_workspace.create_file(self.owner_handle, temporary)
            except BaseException as operation_error:
                try:
                    self._retry_pending_temp_names()
                except BaseException as cleanup_error:
                    raise cleanup_error from operation_error
                raise
            replaced = False
            operation_error: BaseException | None = None
            try:
                _windows_workspace.write_all(
                    handle, payload, chunk_bytes=_MAX_EVIDENCE_BYTES
                )
                _windows_workspace.flush_file(handle)
                while True:
                    try:
                        _windows_workspace.replace_file(
                            handle, self.owner_handle, "lease.json"
                        )
                    except OSError as error:
                        code = getattr(error, "winerror", None) or error.errno
                        remaining = retry_deadline - time.monotonic()
                        if (
                            code not in _WINDOWS_LEASE_RETRY_ERRORS
                            or retry_stop is None
                            or remaining <= 0
                            or retry_stop.wait(
                                min(_WINDOWS_LEASE_RETRY_SECONDS, remaining)
                            )
                        ):
                            raise
                    else:
                        replaced = True
                        self._lease_expires_monotonic = expires_monotonic
                        break
            except BaseException as error:
                operation_error = error
            self._finish_windows_temporary(
                temporary,
                handle,
                moved=replaced,
                operation_error=operation_error,
            )
            self.sync_directory()

    def sync_directory(self) -> None:
        handle = self.owner_handle
        if handle is None:
            raise RuntimeError("LSP owner directory is closed")
        if os.name == "posix":
            os.fsync(handle)
            return
        if os.name == "nt":
            if not _windows_workspace.flush_directory(handle):
                raise OSError("LSP owner directory durability flush failed")
            return
        raise RuntimeError("LSP owner directories are unsupported on this platform")

    def remove_lease(self) -> None:
        if self.owner_handle is None:
            if self._pending_temp_names:
                raise RuntimeError("LSP pending temporary owner is closed")
            return
        self._retry_pending_temp_names()
        if os.name == "posix":
            try:
                os.unlink("lease.json", dir_fd=self.owner_handle)
                os.fsync(self.owner_handle)
            except FileNotFoundError:
                pass
            self._lease_expires_monotonic = None
            return
        with self._child_handle_lock:
            self._retry_pending_child_handles()
            try:
                handle = _windows_workspace.open_deletable_file(
                    self.owner_handle, "lease.json"
                )
            except FileNotFoundError:
                self._lease_expires_monotonic = None
                return
            try:
                _windows_workspace.delete_handle(handle)
            finally:
                self._close_child_handle(handle)
            self._lease_expires_monotonic = None

    def read_record(self, name: str) -> dict[str, object]:
        if name not in {"owner.json", "failure.json"} or self.owner_handle is None:
            raise ValueError("LSP evidence name or owner handle is invalid")
        if os.name == "posix":
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                dir_fd=self.owner_handle,
            )
            try:
                identity = _identity_from_stat(os.fstat(descriptor))
                _require_file_identity(identity, "LSP evidence record")
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = os.read(descriptor, min(4096, _MAX_EVIDENCE_BYTES + 1 - total))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > _MAX_EVIDENCE_BYTES:
                        raise ValueError("LSP evidence record exceeds its byte bound")
                payload = b"".join(chunks)
            finally:
                os.close(descriptor)
        else:
            with self._child_handle_lock:
                self._retry_pending_child_handles()
                handle = _windows_workspace.open_file(self.owner_handle, name)
                try:
                    payload = b"".join(
                        _windows_workspace.read_chunks(
                            handle,
                            chunk_bytes=4096,
                            max_bytes=_MAX_EVIDENCE_BYTES,
                        )
                    )
                finally:
                    self._close_child_handle(handle)
        try:
            record = json.loads(payload.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("LSP evidence record is not canonical JSON") from exc
        if not isinstance(record, dict):
            raise ValueError("LSP evidence record is not a JSON object")
        canonical = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if canonical != payload:
            raise ValueError("LSP evidence record is not canonical JSON")
        return record

    def remove_success_scratch(self) -> None:
        self._retry_pending_temp_names()
        if os.name == "posix":
            if self.owner_handle is None:
                return
            for name in ("owner.json", "failure.json"):
                try:
                    os.unlink(name, dir_fd=self.owner_handle)
                except FileNotFoundError:
                    pass
            try:
                os.rmdir("cancellation", dir_fd=self.owner_handle)
            except FileNotFoundError:
                pass
            os.fsync(self.owner_handle)
            try:
                named_identity = _identity_from_stat(
                    os.stat(
                        self.owner_root.name,
                        dir_fd=self.parent_handle,
                        follow_symlinks=False,
                    )
                )
            except FileNotFoundError:
                named_identity = None
            if named_identity is not None:
                if named_identity != self.owner_identity:
                    raise PermissionError(
                        "LSP owner root identity changed before deletion"
                    )
                os.rmdir(self.owner_root.name, dir_fd=self.parent_handle)
            os.fsync(self.parent_handle)
            return
        with self._child_handle_lock:
            self._retry_pending_child_handles()
            original = self.owner_handle
            expected = self.owner_identity
            if original is not None:
                for name in ("owner.json", "failure.json"):
                    try:
                        handle = _windows_workspace.open_deletable_file(original, name)
                    except FileNotFoundError:
                        continue
                    try:
                        _windows_workspace.delete_handle(handle)
                    finally:
                        self._close_child_handle(handle)
                try:
                    cancellation = _windows_workspace.open_deletable_directory(
                        original, "cancellation"
                    )
                except FileNotFoundError:
                    cancellation = None
                if cancellation is not None:
                    try:
                        _windows_workspace.delete_handle(cancellation)
                    finally:
                        self._close_child_handle(cancellation)
                _windows_workspace.close_handle(original)
                self.owner_handle = None
            try:
                owner = _windows_workspace.open_deletable_directory(
                    self.parent_handle, self.owner_root.name
                )
            except FileNotFoundError:
                return
            try:
                if _windows_workspace.identity(owner, directory=True) != expected:
                    raise PermissionError("LSP owner root identity changed before deletion")
                _windows_workspace.delete_handle(owner)
            finally:
                self._close_child_handle(owner)

    def verify_lexical_identity(self) -> None:
        if self.owner_handle is None or self.owner_identity is None:
            raise RuntimeError("LSP owner directory was not created")
        if os.name == "posix":
            if _current_identity(self.owner_root.parent) != self.parent_identity:
                raise RuntimeError("owner_root parent identity changed during startup")
            if _current_identity(self.owner_root) != self.owner_identity:
                raise RuntimeError("LSP owner root identity changed during startup")
            _verify_descriptor(
                self.owner_handle, self.owner_identity, mode=0o700, directory=True
            )
            return

        with self._child_handle_lock:
            self._retry_pending_child_handles()
            parent = _windows_workspace.open_directory_path(self.owner_root.parent)
            named: int | None = None
            try:
                if (
                    _windows_workspace.identity(parent, directory=True)
                    != self.parent_identity
                ):
                    raise RuntimeError("owner_root parent identity changed during startup")
                named = _windows_workspace.open_directory(parent, self.owner_root.name)
                if (
                    _windows_workspace.identity(named, directory=True)
                    != self.owner_identity
                ):
                    raise RuntimeError("LSP owner root identity changed during startup")
                if (
                    _windows_workspace.identity(self.owner_handle, directory=True)
                    != self.owner_identity
                ):
                    raise RuntimeError(
                        "held LSP owner root identity changed during startup"
                    )
            finally:
                self._close_child_handles(named, parent)

    def close(self) -> None:
        with self._child_handle_lock:
            self._retry_pending_temp_names()
            with self._close_lock:
                if self._closed:
                    return
                owner = self.owner_handle
                parent = self.parent_handle
            first_error: BaseException | None = None
            for label, handle in (("owner", owner), ("parent", parent)):
                if handle is None or handle < 0:
                    continue
                try:
                    if os.name == "posix":
                        os.close(handle)
                    elif os.name == "nt":
                        _windows_workspace.close_handle(handle)
                except BaseException as exc:
                    if first_error is None:
                        first_error = exc
                else:
                    with self._close_lock:
                        if label == "owner" and self.owner_handle == handle:
                            self.owner_handle = None
                        elif label == "parent" and self.parent_handle == handle:
                            self.parent_handle = -1
            with self._close_lock:
                self._closed = self.owner_handle is None and self.parent_handle < 0
            if first_error is not None:
                raise first_error


class ProcessState(str, Enum):
    PROCESS_RUNNING = "process_running"
    PROTOCOL_INITIALIZED = "protocol_initialized"
    WORKSPACE_READY = "workspace_ready"
    DEGRADED = "degraded"
    FAILED = "failed"


class _LifecyclePhase(str, Enum):
    STARTING = "starting"
    RUNNING = "running"
    RECOVERY_PENDING = "recovery_pending"
    RESTARTING = "restarting"
    STOPPING_SUCCESS = "stopping_success"
    STOPPING_FAILURE = "stopping_failure"
    CLEANUP_PENDING = "cleanup_pending"
    STOPPED_SUCCESS = "stopped_success"
    STOPPED_FAILURE = "stopped_failure"


@dataclass(frozen=True, slots=True)
class _FailureIntent:
    generation_nonce: str | None
    reason: str
    owner_fatal: bool
    observed_monotonic: float


@dataclass(frozen=True, slots=True)
class _FailureEvidenceIdentity:
    code: str
    owner_nonce: str
    generation_nonce: str
    pid: int | None


@dataclass(slots=True)
class _Generation:
    nonce: str
    tree: ProcessTree | None
    process: subprocess.Popen[bytes] | None
    server_pid: int | None = None
    windows_job: int | None = None
    protocol: LspProtocol | None = None
    stderr: deque[bytes] = field(default_factory=deque)
    stderr_size: list[int] = field(default_factory=lambda: [0])
    stderr_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    stderr_thread: threading.Thread | None = None
    exit_thread: threading.Thread | None = None
    expected_exit: threading.Event = field(default_factory=threading.Event, repr=False)
    failure_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    failure_queued: bool = field(default=False, repr=False)
    _exit_observed: bool = field(default=False, init=False, repr=False)

    @property
    def released(self) -> bool:
        return (
            self.tree is None
            and self.process is None
            and self.windows_job is None
            and self.protocol is None
            and self.stderr_thread is None
            and self.exit_thread is None
        )


@dataclass(frozen=True, slots=True)
class _CleanupError:
    step: str
    error_type: str
    error_code: int | None


def _sanitized_cleanup_error(step: str, error: BaseException) -> _CleanupError:
    error_type = "Exception"
    for category in (
        TimeoutError,
        PermissionError,
        FileNotFoundError,
        OSError,
        ValueError,
        RuntimeError,
    ):
        if isinstance(error, category):
            error_type = category.__name__
            break
    code: int | None = None
    for attribute in ("winerror", "errno"):
        try:
            candidate = getattr(error, attribute, None)
        except BaseException:
            continue
        if (
            isinstance(candidate, int)
            and not isinstance(candidate, bool)
            and 0 <= candidate <= 0xFFFFFFFF
        ):
            code = candidate
            break
    return _CleanupError(step, error_type, code)


@dataclass(slots=True)
class _CleanupResult:
    tree_termination: str = "pending"
    tree_release: str = "pending"
    protocol_stop: str = "pending"
    generation_joins: str = "pending"
    heartbeat_join: str = "pending"
    recovery_join: str = "pending"
    lease_removal: str = "pending"
    evidence: str = "pending"
    scratch_removal: str = "pending"
    owner_handles: str = "pending"
    errors: list[_CleanupError] = field(default_factory=list)
    ownership_pending: bool = True

    def failed(self, step: str, error: BaseException) -> None:
        if step not in _CLEANUP_STEPS:
            raise ValueError("unknown LSP cleanup step")
        setattr(self, step, "failed")
        self.errors[:] = [item for item in self.errors if item.step != step]
        self.errors.append(_sanitized_cleanup_error(step, error))

    def succeeded(self, step: str, status: str = "success") -> None:
        if step not in _CLEANUP_STEPS or status not in {"success", "not_applicable"}:
            raise ValueError("invalid LSP cleanup resolution")
        setattr(self, step, status)
        self.errors[:] = [item for item in self.errors if item.step != step]


@dataclass(slots=True)
class _LifecycleCoordinator:
    owner_directory: _OwnerDirectory | None
    startup_generation_nonce: str | None = None
    phase: _LifecyclePhase = _LifecyclePhase.STARTING
    active: _Generation | None = None
    candidate: _Generation | None = None
    retired: list[_Generation] = field(default_factory=list)
    terminal_outcome: str | None = None
    terminal_code: str | None = None
    failure_evidence_identity: _FailureEvidenceIdentity | None = None
    mandatory_failure_intent: _FailureEvidenceIdentity | None = None
    background_cleanup_error: _CleanupError | None = field(default=None, repr=False)
    background_error_lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False
    )
    cleanup_result: _CleanupResult = field(default_factory=_CleanupResult)
    recovery_attempted: bool = False
    recovery_request_nonce: str | None = None
    startup_complete: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    condition: threading.Condition = field(init=False, repr=False)
    driver: threading.RLock = field(default_factory=threading.RLock, repr=False)
    lease_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    terminal_intent_lock: threading.RLock = field(
        default_factory=threading.RLock, repr=False
    )
    terminal_state_lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False
    )
    pending_failure_intents: int = 0
    pending_failure_code: str | None = None
    success_committed: bool = False
    cleanup_started: threading.Event = field(default_factory=threading.Event, repr=False)
    failure_queue: queue.SimpleQueue[_FailureIntent] = field(
        default_factory=queue.SimpleQueue, repr=False
    )
    recovery_request_pending: threading.Event = field(
        default_factory=threading.Event, repr=False
    )
    recovery_wake: threading.Event = field(default_factory=threading.Event, repr=False)
    recovery_stop: threading.Event = field(default_factory=threading.Event, repr=False)
    recovery_thread: threading.Thread | None = None
    heartbeat_stop: threading.Event = field(default_factory=threading.Event, repr=False)
    heartbeat_wake: threading.Event = field(default_factory=threading.Event, repr=False)
    heartbeat_thread: threading.Thread | None = None
    seen_failures: set[tuple[str | None, bool]] = field(default_factory=set, repr=False)

    def __post_init__(self) -> None:
        self.condition = threading.Condition(self.lock)


_STARTUP_CLEANUP_LOCK = threading.Lock()
_STARTUP_CLEANUPS: dict[int, _LifecycleCoordinator] = {}


def _register_startup_cleanup(coordinator: _LifecycleCoordinator) -> None:
    with _STARTUP_CLEANUP_LOCK:
        key = id(coordinator)
        if key in _STARTUP_CLEANUPS:
            return
        if len(_STARTUP_CLEANUPS) >= _MAX_STARTUP_CLEANUP_OWNERS:
            raise RuntimeError("LSP startup cleanup registry bound was exhausted")
        _STARTUP_CLEANUPS[key] = coordinator


def _unregister_startup_cleanup(coordinator: _LifecycleCoordinator) -> None:
    with _STARTUP_CLEANUP_LOCK:
        _STARTUP_CLEANUPS.pop(id(coordinator), None)


def _pending_startup_cleanup_snapshot() -> tuple[_LifecycleCoordinator, ...]:
    with _STARTUP_CLEANUP_LOCK:
        return tuple(_STARTUP_CLEANUPS.values())


def _retry_startup_cleanup(
    coordinator: _LifecycleCoordinator,
    deadline: float,
) -> None:
    deadline = _validated_deadline(deadline)
    _mark_terminal_failure(None, coordinator, _STARTUP_FAILED, deadline)
    errors = _drive_cleanup(
        None,
        deadline,
        terminal=True,
        failure_code=_STARTUP_FAILED,
        coordinator_override=coordinator,
    )
    if errors:
        raise errors[0]
    if _coordinator_has_ownership(coordinator):
        raise TimeoutError("LSP startup cleanup retains retryable ownership")
    _unregister_startup_cleanup(coordinator)


def _atexit_cleanup_startups() -> None:
    deadline = time.monotonic() + _GRACEFUL_CLEANUP_SECONDS
    for coordinator in _pending_startup_cleanup_snapshot():
        try:
            _retry_startup_cleanup(coordinator, deadline)
        except BaseException:
            pass


def lsp_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build a new, canonical environment containing only required process values."""
    values = os.environ if source is None else source
    for name, value in values.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise TypeError("environment names and values must be strings")
        if "\0" in name or "\0" in value:
            raise ValueError("environment names and values must not contain NUL")
    if os.name == "nt":
        system_root = values.get("SYSTEMROOT")
        if (
            not system_root
            or not Path(system_root).is_absolute()
            or not Path(system_root).is_dir()
        ):
            raise ValueError("SYSTEMROOT must be an inherited existing directory on Windows")
    return {name: values[name] for name in sorted(LSP_ENV_ALLOWLIST) if name in values}


@dataclass(slots=True)
class LspProcess:
    process: subprocess.Popen[bytes]
    protocol: LspProtocol
    owner_root: Path
    owner_nonce: str
    generation_nonce: str
    state: ProcessState
    started_monotonic: float
    last_used_monotonic: float
    restart_count: int = 0
    _coordinator: _LifecycleCoordinator = field(init=False, repr=False)
    _stderr_projection: deque[bytes] = field(default_factory=deque, init=False, repr=False)
    _stderr_projection_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )
    _command: tuple[str, ...] = field(default=(), init=False, repr=False)
    _cwd: Path | None = field(default=None, init=False, repr=False)
    _environment: dict[str, str] = field(default_factory=dict, init=False, repr=False)

    @classmethod
    def start(
        cls, command: Sequence[str], *, cwd: Path, owner_root: Path
    ) -> LspProcess:
        return _start_lsp_process(cls, command, cwd=cwd, owner_root=owner_root)

    def request(
        self,
        method: str,
        params: object,
        *,
        deadline: float,
        cancellation: CancellationToken | None = None,
    ) -> object:
        return _request_lsp_process(
            self,
            method,
            params,
            deadline=deadline,
            cancellation=cancellation,
        )

    def shutdown(self, deadline: float) -> None:
        _shutdown_lsp_process(self, deadline)

    def cancel_all(self, reason: str) -> None:
        _cancel_all_lsp_process(self, reason)

    def restart(self, deadline: float) -> None:
        _restart_lsp_process(self, deadline)

    def close(self, deadline: float) -> None:
        _shutdown_lsp_process(self, deadline)

    def idle_expired(self, now: float) -> bool:
        return _idle_expired_lsp_process(self, now)

    def stderr_bytes(self) -> bytes:
        with self._stderr_projection_lock:
            return b"".join(self._stderr_projection)

    def wait_for_exit(self, deadline: float) -> int:
        return _wait_for_lsp_exit(self, deadline)

    def _terminal_failure(self, code: str, deadline: float) -> None:
        _terminal_failure_lsp_process(self, code, deadline)

    def _atexit_close(self) -> None:
        try:
            self.close(time.monotonic() + _GRACEFUL_CLEANUP_SECONDS)
        except BaseException:
            pass

    def _stop_heartbeat(self, deadline: float) -> None:
        try:
            _stop_heartbeat_owner(self._coordinator, _validated_deadline(deadline))
        except TimeoutError as exc:
            raise TimeoutError("LSP heartbeat thread did not stop before deadline") from exc

    def _projected_generation(self) -> _Generation | None:
        coordinator = self._coordinator
        if coordinator.active is not None:
            return coordinator.active
        if coordinator.candidate is not None:
            return coordinator.candidate
        for generation in coordinator.retired:
            if generation.nonce == self.generation_nonce:
                return generation
        return None

    @property
    def _tree(self) -> ProcessTree | None:
        generation = self._projected_generation()
        return generation.tree if generation is not None else None

    @property
    def _owner_directory(self) -> _OwnerDirectory | None:
        return self._coordinator.owner_directory

    @property
    def _stderr_thread(self) -> threading.Thread | None:
        generation = self._projected_generation()
        return generation.stderr_thread if generation is not None else None

    @property
    def _exit_thread(self) -> threading.Thread | None:
        generation = self._projected_generation()
        return generation.exit_thread if generation is not None else None

    @property
    def _heartbeat_thread(self) -> threading.Thread | None:
        return self._coordinator.heartbeat_thread

    @property
    def _recovery_thread(self) -> threading.Thread | None:
        return self._coordinator.recovery_thread


def _acquire_lifecycle(
    coordinator: _LifecycleCoordinator,
    deadline: float,
    *,
    allow_expired: bool = False,
) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        acquired = allow_expired and coordinator.lock.acquire(blocking=False)
    else:
        acquired = coordinator.lock.acquire(timeout=remaining)
    if not acquired:
        raise TimeoutError("LSP lifecycle transition lock deadline expired")


def _release_lifecycle(coordinator: _LifecycleCoordinator) -> None:
    coordinator.lock.release()


def _acquire_driver(
    coordinator: _LifecycleCoordinator,
    deadline: float,
    *,
    allow_expired: bool = False,
) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        acquired = allow_expired and coordinator.driver.acquire(blocking=False)
    else:
        acquired = coordinator.driver.acquire(timeout=remaining)
    if not acquired:
        raise TimeoutError("LSP lifecycle driver deadline expired")


def _release_driver(coordinator: _LifecycleCoordinator) -> None:
    coordinator.driver.release()


def _acquire_lease(
    coordinator: _LifecycleCoordinator,
    deadline: float,
    *,
    allow_expired: bool = False,
) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        acquired = allow_expired and coordinator.lease_lock.acquire(blocking=False)
    else:
        acquired = coordinator.lease_lock.acquire(timeout=remaining)
    if not acquired:
        raise TimeoutError("LSP lease lock deadline expired")


def _release_lease(coordinator: _LifecycleCoordinator) -> None:
    coordinator.lease_lock.release()


def _acquire_terminal_intent(
    coordinator: _LifecycleCoordinator,
    deadline: float,
    *,
    allow_expired: bool = False,
) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        acquired = allow_expired and coordinator.terminal_intent_lock.acquire(
            blocking=False
        )
    else:
        acquired = coordinator.terminal_intent_lock.acquire(timeout=remaining)
    if not acquired:
        raise TimeoutError("LSP terminal intent lock deadline expired")


def _release_terminal_intent(coordinator: _LifecycleCoordinator) -> None:
    coordinator.terminal_intent_lock.release()


def _notify_lifecycle_locked(coordinator: _LifecycleCoordinator) -> None:
    coordinator.condition.notify_all()


def _require_startup_deadline(deadline: float, stage: str) -> None:
    if time.monotonic() >= deadline:
        raise TimeoutError(f"LSP startup deadline expired after {stage}")


def _assign_candidate(
    coordinator: _LifecycleCoordinator,
    generation: _Generation,
    deadline: float,
) -> None:
    _acquire_lifecycle(coordinator, deadline)
    try:
        if coordinator.candidate is not None and coordinator.candidate is not generation:
            raise RuntimeError("another LSP candidate already owns lifecycle resources")
        coordinator.candidate = generation
        _notify_lifecycle_locked(coordinator)
    finally:
        _release_lifecycle(coordinator)


def _queue_generation_failure(
    coordinator: _LifecycleCoordinator,
    generation: _Generation,
    reason: str,
) -> bool:
    return _commit_generation_failure(
        coordinator,
        generation,
        reason,
        exit_observed=False,
    )


def _commit_generation_failure(
    coordinator: _LifecycleCoordinator,
    generation: _Generation,
    reason: str,
    *,
    exit_observed: bool,
) -> bool:
    with generation.failure_lock:
        if exit_observed:
            generation._exit_observed = True
        if generation.expected_exit.is_set() or generation.failure_queued:
            return False
        intent = _FailureIntent(generation.nonce, reason, False, time.monotonic())
        if not _enqueue_failure_intent(coordinator, intent):
            return False
        generation.failure_queued = True
    return True


def _mark_generation_expected_exit(generation: _Generation) -> bool:
    with generation.failure_lock:
        if generation._exit_observed or generation.failure_queued:
            return False
        generation.expected_exit.set()
        return True


def _enqueue_failure_intent(
    coordinator: _LifecycleCoordinator, intent: _FailureIntent
) -> bool:
    with coordinator.terminal_state_lock:
        if coordinator.success_committed:
            return False
        coordinator.pending_failure_intents += 1
        if coordinator.pending_failure_code is None:
            coordinator.pending_failure_code = (
                "heartbeat_failed" if intent.owner_fatal else _PROCESS_EXITED
            )
        coordinator.failure_queue.put(intent)
    coordinator.recovery_wake.set()
    return True


def _acknowledge_failure_intent(coordinator: _LifecycleCoordinator) -> None:
    with coordinator.terminal_state_lock:
        if coordinator.pending_failure_intents <= 0:
            raise RuntimeError("LSP failure intent acknowledgement underflow")
        coordinator.pending_failure_intents -= 1
        if coordinator.pending_failure_intents == 0:
            coordinator.pending_failure_code = None


def _queue_owner_failure(
    coordinator: _LifecycleCoordinator, reason: str
) -> bool:
    return _enqueue_failure_intent(
        coordinator,
        _FailureIntent(None, reason, True, time.monotonic()),
    )


def _prepare_generation(
    coordinator: _LifecycleCoordinator,
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    deadline: float,
    generation_nonce: str,
    owner_record: Mapping[str, object] | None = None,
) -> _Generation:
    generation = _Generation(generation_nonce, None, None)
    _assign_candidate(coordinator, generation, deadline)
    try:
        tree = ProcessTree._spawn_with_deadline(
            command,
            cwd=cwd,
            env=environment,
            deadline=deadline,
        )
    except _lsp_process_tree._ProcessTreeSpawnError as error:
        generation.tree = error.tree
        if error.tree is not None:
            generation.process = error.tree.process
        else:
            generation.windows_job = error.windows_job
        raise
    generation.tree = tree
    generation.process = tree.process
    process = tree.process
    generation.server_pid = process.pid
    _require_startup_deadline(deadline, "process-tree start")
    if process.stdin is None or process.stdout is None or process.stderr is None:
        raise RuntimeError("LSP process pipes were not created")

    stderr_thread = threading.Thread(
        target=_drain_stderr,
        args=(
            process.stderr,
            generation.stderr,
            generation.stderr_size,
            generation.stderr_lock,
        ),
        name=f"lsp-stderr-{generation_nonce}",
        daemon=True,
    )
    generation.stderr_thread = stderr_thread
    _require_startup_deadline(deadline, "stderr thread preparation")
    stderr_thread.start()
    _require_startup_deadline(deadline, "stderr thread start")

    owner = coordinator.owner_directory
    if owner is None:
        raise RuntimeError("LSP owner directory is unavailable")
    owner.verify_lexical_identity()
    if owner_record is not None:
        published_owner_record = dict(owner_record)
        published_owner_record["owner_pid"] = process.pid
        _write_owner_record(owner, published_owner_record)
        owner.verify_lexical_identity()

    try:
        protocol = LspProtocol(
            process.stdout,
            process.stdin,
            generation_nonce,
            fatal_callback=lambda reason: _queue_generation_failure(
                coordinator, generation, reason
            ),
            _startup_deadline=deadline,
            _drain_wake=coordinator.recovery_wake,
        )
    except _ProtocolStartupCleanupError as error:
        generation.protocol = error.protocol
        raise
    generation.protocol = protocol
    _require_startup_deadline(deadline, "protocol owner start")
    owner.verify_lexical_identity()

    exit_thread = threading.Thread(
        target=_monitor_generation_exit,
        args=(coordinator, generation),
        name=f"lsp-exit-{generation_nonce}",
        daemon=True,
    )
    generation.exit_thread = exit_thread
    _require_startup_deadline(deadline, "exit thread preparation")
    exit_thread.start()
    _require_startup_deadline(deadline, "exit thread start")
    if process.poll() is not None or protocol.fatal:
        raise RuntimeError("LSP process exited during generation startup")
    return generation


def _monitor_generation_exit(
    coordinator: _LifecycleCoordinator,
    generation: _Generation,
) -> None:
    process = generation.process
    if process is None:
        return
    try:
        process.wait()
    except BaseException as error:
        _queue_generation_failure(coordinator, generation, str(error))
        return
    if not _commit_generation_failure(
        coordinator,
        generation,
        "LSP process exited unexpectedly",
        exit_observed=True,
    ):
        return
    protocol = generation.protocol
    if protocol is not None:
        protocol._become_fatal("LSP process exited unexpectedly")


def _write_generation_lease(
    owner: _OwnerDirectory,
    generation: _Generation,
    owner_nonce: str,
    deadline: float,
    retry_stop: threading.Event,
) -> None:
    process = generation.process
    server_pid = process.pid if process is not None else generation.server_pid
    if server_pid is None:
        raise RuntimeError("LSP generation process is unavailable")
    heartbeat_monotonic = time.monotonic()
    heartbeat = datetime.now(timezone.utc)
    expires = heartbeat + timedelta(seconds=_LEASE_EXPIRY_SECONDS)
    owner.write_lease(
        {
            "expires_at": expires.isoformat().replace("+00:00", "Z"),
            "generation_nonce": generation.nonce,
            "heartbeat_at": heartbeat.isoformat().replace("+00:00", "Z"),
            "manager_pid": os.getpid(),
            "owner_nonce": owner_nonce,
            "schema_version": 1,
            "server_pid": server_pid,
            "state": "live",
        },
        deadline=deadline,
        expires_monotonic=heartbeat_monotonic + _LEASE_EXPIRY_SECONDS,
        retry_stop=retry_stop,
    )


def _start_lsp_process(
    cls: type[LspProcess],
    command: Sequence[str],
    *,
    cwd: Path,
    owner_root: Path,
) -> LspProcess:
    cwd = Path(cwd)
    owner_root = Path(owner_root)
    if not cwd.exists() or not cwd.is_dir():
        raise ValueError("cwd must be an existing directory")
    cwd = cwd.resolve()
    arguments = _validated_command(command, cwd)
    environment = lsp_environment()
    owner_nonce = _validated_owner_root(owner_root)
    generation_nonce = _new_generation_nonce()
    started_monotonic = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    coordinator = _LifecycleCoordinator(
        None, startup_generation_nonce=generation_nonce
    )
    _register_startup_cleanup(coordinator)
    startup_deadline: float | None = None
    instance: LspProcess | None = None
    driver_acquired = False

    try:
        owner = _OwnerDirectory.open(owner_root)
        coordinator.owner_directory = owner
        startup_deadline = time.monotonic() + _STARTUP_WAIT_SECONDS
        _acquire_driver(coordinator, startup_deadline)
        driver_acquired = True
        owner.create(startup_deadline)
        _require_startup_deadline(startup_deadline, "owner creation")
        owner_record: dict[str, object] = {
            "command_basename": Path(arguments[0]).name,
            "generation_nonce": generation_nonce,
            "owner_nonce": owner_nonce,
            "owner_pid": None,
            "started_at": started_at,
            "state": ProcessState.PROCESS_RUNNING.value,
        }
        generation = _prepare_generation(
            coordinator,
            arguments,
            cwd=cwd,
            environment=environment,
            deadline=startup_deadline,
            generation_nonce=generation_nonce,
            owner_record=owner_record,
        )
        _require_startup_deadline(startup_deadline, "generation preparation")
        process = generation.process
        protocol = generation.protocol
        if process is None or protocol is None:
            raise RuntimeError("LSP generation did not acquire process and protocol owners")
        instance = cls(
            process=process,
            protocol=protocol,
            owner_root=owner_root,
            owner_nonce=owner_nonce,
            generation_nonce=generation_nonce,
            state=ProcessState.PROCESS_RUNNING,
            started_monotonic=started_monotonic,
            last_used_monotonic=started_monotonic,
        )
        instance._coordinator = coordinator
        instance._command = tuple(arguments)
        instance._cwd = cwd
        instance._environment = dict(environment)
        instance._stderr_projection = generation.stderr
        instance._stderr_projection_lock = generation.stderr_lock
        _acquire_lease(coordinator, startup_deadline)
        try:
            _write_generation_lease(
                owner,
                generation,
                owner_nonce,
                startup_deadline,
                coordinator.heartbeat_stop,
            )
        finally:
            _release_lease(coordinator)
        _require_startup_deadline(startup_deadline, "initial lease publication")

        _acquire_lifecycle(coordinator, startup_deadline)
        try:
            if process.poll() is not None or protocol.fatal:
                raise RuntimeError("LSP process exited during startup")
            coordinator.active = generation
            coordinator.candidate = None
            coordinator.phase = _LifecyclePhase.RUNNING
            coordinator.startup_complete = True
            _notify_lifecycle_locked(coordinator)
        finally:
            _release_lifecycle(coordinator)

        _start_lifecycle_workers(instance, startup_deadline)
        _require_startup_deadline(startup_deadline, "lifecycle worker start")
        owner.verify_lexical_identity()
        if process.poll() is not None or protocol.fatal:
            raise RuntimeError("LSP process exited during startup")
        _require_startup_deadline(startup_deadline, "final startup fence")
        atexit.register(instance._atexit_close)
        _unregister_startup_cleanup(coordinator)
        return instance
    except BaseException as startup_error:
        deadline = startup_deadline if startup_deadline is not None else time.monotonic()
        _remember_mandatory_terminal_failure(
            instance,
            coordinator,
            _STARTUP_FAILED,
        )
        try:
            _mark_terminal_failure(
                instance,
                coordinator,
                _STARTUP_FAILED,
                deadline,
            )
        except BaseException:
            pass
        try:
            _drive_cleanup(
                instance,
                deadline,
                terminal=True,
                failure_code=_STARTUP_FAILED,
                coordinator_override=coordinator,
            )
        except BaseException:
            pass
        pending = _coordinator_has_ownership(coordinator)
        if pending:
            _register_startup_cleanup(coordinator)
            evidence_failed = coordinator.cleanup_result.evidence == "failed"
            message = (
                "LSP startup failed and retained evidence could not be written safely"
                if evidence_failed
                else "LSP startup cleanup retains retryable ownership"
            )
            error = StartupCleanupError(message, coordinator=coordinator)
            raise error from startup_error
        raise startup_error
    finally:
        try:
            if driver_acquired:
                _release_driver(coordinator)
        finally:
            if not _coordinator_has_ownership(coordinator):
                _unregister_startup_cleanup(coordinator)


def _start_lifecycle_workers(instance: LspProcess, deadline: float | None = None) -> None:
    coordinator = instance._coordinator
    if deadline is None:
        deadline = time.monotonic() + _GRACEFUL_CLEANUP_SECONDS
    _acquire_lifecycle(coordinator, deadline)
    try:
        if coordinator.recovery_thread is None:
            coordinator.recovery_thread = threading.Thread(
                target=_recovery_loop,
                args=(instance,),
                name=f"lsp-recovery-{instance.owner_nonce}",
                daemon=True,
            )
        if coordinator.heartbeat_thread is None:
            coordinator.heartbeat_thread = threading.Thread(
                target=_heartbeat_loop,
                args=(instance,),
                name=f"lsp-heartbeat-{instance.owner_nonce}",
                daemon=True,
            )
        recovery = coordinator.recovery_thread
        heartbeat = coordinator.heartbeat_thread
    finally:
        _release_lifecycle(coordinator)
    assert recovery is not None and heartbeat is not None
    if recovery.ident is None and recovery not in threading.enumerate():
        _require_startup_deadline(deadline, "recovery thread preparation")
        recovery.start()
        _require_startup_deadline(deadline, "recovery thread start")
    if heartbeat.ident is None and heartbeat not in threading.enumerate():
        _require_startup_deadline(deadline, "heartbeat thread preparation")
        heartbeat.start()
        _require_startup_deadline(deadline, "heartbeat thread start")


def _schedule_autonomous_recovery(instance: LspProcess) -> None:
    coordinator = instance._coordinator
    if not coordinator.recovery_request_pending.is_set():
        return
    recovery = coordinator.recovery_thread
    if (
        recovery is not None
        and recovery.is_alive()
        and not coordinator.recovery_stop.is_set()
    ):
        coordinator.recovery_wake.set()
        return
    maintenance_deadline = time.monotonic() + _GRACEFUL_CLEANUP_SECONDS
    _acquire_lifecycle(coordinator, maintenance_deadline)
    try:
        recovery = coordinator.recovery_thread
        if (
            recovery is not None
            and recovery.is_alive()
            and not coordinator.recovery_stop.is_set()
        ):
            coordinator.recovery_wake.set()
            return
    finally:
        _release_lifecycle(coordinator)

    if recovery is not None and recovery is not threading.current_thread():
        coordinator.recovery_stop.set()
        coordinator.recovery_wake.set()
        remaining = maintenance_deadline - time.monotonic()
        if remaining > 0:
            recovery.join(remaining)

    replacement: threading.Thread | None = None
    _acquire_lifecycle(coordinator, maintenance_deadline)
    try:
        if not coordinator.recovery_request_pending.is_set():
            return
        current = coordinator.recovery_thread
        if current is not None and current.is_alive():
            coordinator.recovery_stop.clear()
            coordinator.cleanup_result.recovery_join = "pending"
            coordinator.recovery_wake.set()
            return
        coordinator.recovery_stop.clear()
        coordinator.recovery_wake.clear()
        replacement = threading.Thread(
            target=_recovery_loop,
            args=(instance,),
            name=f"lsp-recovery-{instance.owner_nonce}",
            daemon=True,
        )
        coordinator.recovery_thread = replacement
        coordinator.cleanup_result.recovery_join = "pending"
        _notify_lifecycle_locked(coordinator)
    finally:
        _release_lifecycle(coordinator)
    assert replacement is not None
    replacement.start()
    coordinator.recovery_wake.set()


def _heartbeat_loop(instance: LspProcess) -> None:
    coordinator = instance._coordinator
    while True:
        coordinator.heartbeat_wake.wait(_HEARTBEAT_SECONDS)
        coordinator.heartbeat_wake.clear()
        if coordinator.heartbeat_stop.is_set():
            return
        maintenance_deadline = time.monotonic() + _GRACEFUL_CLEANUP_SECONDS
        try:
            _write_current_lease(instance, maintenance_deadline)
        except BaseException as error:
            if not _queue_owner_failure(coordinator, f"heartbeat_failed: {error}"):
                return
            if coordinator.cleanup_started.is_set():
                _remember_background_cleanup_error(coordinator, error)
            try:
                _acquire_lifecycle(coordinator, maintenance_deadline)
            except TimeoutError:
                return
            try:
                _select_terminal_failure_locked(
                    instance, coordinator, "heartbeat_failed"
                )
                coordinator.phase = _LifecyclePhase.STOPPING_FAILURE
                for generation in _generations_locked(coordinator):
                    _mark_generation_expected_exit(generation)
                _notify_lifecycle_locked(coordinator)
            finally:
                _release_lifecycle(coordinator)
            return


def _write_current_lease(instance: LspProcess, deadline: float) -> None:
    coordinator = instance._coordinator
    if coordinator.heartbeat_stop.is_set():
        return
    _acquire_lease(coordinator, deadline)
    try:
        _acquire_lifecycle(coordinator, deadline)
        try:
            if coordinator.phase in {
                _LifecyclePhase.STARTING,
                _LifecyclePhase.STOPPED_SUCCESS,
                _LifecyclePhase.STOPPED_FAILURE,
            }:
                return
            owner = coordinator.owner_directory
            generation = next(
                (
                    item
                    for item in _generations_locked(coordinator)
                    if item.server_pid is not None
                ),
                None,
            )
        finally:
            _release_lifecycle(coordinator)
        if generation is not None and owner is not None:
            _write_generation_lease(
                owner,
                generation,
                instance.owner_nonce,
                deadline,
                coordinator.heartbeat_stop,
            )
    finally:
        _release_lease(coordinator)


def _active_drain_generation(
    instance: LspProcess,
) -> tuple[_Generation, LspProtocol] | None:
    coordinator = instance._coordinator
    deadline = time.monotonic() + _GRACEFUL_CLEANUP_SECONDS
    try:
        _acquire_lifecycle(coordinator, deadline)
    except TimeoutError:
        bounded_error = TimeoutError(
            "LSP expired-drain inspection exceeded its maintenance deadline"
        )
        if _queue_owner_failure(coordinator, str(bounded_error)):
            _remember_background_cleanup_error(coordinator, bounded_error)
        return None
    try:
        generation = (
            coordinator.active
            if coordinator.phase is _LifecyclePhase.RUNNING
            else None
        )
        protocol = generation.protocol if generation is not None else None
    finally:
        _release_lifecycle(coordinator)
    if generation is None or protocol is None:
        return None
    with generation.failure_lock:
        if (
            generation._exit_observed
            or generation.failure_queued
            or generation.expected_exit.is_set()
        ):
            return None
    return generation, protocol


def _next_drain_deadline(instance: LspProcess) -> float | None:
    active = _active_drain_generation(instance)
    if active is None:
        return None
    return active[1].next_drain_deadline()


def _queue_expired_drain(instance: LspProcess) -> None:
    active = _active_drain_generation(instance)
    if active is None:
        return
    generation, protocol = active
    if protocol.expired_drain_keys(time.monotonic()):
        _queue_generation_failure(instance._coordinator, generation, "expired drain")


def _recovery_loop(instance: LspProcess) -> None:
    coordinator = instance._coordinator
    request_retry = False
    terminal_retry_code: str | None = None
    while not coordinator.recovery_stop.is_set():
        if request_retry or terminal_retry_code is not None:
            coordinator.recovery_wake.clear()
            if coordinator.recovery_stop.is_set():
                return
            coordinator.recovery_wake.wait(_RECOVERY_RETRY_SECONDS)
        else:
            drain_deadline = _next_drain_deadline(instance)
            wait_for = (
                None
                if drain_deadline is None
                else max(0.0, drain_deadline - time.monotonic())
            )
            coordinator.recovery_wake.wait(wait_for)
        coordinator.recovery_wake.clear()
        if coordinator.recovery_stop.is_set():
            return

        if terminal_retry_code is not None:
            if _retry_autonomous_terminal_cleanup(instance, terminal_retry_code):
                continue
            terminal_retry_code = None
            if coordinator.recovery_stop.is_set():
                return

        request_failure: str | None = None
        if coordinator.recovery_request_pending.is_set():
            maintenance_deadline = time.monotonic() + _GRACEFUL_CLEANUP_SECONDS
            request_retry, request_failure = _process_recovery_request(
                instance, maintenance_deadline
            )
        else:
            request_retry = False
        if request_failure is not None:
            terminal_retry_code = request_failure
        if request_retry or terminal_retry_code is not None:
            continue

        _queue_expired_drain(instance)
        while not coordinator.recovery_stop.is_set():
            try:
                intent = coordinator.failure_queue.get_nowait()
            except queue.Empty:
                break
            maintenance_deadline = time.monotonic() + _GRACEFUL_CLEANUP_SECONDS
            terminal_retry_code = _process_failure_intent(
                instance, intent, maintenance_deadline
            )
            if terminal_retry_code is not None:
                break


def _acquire_recovery_driver(
    coordinator: _LifecycleCoordinator,
    deadline: float,
) -> bool:
    while not coordinator.recovery_stop.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        try:
            _acquire_driver(
                coordinator,
                min(deadline, time.monotonic() + min(0.05, remaining)),
            )
        except TimeoutError:
            continue
        return True
    return False


def _process_recovery_request(
    instance: LspProcess,
    deadline: float,
) -> tuple[bool, str | None]:
    coordinator = instance._coordinator
    if not _acquire_recovery_driver(coordinator, deadline):
        return (
            coordinator.recovery_request_pending.is_set()
            and not coordinator.recovery_stop.is_set(),
            None,
        )
    try:
        try:
            _acquire_lifecycle(coordinator, deadline)
        except TimeoutError:
            return True, None
        try:
            requested_nonce = coordinator.recovery_request_nonce
            if requested_nonce is None:
                coordinator.recovery_request_pending.clear()
                return False, None
            if coordinator.terminal_outcome is not None:
                if coordinator.cleanup_result.ownership_pending:
                    return False, coordinator.terminal_code or "restart_failed"
                coordinator.recovery_request_nonce = None
                coordinator.recovery_request_pending.clear()
                _notify_lifecycle_locked(coordinator)
                return False, None
            active = coordinator.active
            if active is None or active.nonce != requested_nonce:
                requested_owned = any(
                    generation.nonce == requested_nonce
                    for generation in _generations_locked(coordinator)
                )
                if not requested_owned:
                    coordinator.recovery_request_nonce = None
                    coordinator.recovery_request_pending.clear()
                    _notify_lifecycle_locked(coordinator)
                    return False, None
            if coordinator.phase not in {
                _LifecyclePhase.RECOVERY_PENDING,
                _LifecyclePhase.RESTARTING,
                _LifecyclePhase.CLEANUP_PENDING,
            }:
                return True, None
        finally:
            _release_lifecycle(coordinator)

        try:
            _restart_generation_owned(instance, deadline)
        except BaseException:
            _remember_mandatory_terminal_failure(
                instance,
                coordinator,
                "restart_failed",
            )
            if coordinator.cleanup_result.ownership_pending:
                _retain_autonomous_cleanup_owners(instance)
                return False, "restart_failed"
        return False, None
    finally:
        _release_driver(coordinator)


def _retry_autonomous_terminal_cleanup(
    instance: LspProcess,
    code: str,
) -> bool:
    coordinator = instance._coordinator
    deadline = time.monotonic() + _GRACEFUL_CLEANUP_SECONDS
    if not _acquire_recovery_driver(coordinator, deadline):
        return (
            coordinator.cleanup_result.ownership_pending
            and not coordinator.recovery_stop.is_set()
        )
    try:
        try:
            _terminal_failure_lsp_process(instance, code, deadline)
        except BaseException as cleanup_error:
            _remember_background_cleanup_error(coordinator, cleanup_error)
        if not coordinator.cleanup_result.ownership_pending:
            return False
        return _retain_autonomous_cleanup_owners(instance)
    finally:
        _release_driver(coordinator)


def _retain_autonomous_cleanup_owners(instance: LspProcess) -> bool:
    coordinator = instance._coordinator
    deadline = time.monotonic() + _GRACEFUL_CLEANUP_SECONDS
    try:
        _acquire_driver(coordinator, deadline)
    except TimeoutError:
        return True
    heartbeat: threading.Thread | None = None
    try:
        try:
            _acquire_lifecycle(coordinator, deadline)
        except TimeoutError:
            return True
        try:
            if not coordinator.cleanup_result.ownership_pending:
                return False
            current = threading.current_thread()
            owner = coordinator.recovery_thread
            if owner is not None and owner is not current and owner.is_alive():
                return False
            coordinator.recovery_thread = current
            coordinator.recovery_stop.clear()
            coordinator.cleanup_result.recovery_join = "pending"
            heartbeat_owner = coordinator.heartbeat_thread
            if heartbeat_owner is None or not heartbeat_owner.is_alive():
                coordinator.heartbeat_stop.clear()
                coordinator.heartbeat_wake.clear()
                heartbeat = threading.Thread(
                    target=_heartbeat_loop,
                    args=(instance,),
                    name=f"lsp-heartbeat-{instance.owner_nonce}",
                    daemon=True,
                )
                coordinator.heartbeat_thread = heartbeat
                coordinator.cleanup_result.heartbeat_join = "pending"
            _notify_lifecycle_locked(coordinator)
        finally:
            _release_lifecycle(coordinator)
        if heartbeat is not None:
            try:
                heartbeat.start()
            except BaseException as error:
                _remember_background_cleanup_error(coordinator, error)
        return True
    finally:
        _release_driver(coordinator)


def _process_failure_intent(
    instance: LspProcess,
    intent: _FailureIntent,
    deadline: float,
) -> str | None:
    coordinator = instance._coordinator
    while True:
        if coordinator.recovery_stop.is_set():
            coordinator.failure_queue.put(intent)
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            coordinator.failure_queue.put(intent)
            coordinator.recovery_wake.set()
            return None
        try:
            _acquire_driver(
                coordinator,
                min(deadline, time.monotonic() + min(0.05, remaining)),
            )
        except TimeoutError:
            continue
        else:
            break
    try:
        return _process_failure_intent_owned(instance, intent, deadline)
    finally:
        _release_driver(coordinator)


def _process_failure_intent_owned(
    instance: LspProcess,
    intent: _FailureIntent,
    deadline: float,
) -> str | None:
    coordinator = instance._coordinator
    key = (intent.generation_nonce, intent.owner_fatal)
    try:
        _acquire_lifecycle(coordinator, deadline)
    except TimeoutError:
        coordinator.failure_queue.put(intent)
        coordinator.recovery_wake.set()
        return None

    terminal = False
    restart = False
    acknowledge = False
    try:
        if key in coordinator.seen_failures:
            acknowledge = True
            return None
        if not coordinator.startup_complete:
            coordinator.failure_queue.put(intent)
            coordinator.recovery_wake.set()
            return None
        active = coordinator.active
        if not intent.owner_fatal and (
            active is None or active.nonce != intent.generation_nonce
        ):
            coordinator.seen_failures.add(key)
            acknowledge = True
            return None
        coordinator.seen_failures.add(key)
        acknowledge = True
        if intent.owner_fatal or coordinator.recovery_attempted or instance.restart_count >= 1:
            _select_terminal_failure_locked(
                instance,
                coordinator,
                "heartbeat_failed" if intent.owner_fatal else _PROCESS_EXITED,
            )
            coordinator.phase = _LifecyclePhase.STOPPING_FAILURE
            terminal = True
        else:
            coordinator.recovery_attempted = True
            coordinator.phase = _LifecyclePhase.RECOVERY_PENDING
            instance.state = ProcessState.DEGRADED
            if active is not None:
                _mark_generation_expected_exit(active)
            restart = True
        _notify_lifecycle_locked(coordinator)
    finally:
        _release_lifecycle(coordinator)
        if acknowledge:
            _acknowledge_failure_intent(coordinator)

    if restart:
        try:
            _restart_generation(instance, deadline)
        except BaseException:
            _remember_mandatory_terminal_failure(
                instance,
                coordinator,
                "restart_failed",
            )
            return "restart_failed"
    elif terminal:
        try:
            errors = _drive_cleanup(
                instance,
                deadline,
                terminal=True,
                failure_code=coordinator.terminal_code,
            )
        except BaseException as cleanup_error:
            _remember_background_cleanup_error(coordinator, cleanup_error)
            if coordinator.cleanup_result.ownership_pending:
                return coordinator.terminal_code or _PROCESS_EXITED
        else:
            if errors:
                _remember_background_cleanup_error(coordinator, errors[0])
            if coordinator.cleanup_result.ownership_pending:
                return coordinator.terminal_code or _PROCESS_EXITED
    return None


def _remember_background_cleanup_error(
    coordinator: _LifecycleCoordinator, error: BaseException
) -> None:
    with coordinator.background_error_lock:
        if coordinator.background_cleanup_error is None:
            coordinator.background_cleanup_error = _sanitized_cleanup_error(
                "background_cleanup", error
            )


def _take_background_cleanup_error(
    coordinator: _LifecycleCoordinator,
) -> BaseException | None:
    with coordinator.background_error_lock:
        error = coordinator.background_cleanup_error
        coordinator.background_cleanup_error = None
    if error is None:
        return None
    code = f", code {error.error_code}" if error.error_code is not None else ""
    return RuntimeError(f"LSP background cleanup failed ({error.error_type}{code})")


def _request_generation(instance: LspProcess, deadline: float) -> _Generation:
    coordinator = instance._coordinator
    _acquire_lifecycle(coordinator, deadline)
    try:
        while coordinator.phase in {
            _LifecyclePhase.STARTING,
            _LifecyclePhase.RECOVERY_PENDING,
            _LifecyclePhase.RESTARTING,
        }:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not coordinator.condition.wait(remaining):
                raise TimeoutError("LSP lifecycle transition did not finish before deadline")
        if coordinator.terminal_outcome is not None or coordinator.phase in {
            _LifecyclePhase.STOPPING_SUCCESS,
            _LifecyclePhase.STOPPING_FAILURE,
            _LifecyclePhase.CLEANUP_PENDING,
            _LifecyclePhase.STOPPED_SUCCESS,
            _LifecyclePhase.STOPPED_FAILURE,
        }:
            if instance.state is ProcessState.FAILED:
                raise RuntimeError("LSP process has exited")
            raise RuntimeError("LSP process is closed")
        generation = coordinator.active
        if generation is None or generation.protocol is None or generation.process is None:
            raise RuntimeError("LSP process generation is unavailable")
        instance.last_used_monotonic = time.monotonic()
        return generation
    finally:
        _release_lifecycle(coordinator)


def _wait_for_generation_change(
    instance: LspProcess,
    generation_nonce: str,
    deadline: float,
) -> bool:
    coordinator = instance._coordinator
    _acquire_lifecycle(coordinator, deadline)
    try:
        while True:
            active = coordinator.active
            if (
                coordinator.phase is _LifecyclePhase.RUNNING
                and active is not None
                and active.nonce != generation_nonce
            ):
                return True
            if coordinator.phase in {
                _LifecyclePhase.CLEANUP_PENDING,
                _LifecyclePhase.STOPPED_FAILURE,
                _LifecyclePhase.STOPPED_SUCCESS,
            }:
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not coordinator.condition.wait(remaining):
                raise TimeoutError("LSP recovery did not finish before deadline")
    finally:
        _release_lifecycle(coordinator)


def _generation_changed(
    instance: LspProcess,
    generation: _Generation,
    deadline: float,
) -> bool:
    coordinator = instance._coordinator
    _acquire_lifecycle(coordinator, deadline)
    try:
        return coordinator.active is not generation
    finally:
        _release_lifecycle(coordinator)


def _request_lsp_process(
    instance: LspProcess,
    method: str,
    params: object,
    *,
    deadline: float,
    cancellation: CancellationToken | None,
) -> object:
    deadline = _validated_deadline(deadline)
    for attempt in range(2):
        generation = _request_generation(instance, deadline)
        protocol = generation.protocol
        process = generation.process
        assert protocol is not None and process is not None
        expired = protocol.expired_drain_keys(time.monotonic())
        if expired or process.poll() is not None or protocol.fatal:
            _queue_generation_failure(
                instance._coordinator,
                generation,
                "expired drain" if expired else _PROCESS_EXITED,
            )
            changed = _wait_for_generation_change(
                instance, generation.nonce, deadline
            )
            if attempt == 0 and changed:
                continue
            raise ProtocolViolation("LSP generation is fatally unavailable")
        try:
            return protocol.request(
                method,
                params,
                deadline=deadline,
                cancellation=cancellation,
            )
        except _LocalRequestViolation:
            raise
        except ProtocolViolation:
            changed = _generation_changed(instance, generation, deadline)
            objectively_fatal = changed or protocol.fatal or process.poll() is not None
            if not objectively_fatal:
                raise
            _queue_generation_failure(
                instance._coordinator, generation, _PROCESS_EXITED
            )
            changed = _wait_for_generation_change(
                instance, generation.nonce, deadline
            )
            if attempt == 0 and changed:
                continue
            raise
    raise RuntimeError("LSP request retry invariant breached")


def _failure_identity(
    instance: LspProcess | None,
    coordinator: _LifecycleCoordinator,
    code: str,
) -> _FailureEvidenceIdentity | None:
    owner = coordinator.owner_directory
    if owner is None:
        return None
    generation = coordinator.active or coordinator.candidate
    if generation is None and coordinator.retired:
        generation = coordinator.retired[-1]
    generation_nonce = (
        generation.nonce
        if generation is not None
        else (
            instance.generation_nonce
            if instance is not None
            else coordinator.startup_generation_nonce
        )
    )
    if generation_nonce is None:
        return None
    process = generation.process if generation is not None else None
    server_pid = (
        process.pid
        if process is not None
        else (
            generation.server_pid
            if generation is not None
            else (instance.process.pid if instance is not None else None)
        )
    )
    return _FailureEvidenceIdentity(
        code,
        instance.owner_nonce if instance is not None else owner.owner_root.name,
        generation_nonce,
        server_pid,
    )


def _remember_mandatory_terminal_failure(
    instance: LspProcess | None,
    coordinator: _LifecycleCoordinator,
    code: str,
) -> None:
    identity = _failure_identity(instance, coordinator, code)
    if identity is None:
        return
    with coordinator.terminal_state_lock:
        if coordinator.mandatory_failure_intent is None:
            coordinator.mandatory_failure_intent = identity


def _select_terminal_failure_locked(
    instance: LspProcess | None,
    coordinator: _LifecycleCoordinator,
    code: str,
) -> None:
    coordinator.terminal_outcome = "failure"
    if coordinator.terminal_code is None:
        coordinator.terminal_code = code
    if coordinator.failure_evidence_identity is None:
        coordinator.failure_evidence_identity = _failure_identity(
            instance, coordinator, coordinator.terminal_code
        )
    if instance is not None and instance.state is not ProcessState.FAILED:
        instance.state = ProcessState.DEGRADED


def _mark_terminal_failure(
    instance: LspProcess | None,
    coordinator: _LifecycleCoordinator,
    code: str,
    deadline: float,
) -> None:
    _acquire_lifecycle(coordinator, deadline, allow_expired=True)
    try:
        _select_terminal_failure_locked(instance, coordinator, code)
        coordinator.phase = _LifecyclePhase.STOPPING_FAILURE
        for generation in _generations_locked(coordinator):
            _mark_generation_expected_exit(generation)
        _notify_lifecycle_locked(coordinator)
    finally:
        _release_lifecycle(coordinator)


def _terminal_failure_lsp_process(
    instance: LspProcess,
    code: str,
    deadline: float,
) -> None:
    coordinator = instance._coordinator
    _remember_mandatory_terminal_failure(instance, coordinator, code)
    deadline = _validated_deadline(deadline)
    _acquire_driver(coordinator, deadline)
    try:
        _mark_terminal_failure(instance, coordinator, code, deadline)
        errors = _drive_cleanup(
            instance,
            deadline,
            terminal=True,
            failure_code=code,
        )
        if errors:
            raise errors[0]
    finally:
        _release_driver(coordinator)


def _restart_lsp_process(instance: LspProcess, deadline: float) -> None:
    deadline = _validated_deadline(deadline)
    coordinator = instance._coordinator
    _acquire_driver(coordinator, deadline)
    handoff_required = False
    try:
        _restart_lsp_process_owned(instance, deadline)
    except BaseException:
        handoff_required = True
        raise
    finally:
        try:
            _release_driver(coordinator)
        finally:
            if handoff_required:
                try:
                    _schedule_autonomous_recovery(instance)
                except BaseException as recovery_error:
                    recovery = coordinator.recovery_thread
                    if recovery is None or not recovery.is_alive():
                        _remember_background_cleanup_error(
                            coordinator, recovery_error
                        )
                    coordinator.recovery_wake.set()


def _restart_lsp_process_owned(instance: LspProcess, deadline: float) -> None:
    coordinator = instance._coordinator
    _acquire_lifecycle(coordinator, deadline)
    terminal = False
    try:
        if coordinator.terminal_outcome is not None:
            raise RuntimeError("LSP process is closed")
        if coordinator.recovery_attempted or instance.restart_count >= 1:
            terminal = True
        else:
            active = coordinator.active
            if active is None:
                raise RuntimeError("LSP process generation is unavailable")
            coordinator.recovery_attempted = True
            coordinator.recovery_request_nonce = active.nonce
            coordinator.recovery_request_pending.set()
            coordinator.phase = _LifecyclePhase.RECOVERY_PENDING
            instance.state = ProcessState.DEGRADED
            _mark_generation_expected_exit(active)
            coordinator.recovery_wake.set()
            _notify_lifecycle_locked(coordinator)
    finally:
        _release_lifecycle(coordinator)
    if terminal:
        _terminal_failure_lsp_process(instance, _PROCESS_EXITED, deadline)
        raise ProtocolViolation("LSP process restart limit exceeded")
    _restart_generation(instance, deadline)


def _restart_generation(
    instance: LspProcess,
    deadline: float,
) -> None:
    coordinator = instance._coordinator
    _acquire_driver(coordinator, deadline)
    try:
        _restart_generation_owned(instance, deadline)
    finally:
        _release_driver(coordinator)


def _restart_generation_owned(instance: LspProcess, deadline: float) -> None:
    coordinator = instance._coordinator
    _acquire_lifecycle(coordinator, deadline)
    try:
        if coordinator.terminal_outcome is not None:
            raise RuntimeError("LSP process is closed")
        old = coordinator.active
        if old is not None:
            _mark_generation_expected_exit(old)
            coordinator.retired.append(old)
            coordinator.active = None
        coordinator.phase = _LifecyclePhase.RESTARTING
        _notify_lifecycle_locked(coordinator)
    finally:
        _release_lifecycle(coordinator)

    retirement_errors = _drive_cleanup(instance, deadline, terminal=False)
    if retirement_errors or any(not item.released for item in coordinator.retired):
        _remember_mandatory_terminal_failure(
            instance,
            coordinator,
            "restart_failed",
        )
        _mark_terminal_failure(instance, coordinator, "restart_failed", deadline)
        _drive_cleanup(
            instance,
            deadline,
            terminal=True,
            failure_code="restart_failed",
        )
        if retirement_errors:
            raise retirement_errors[0]
        raise TimeoutError("LSP retired generation cleanup is incomplete")

    try:
        if instance._cwd is None or not instance._command:
            raise RuntimeError("LSP restart inputs are unavailable")
        generation_nonce = _new_generation_nonce()
        candidate = _prepare_generation(
            coordinator,
            instance._command,
            cwd=instance._cwd,
            environment=instance._environment,
            deadline=deadline,
            generation_nonce=generation_nonce,
        )
        owner = coordinator.owner_directory
        if owner is None:
            raise RuntimeError("LSP owner directory is unavailable")
        if (
            candidate.process is None
            or candidate.protocol is None
            or candidate.process.poll() is not None
            or candidate.protocol.fatal
        ):
            raise RuntimeError("LSP restart candidate failed before commit")
        _acquire_lease(coordinator, deadline)
        try:
            _write_generation_lease(
                owner,
                candidate,
                instance.owner_nonce,
                deadline,
                coordinator.heartbeat_stop,
            )
        finally:
            _release_lease(coordinator)
        _acquire_lifecycle(coordinator, deadline)
        try:
            if coordinator.terminal_outcome is not None:
                raise RuntimeError("LSP process became terminal during restart")
            if candidate.process.poll() is not None or candidate.protocol.fatal:
                raise RuntimeError("LSP restart candidate failed before commit")
            coordinator.active = candidate
            coordinator.candidate = None
            coordinator.phase = _LifecyclePhase.RUNNING
            instance.process = candidate.process
            instance.protocol = candidate.protocol
            instance.generation_nonce = candidate.nonce
            instance.restart_count += 1
            instance.state = ProcessState.PROCESS_RUNNING
            instance._stderr_projection = candidate.stderr
            instance._stderr_projection_lock = candidate.stderr_lock
            coordinator.recovery_request_nonce = None
            coordinator.recovery_request_pending.clear()
            _notify_lifecycle_locked(coordinator)
        finally:
            _release_lifecycle(coordinator)
    except BaseException:
        _remember_mandatory_terminal_failure(
            instance,
            coordinator,
            "restart_failed",
        )
        try:
            _mark_terminal_failure(instance, coordinator, "restart_failed", deadline)
        except BaseException:
            pass
        _drive_cleanup(
            instance,
            deadline,
            terminal=True,
            failure_code="restart_failed",
        )
        raise


def _shutdown_lsp_process(instance: LspProcess, deadline: float) -> None:
    deadline = _validated_deadline(deadline)
    coordinator = instance._coordinator
    _acquire_driver(coordinator, deadline)
    try:
        _shutdown_lsp_process_owned(instance, deadline)
    finally:
        _release_driver(coordinator)


def _shutdown_lsp_process_owned(instance: LspProcess, deadline: float) -> None:
    coordinator = instance._coordinator
    _acquire_lifecycle(coordinator, deadline)
    try:
        if coordinator.phase in {
            _LifecyclePhase.STOPPED_SUCCESS,
            _LifecyclePhase.STOPPED_FAILURE,
        } and not _coordinator_has_ownership_locked(coordinator):
            background_error = _take_background_cleanup_error(coordinator)
            if background_error is not None:
                raise background_error
            return
        if coordinator.terminal_outcome is None:
            coordinator.terminal_outcome = "success"
        _linearize_terminal_outcome_locked(
            instance, coordinator, deadline, commit_success=False
        )
        failure = coordinator.terminal_outcome == "failure"
        coordinator.phase = (
            _LifecyclePhase.STOPPING_FAILURE
            if failure
            else _LifecyclePhase.STOPPING_SUCCESS
        )
        generation = coordinator.active
        if generation is not None:
            _mark_generation_expected_exit(generation)
        _notify_lifecycle_locked(coordinator)
    finally:
        _release_lifecycle(coordinator)

    graceful_error: BaseException | None = None
    if not failure and generation is not None:
        process = generation.process
        protocol = generation.protocol
        graceful_deadline = min(
            deadline, time.monotonic() + _GRACEFUL_CLEANUP_SECONDS
        )
        try:
            if (
                process is not None
                and protocol is not None
                and process.poll() is None
                and time.monotonic() < graceful_deadline
            ):
                protocol.request("shutdown", {}, deadline=graceful_deadline)
                protocol.notify("exit", {}, deadline=graceful_deadline)
                _wait_for_lsp_exit(instance, graceful_deadline)
        except (OSError, RuntimeError, TimeoutError):
            pass
        except BaseException as error:
            graceful_error = error

    errors = _drive_cleanup(
        instance,
        deadline,
        terminal=True,
        failure_code=coordinator.terminal_code,
    )
    if errors:
        if graceful_error is not None:
            raise errors[0] from graceful_error
        raise errors[0]
    if graceful_error is not None:
        raise graceful_error
    background_error = _take_background_cleanup_error(coordinator)
    if background_error is not None:
        raise background_error


def _cancel_all_lsp_process(instance: LspProcess, reason: str) -> None:
    deadline = time.monotonic() + _GRACEFUL_CLEANUP_SECONDS
    coordinator = instance._coordinator
    _acquire_lifecycle(coordinator, deadline)
    try:
        if coordinator.terminal_outcome == "failure":
            return
        generation = coordinator.active
        if generation is None or generation.protocol is None:
            raise RuntimeError("LSP process is closed")
        protocol = generation.protocol
    finally:
        _release_lifecycle(coordinator)
    protocol.cancel_all(reason)


def _idle_expired_lsp_process(instance: LspProcess, now: float) -> bool:
    now = _validated_deadline(now)
    coordinator = instance._coordinator
    deadline = time.monotonic() + _GRACEFUL_CLEANUP_SECONDS
    _acquire_lifecycle(coordinator, deadline)
    try:
        return now - instance.last_used_monotonic >= _IDLE_SECONDS
    finally:
        _release_lifecycle(coordinator)


def _wait_for_lsp_exit(instance: LspProcess, deadline: float) -> int:
    deadline = _validated_deadline(deadline)
    coordinator = instance._coordinator
    _acquire_lifecycle(coordinator, deadline)
    try:
        generation = instance._projected_generation()
        process = instance.process
        stderr_thread = generation.stderr_thread if generation is not None else None
        exit_thread = generation.exit_thread if generation is not None else None
    finally:
        _release_lifecycle(coordinator)
    remaining = deadline - time.monotonic()
    if remaining <= 0 and process.poll() is None:
        raise TimeoutError("LSP process did not exit before deadline")
    try:
        return_code = process.wait(timeout=max(0.0, remaining))
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("LSP process did not exit before deadline") from exc
    for owner in (stderr_thread, exit_thread):
        if owner is None or owner is threading.current_thread():
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("LSP process streams did not drain before deadline")
        owner.join(remaining)
        if owner.is_alive():
            raise TimeoutError("LSP process streams did not drain before deadline")
    return return_code


def _generations_locked(coordinator: _LifecycleCoordinator) -> list[_Generation]:
    generations: list[_Generation] = []
    identities: set[int] = set()
    for generation in [
        coordinator.active,
        coordinator.candidate,
        *coordinator.retired,
    ]:
        if generation is not None and id(generation) not in identities:
            identities.add(id(generation))
            generations.append(generation)
    return generations


def _join_owned_thread(thread: threading.Thread | None, deadline: float) -> bool:
    if thread is None:
        return True
    if thread is threading.current_thread():
        return False
    if thread.ident is None and thread not in threading.enumerate():
        return True
    remaining = deadline - time.monotonic()
    if remaining > 0:
        thread.join(remaining)
    return not thread.is_alive()


def _record_cleanup_error(
    result: _CleanupResult,
    current: list[BaseException],
    step: str,
    error: BaseException,
) -> None:
    result.failed(step, error)
    current.append(error)


def _ensure_failure_evidence(
    instance: LspProcess | None,
    coordinator: _LifecycleCoordinator,
    code: str,
    deadline: float,
) -> None:
    result = coordinator.cleanup_result
    _acquire_lifecycle(coordinator, deadline, allow_expired=True)
    try:
        owner = coordinator.owner_directory
        if owner is None:
            raise RuntimeError("LSP failure evidence owner is unavailable")
        terminal_code = coordinator.terminal_code
        if terminal_code is None or code != terminal_code:
            raise RuntimeError("LSP failure evidence code does not match terminal identity")
        identity = coordinator.failure_evidence_identity
        if identity is None:
            identity = _failure_identity(instance, coordinator, terminal_code)
            if identity is None:
                raise RuntimeError(
                    "LSP failure evidence generation identity is unavailable"
                )
            coordinator.failure_evidence_identity = identity
        if identity.code != terminal_code:
            raise RuntimeError("LSP failure evidence identity is not terminal-code exact")
    finally:
        _release_lifecycle(coordinator)

    with owner._child_handle_lock:
        owner._retry_pending_temp_names()
        if result.evidence == "success":
            return
        try:
            _write_failure_record(
                owner,
                code=identity.code,
                owner_nonce=identity.owner_nonce,
                generation_nonce=identity.generation_nonce,
                pid=identity.pid,
            )
        except FileExistsError:
            record = owner.read_record("failure.json")
            _validate_failure_record(
                record,
                code=identity.code,
                owner_nonce=identity.owner_nonce,
                generation_nonce=identity.generation_nonce,
                pid=identity.pid,
            )
            owner.sync_directory()
        owner._retry_pending_temp_names()

        _acquire_lifecycle(coordinator, deadline, allow_expired=True)
        try:
            if (
                coordinator.terminal_code != identity.code
                or coordinator.failure_evidence_identity != identity
            ):
                raise RuntimeError("LSP terminal identity changed during evidence write")
            result.succeeded("evidence")
            if instance is not None:
                instance.state = ProcessState.FAILED
            _notify_lifecycle_locked(coordinator)
        finally:
            _release_lifecycle(coordinator)


def _failure_evidence_required(coordinator: _LifecycleCoordinator) -> bool:
    owner = coordinator.owner_directory
    return owner is not None and owner.owner_permissions_verified


def _stop_heartbeat_owner(
    coordinator: _LifecycleCoordinator,
    deadline: float,
    *,
    allow_expired: bool = False,
) -> None:
    _acquire_lifecycle(coordinator, deadline, allow_expired=allow_expired)
    try:
        thread = coordinator.heartbeat_thread
    finally:
        _release_lifecycle(coordinator)
    coordinator.heartbeat_stop.set()
    coordinator.heartbeat_wake.set()
    if not _join_owned_thread(thread, deadline):
        raise TimeoutError("LSP heartbeat thread did not stop before deadline")
    _acquire_lifecycle(coordinator, deadline, allow_expired=allow_expired)
    try:
        if coordinator.heartbeat_thread is thread:
            coordinator.heartbeat_thread = None
            _notify_lifecycle_locked(coordinator)
    finally:
        _release_lifecycle(coordinator)


def _stop_recovery_owner(
    coordinator: _LifecycleCoordinator,
    deadline: float,
    *,
    allow_expired: bool = False,
) -> bool:
    _acquire_lifecycle(coordinator, deadline, allow_expired=allow_expired)
    try:
        thread = coordinator.recovery_thread
    finally:
        _release_lifecycle(coordinator)
    coordinator.recovery_stop.set()
    coordinator.recovery_wake.set()
    if thread is threading.current_thread():
        _acquire_lifecycle(coordinator, deadline, allow_expired=allow_expired)
        try:
            if coordinator.recovery_thread is thread:
                coordinator.recovery_thread = None
                _notify_lifecycle_locked(coordinator)
        finally:
            _release_lifecycle(coordinator)
        return True
    if not _join_owned_thread(thread, deadline):
        raise TimeoutError("LSP recovery thread did not stop before deadline")
    _acquire_lifecycle(coordinator, deadline, allow_expired=allow_expired)
    try:
        if coordinator.recovery_thread is thread:
            coordinator.recovery_thread = None
            _notify_lifecycle_locked(coordinator)
    finally:
        _release_lifecycle(coordinator)
    return True


def _drain_terminal_failures(
    instance: LspProcess | None,
    coordinator: _LifecycleCoordinator,
    deadline: float,
) -> None:
    intents: list[_FailureIntent] = []
    while True:
        try:
            intents.append(coordinator.failure_queue.get_nowait())
        except queue.Empty:
            break
    if not intents:
        return
    try:
        _acquire_lifecycle(coordinator, deadline, allow_expired=True)
    except BaseException:
        for intent in intents:
            coordinator.failure_queue.put(intent)
        coordinator.recovery_wake.set()
        raise
    try:
        _select_terminal_failure_locked(
            instance,
            coordinator,
            (
                "heartbeat_failed"
                if any(intent.owner_fatal for intent in intents)
                else _PROCESS_EXITED
            ),
        )
        coordinator.phase = _LifecyclePhase.STOPPING_FAILURE
        _notify_lifecycle_locked(coordinator)
    finally:
        _release_lifecycle(coordinator)
    for _intent in intents:
        _acknowledge_failure_intent(coordinator)


def _linearize_terminal_outcome_locked(
    instance: LspProcess | None,
    coordinator: _LifecycleCoordinator,
    deadline: float,
    *,
    commit_success: bool,
) -> str | None:
    _acquire_terminal_intent(
        coordinator,
        deadline,
        allow_expired=not commit_success,
    )
    try:
        mark_failure_exits = False
        with coordinator.terminal_state_lock:
            mandatory_intent = coordinator.mandatory_failure_intent
            if mandatory_intent is not None:
                coordinator.terminal_outcome = "failure"
                coordinator.terminal_code = mandatory_intent.code
                coordinator.failure_evidence_identity = mandatory_intent
                if instance is not None and instance.state is not ProcessState.FAILED:
                    instance.state = ProcessState.DEGRADED
                coordinator.phase = _LifecyclePhase.STOPPING_FAILURE
                mark_failure_exits = True
            elif (
                coordinator.pending_failure_intents > 0
                and not coordinator.success_committed
            ):
                _select_terminal_failure_locked(
                    instance,
                    coordinator,
                    coordinator.pending_failure_code or _PROCESS_EXITED,
                )
                coordinator.phase = _LifecyclePhase.STOPPING_FAILURE
                mark_failure_exits = True
            if (
                commit_success
                and mandatory_intent is None
                and coordinator.terminal_outcome == "success"
            ):
                coordinator.success_committed = True
        if mark_failure_exits:
            for generation in _generations_locked(coordinator):
                _mark_generation_expected_exit(generation)
        return coordinator.terminal_outcome
    finally:
        _release_terminal_intent(coordinator)


def _linearize_terminal_outcome(
    instance: LspProcess | None,
    coordinator: _LifecycleCoordinator,
    deadline: float,
    *,
    commit_success: bool,
) -> str | None:
    _acquire_lifecycle(coordinator, deadline, allow_expired=True)
    try:
        outcome = _linearize_terminal_outcome_locked(
            instance, coordinator, deadline, commit_success=commit_success
        )
        _notify_lifecycle_locked(coordinator)
        return outcome
    finally:
        _release_lifecycle(coordinator)


def _drive_cleanup(
    instance: LspProcess | None,
    deadline: float,
    *,
    terminal: bool,
    failure_code: str | None = None,
    coordinator_override: _LifecycleCoordinator | None = None,
) -> list[BaseException]:
    coordinator = (
        instance._coordinator if instance is not None else coordinator_override
    )
    if coordinator is None:
        raise RuntimeError("LSP cleanup requires a lifecycle coordinator")
    if terminal:
        coordinator.cleanup_started.set()
    _acquire_driver(coordinator, deadline, allow_expired=True)
    try:
        return _drive_cleanup_owned(
            instance,
            deadline,
            terminal=terminal,
            failure_code=failure_code,
            coordinator=coordinator,
        )
    finally:
        _release_driver(coordinator)


def _drive_cleanup_owned(
    instance: LspProcess | None,
    deadline: float,
    *,
    terminal: bool,
    failure_code: str | None,
    coordinator: _LifecycleCoordinator,
) -> list[BaseException]:
    result = coordinator.cleanup_result
    current_errors: list[BaseException] = []
    terminal_outcome_ready = not terminal

    try:
        _acquire_lifecycle(coordinator, deadline, allow_expired=True)
    except BaseException as error:
        _record_cleanup_error(result, current_errors, "generation_joins", error)
        result.ownership_pending = True
        return current_errors
    try:
        generations = _generations_locked(coordinator)
        if terminal:
            try:
                outcome = _linearize_terminal_outcome_locked(
                    instance, coordinator, deadline, commit_success=False
                )
            except BaseException as error:
                terminal_outcome_ready = False
                _record_cleanup_error(result, current_errors, "recovery_join", error)
                outcome = coordinator.terminal_outcome
            else:
                terminal_outcome_ready = True
        else:
            outcome = coordinator.terminal_outcome
        code = coordinator.terminal_code or failure_code or _PROCESS_EXITED
        if terminal:
            for generation in generations:
                _mark_generation_expected_exit(generation)
        _notify_lifecycle_locked(coordinator)
    finally:
        _release_lifecycle(coordinator)

    owner = coordinator.owner_directory
    evidence_unavailable = (
        terminal
        and outcome == "failure"
        and (
            owner is None
            or (not generations and not owner.owner_permissions_verified)
        )
    )
    if evidence_unavailable:
        result.succeeded("evidence", "not_applicable")
    elif terminal and outcome == "failure":
        try:
            _ensure_failure_evidence(instance, coordinator, code, deadline)
        except BaseException as error:
            _record_cleanup_error(result, current_errors, "evidence", error)
    elif terminal:
        result.succeeded("evidence", "not_applicable")

    tree_termination_ok = True
    protocol_stop_ok = True
    protocol_stop_pending = False
    tree_release_ok = True
    tree_release_pending = False
    joins_ok = True
    joins_pending = False
    for generation in generations:
        protocol = generation.protocol
        if protocol is not None:
            try:
                protocol._stop_io_for_process_cleanup()
            except BaseException as error:
                protocol_stop_ok = False
                _record_cleanup_error(result, current_errors, "protocol_stop", error)

        tree = generation.tree
        if tree is not None:
            try:
                tree.terminate(deadline=deadline)
            except BaseException as error:
                tree_termination_ok = False
                _record_cleanup_error(
                    result, current_errors, "tree_termination", error
                )

        process = generation.process
        try:
            process_exited = process is None or process.poll() is not None
        except BaseException as error:
            process_exited = False
            tree_termination_ok = False
            _record_cleanup_error(
                result, current_errors, "tree_termination", error
            )
        pipes_closed = True
        if protocol is not None and process_exited:
            try:
                protocol._finish_io_after_process_exit(deadline)
            except BaseException as error:
                protocol_stop_ok = False
                _record_cleanup_error(result, current_errors, "protocol_stop", error)
            else:
                generation.protocol = None
        elif protocol is not None:
            protocol_stop_pending = True

        if process is not None and process_exited:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is None or getattr(stream, "closed", False):
                    continue
                try:
                    stream.close()
                except BaseException as error:
                    pipes_closed = False
                    protocol_stop_ok = False
                    _record_cleanup_error(
                        result, current_errors, "protocol_stop", error
                    )

        if tree is not None and os.name != "nt":
            try:
                tree.close()
            except BaseException as error:
                tree_release_ok = False
                _record_cleanup_error(result, current_errors, "tree_release", error)
            else:
                generation.tree = None

        if process_exited:
            for attribute in ("stderr_thread", "exit_thread"):
                thread = getattr(generation, attribute)
                if _join_owned_thread(thread, deadline):
                    setattr(generation, attribute, None)
                else:
                    joins_ok = False
                    error = TimeoutError(
                        "LSP generation thread did not stop before deadline"
                    )
                    _record_cleanup_error(
                        result, current_errors, "generation_joins", error
                    )
        else:
            joins_pending = True

        windows_release_ready = (
            process_exited
            and generation.protocol is None
            and generation.stderr_thread is None
            and generation.exit_thread is None
        )
        if tree is not None and os.name == "nt":
            if windows_release_ready:
                try:
                    tree.close()
                except BaseException as error:
                    tree_release_ok = False
                    _record_cleanup_error(result, current_errors, "tree_release", error)
                else:
                    generation.tree = None
            else:
                tree_release_pending = True

        windows_job = generation.windows_job
        if windows_job is not None:
            if windows_release_ready:
                try:
                    _lsp_process_tree._close_windows_handle(windows_job)
                except BaseException as error:
                    tree_release_ok = False
                    _record_cleanup_error(result, current_errors, "tree_release", error)
                else:
                    generation.windows_job = None
            else:
                tree_release_pending = True

        if (
            process is not None
            and process_exited
            and generation.tree is None
            and generation.protocol is None
            and generation.stderr_thread is None
            and generation.exit_thread is None
            and pipes_closed
        ):
            generation.process = None

    if tree_termination_ok:
        result.succeeded("tree_termination")
    if protocol_stop_ok and not protocol_stop_pending:
        result.succeeded("protocol_stop")
    if tree_release_ok and not tree_release_pending:
        result.succeeded("tree_release")
    if joins_ok and not joins_pending:
        result.succeeded("generation_joins")

    all_generations_released = all(generation.released for generation in generations)
    recovery_stopped = (
        coordinator.recovery_thread is None
        or not coordinator.recovery_thread.is_alive()
    )
    if terminal:
        recovery = coordinator.recovery_thread
        autonomous_handoff = (
            coordinator.recovery_request_pending.is_set()
            and recovery is not None
            and recovery is not threading.current_thread()
            and recovery.is_alive()
            and (current_errors or not all_generations_released)
        )
        if autonomous_handoff:
            recovery_stopped = False
            result.recovery_join = "pending"
            coordinator.recovery_wake.set()
        else:
            try:
                recovery_stopped = _stop_recovery_owner(
                    coordinator, deadline, allow_expired=True
                )
                if recovery_stopped:
                    result.succeeded("recovery_join")
                else:
                    result.recovery_join = "pending"
            except BaseException as error:
                recovery_stopped = False
                _record_cleanup_error(result, current_errors, "recovery_join", error)
        if recovery_stopped:
            try:
                _drain_terminal_failures(instance, coordinator, deadline)
            except BaseException as error:
                _record_cleanup_error(result, current_errors, "recovery_join", error)
        try:
            outcome = _linearize_terminal_outcome(
                instance, coordinator, deadline, commit_success=False
            )
        except BaseException as error:
            terminal_outcome_ready = False
            _record_cleanup_error(result, current_errors, "recovery_join", error)
            outcome = coordinator.terminal_outcome
        else:
            terminal_outcome_ready = True
        if outcome == "failure" and _failure_evidence_required(coordinator):
            try:
                _ensure_failure_evidence(
                    instance,
                    coordinator,
                    coordinator.terminal_code or failure_code or _PROCESS_EXITED,
                    deadline,
                )
            except BaseException as error:
                _record_cleanup_error(result, current_errors, "evidence", error)
        evidence_ready = (
            outcome != "failure"
            or not _failure_evidence_required(coordinator)
            or result.evidence == "success"
        )
        if all_generations_released and recovery_stopped and evidence_ready:
            try:
                outcome = _linearize_terminal_outcome(
                    instance, coordinator, deadline, commit_success=True
                )
            except BaseException as error:
                terminal_outcome_ready = False
                _record_cleanup_error(result, current_errors, "heartbeat_join", error)
            else:
                terminal_outcome_ready = True
                if (
                    outcome == "failure"
                    and result.evidence != "success"
                    and _failure_evidence_required(coordinator)
                ):
                    try:
                        _ensure_failure_evidence(
                            instance,
                            coordinator,
                            coordinator.terminal_code
                            or failure_code
                            or _PROCESS_EXITED,
                            deadline,
                        )
                    except BaseException as error:
                        _record_cleanup_error(
                            result, current_errors, "evidence", error
                        )
                evidence_ready = (
                    outcome != "failure"
                    or not _failure_evidence_required(coordinator)
                    or result.evidence == "success"
                )
                if evidence_ready:
                    try:
                        _stop_heartbeat_owner(
                            coordinator, deadline, allow_expired=True
                        )
                        result.succeeded("heartbeat_join")
                    except BaseException as error:
                        _record_cleanup_error(
                            result, current_errors, "heartbeat_join", error
                        )

    heartbeat_stopped = (
        coordinator.heartbeat_thread is None
        or not coordinator.heartbeat_thread.is_alive()
    )
    recovery_stopped = (
        coordinator.recovery_thread is None
        or not coordinator.recovery_thread.is_alive()
    )
    owner = coordinator.owner_directory
    ownership_stopped = (
        all_generations_released and heartbeat_stopped and recovery_stopped
    )

    if terminal and ownership_stopped:
        try:
            _drain_terminal_failures(instance, coordinator, deadline)
            outcome = _linearize_terminal_outcome(
                instance, coordinator, deadline, commit_success=True
            )
        except BaseException as error:
            terminal_outcome_ready = False
            _record_cleanup_error(result, current_errors, "recovery_join", error)
            outcome = coordinator.terminal_outcome
        else:
            terminal_outcome_ready = True
        if outcome == "failure" and _failure_evidence_required(coordinator):
            try:
                _ensure_failure_evidence(
                    instance,
                    coordinator,
                    coordinator.terminal_code or failure_code or _PROCESS_EXITED,
                    deadline,
                )
            except BaseException as error:
                _record_cleanup_error(result, current_errors, "evidence", error)

    if terminal and ownership_stopped and terminal_outcome_ready and owner is not None:
        evidence_ready = (
            coordinator.terminal_outcome != "failure"
            or not _failure_evidence_required(coordinator)
            or result.evidence == "success"
        )
        if evidence_ready:
            try:
                _acquire_lease(coordinator, deadline, allow_expired=True)
                try:
                    owner.remove_lease()
                finally:
                    _release_lease(coordinator)
                result.succeeded("lease_removal")
            except BaseException as error:
                _record_cleanup_error(result, current_errors, "lease_removal", error)
            if result.lease_removal == "success":
                if coordinator.terminal_outcome == "success":
                    try:
                        owner.remove_success_scratch()
                        result.succeeded("scratch_removal")
                    except BaseException as error:
                        _record_cleanup_error(
                            result, current_errors, "scratch_removal", error
                        )
                else:
                    result.succeeded("scratch_removal", "not_applicable")
                if result.scratch_removal in {"success", "not_applicable"}:
                    try:
                        owner.close()
                        result.succeeded("owner_handles")
                    except BaseException as error:
                        _record_cleanup_error(
                            result, current_errors, "owner_handles", error
                        )

    try:
        _acquire_lifecycle(coordinator, deadline, allow_expired=True)
    except BaseException as error:
        _record_cleanup_error(result, current_errors, "generation_joins", error)
        result.ownership_pending = True
        return current_errors
    try:
        coordinator.retired = [
            generation for generation in coordinator.retired if not generation.released
        ]
        if coordinator.candidate is not None and coordinator.candidate.released:
            coordinator.candidate = None
        owner = coordinator.owner_directory
        if owner is not None and owner._closed:
            coordinator.owner_directory = None
        if (
            terminal
            and coordinator.active is not None
            and coordinator.active.released
            and coordinator.owner_directory is None
        ):
            coordinator.active = None
        if terminal:
            with coordinator.terminal_state_lock:
                success_committed = coordinator.success_committed
                mandatory_failure_pending = (
                    coordinator.mandatory_failure_intent is not None
                    and coordinator.terminal_outcome != "failure"
                )
            result.ownership_pending = (
                _coordinator_has_ownership_locked(coordinator)
                or not terminal_outcome_ready
                or mandatory_failure_pending
                or (
                    coordinator.terminal_outcome == "failure"
                    and result.evidence != "success"
                )
                or (
                    coordinator.terminal_outcome == "success"
                    and not success_committed
                )
            )
            if not result.ownership_pending:
                coordinator.recovery_request_nonce = None
                coordinator.recovery_request_pending.clear()
        else:
            result.ownership_pending = any(
                not generation.released for generation in _generations_locked(coordinator)
            )
        if terminal:
            if result.ownership_pending:
                coordinator.phase = _LifecyclePhase.CLEANUP_PENDING
            elif coordinator.terminal_outcome == "failure":
                coordinator.phase = _LifecyclePhase.STOPPED_FAILURE
            else:
                coordinator.phase = _LifecyclePhase.STOPPED_SUCCESS
        elif result.ownership_pending:
            coordinator.phase = _LifecyclePhase.CLEANUP_PENDING
        _notify_lifecycle_locked(coordinator)
    finally:
        _release_lifecycle(coordinator)
    if terminal and instance is not None and not result.ownership_pending:
        atexit.unregister(instance._atexit_close)
    if terminal and not result.ownership_pending:
        _unregister_startup_cleanup(coordinator)
    return current_errors


def _coordinator_has_ownership_locked(coordinator: _LifecycleCoordinator) -> bool:
    generations = _generations_locked(coordinator)
    if any(not generation.released for generation in generations):
        return True
    for thread in (coordinator.heartbeat_thread, coordinator.recovery_thread):
        if thread is not None and thread.is_alive():
            return True
    owner = coordinator.owner_directory
    return owner is not None and not owner._closed


def _coordinator_has_ownership(coordinator: _LifecycleCoordinator) -> bool:
    if not coordinator.lock.acquire(timeout=_GRACEFUL_CLEANUP_SECONDS):
        return True
    try:
        return _coordinator_has_ownership_locked(coordinator)
    finally:
        coordinator.lock.release()


def _validated_command(command: Sequence[str], cwd: Path) -> list[str]:
    if isinstance(command, (str, bytes)) or not isinstance(command, Sequence):
        raise TypeError("command must be a sequence of strings")
    if not command:
        raise ValueError("command must not be empty")
    arguments = list(command)
    for argument in arguments:
        if not isinstance(argument, str):
            raise TypeError("command arguments must be strings")
        if "\0" in argument:
            raise ValueError("command arguments must not contain NUL")
    if not arguments[0]:
        raise ValueError("command executable must not be empty")

    executable = Path(arguments[0])
    if executable.is_absolute():
        resolved = executable.resolve()
    elif executable.parent != Path("."):
        resolved = (cwd / executable).resolve()
    else:
        found = shutil.which(arguments[0], path=lsp_environment().get("PATH"))
        if found is None:
            raise FileNotFoundError(arguments[0])
        resolved = Path(found).resolve()
    if os.name == "nt" and resolved.suffix.casefold() in {".bat", ".cmd"}:
        raise ValueError("Windows shell scripts are not valid LSP executables")
    if not resolved.is_file():
        raise FileNotFoundError(arguments[0])
    if os.name == "posix" and not os.access(resolved, os.X_OK):
        raise ValueError("LSP executable is not executable")
    arguments[0] = str(resolved)
    return arguments


def _validated_owner_root(owner_root: Path) -> str:
    owner_nonce = owner_root.name
    if re.fullmatch(r"[0-9a-f]{32}", owner_nonce) is None:
        raise ValueError(
            "owner_root basename must be 32 lowercase hexadecimal characters"
        )
    if os.path.lexists(owner_root):
        raise FileExistsError(owner_root)
    if not owner_root.parent.exists() or not owner_root.parent.is_dir():
        raise FileNotFoundError(owner_root.parent)
    return owner_nonce


def _object_identity(path: Path) -> _ObjectIdentity:
    return _identity_from_stat(path.lstat())


def _identity_from_stat(info: os.stat_result) -> _ObjectIdentity:
    return _ObjectIdentity(
        int(info.st_dev),
        int(info.st_ino),
        stat.S_IFMT(info.st_mode),
        int(getattr(info, "st_file_attributes", 0)) & _FILE_ATTRIBUTE_REPARSE_POINT,
    )


def _require_directory_identity(identity: _ObjectIdentity, label: str) -> None:
    if identity.file_type == stat.S_IFLNK or identity.reparse_attributes:
        raise ValueError(f"{label} must not be a symlink or reparse point")
    if identity.file_type != stat.S_IFDIR:
        raise ValueError(f"{label} must be a directory")


def _require_file_identity(identity: _ObjectIdentity, label: str) -> None:
    if identity.file_type != stat.S_IFREG or identity.reparse_attributes:
        raise PermissionError(f"{label} has an unsafe identity")


def _verify_descriptor(
    descriptor: int,
    expected: _ObjectIdentity,
    *,
    mode: int,
    directory: bool,
) -> None:
    current = _identity_from_stat(os.fstat(descriptor))
    expected_type = stat.S_IFDIR if directory else stat.S_IFREG
    if current != expected or current.file_type != expected_type:
        raise PermissionError("held LSP artifact identity changed")
    if stat.S_IMODE(os.fstat(descriptor).st_mode) != mode:
        raise PermissionError("held LSP artifact is not owner-only")


def _current_identity(path: Path) -> _ObjectIdentity | None:
    try:
        return _object_identity(path)
    except (FileNotFoundError, OSError):
        return None


def _secure_windows_owner_root(path: Path, deadline: float) -> None:
    identity = _windows_acl_identity()
    changed = _run_windows_owner_acl(
        [
            "icacls",
            str(path),
            "/inheritance:r",
            "/grant:r",
            f"{identity}:(OI)(CI)(F)",
        ],
        deadline,
    )
    if changed.returncode != 0:
        raise PermissionError("could not apply owner-only LSP ACL")
    verified = _run_windows_owner_acl(["icacls", str(path)], deadline)
    if verified.returncode != 0:
        raise PermissionError("could not verify owner-only LSP ACL")
    acl_lines = [
        line.strip()
        for line in _acl_output_text(verified.stdout).splitlines()
        if ":(" in line
    ]
    if len(acl_lines) != 1:
        raise PermissionError("owner-only LSP ACL verification was ambiguous")
    owner_ace = acl_lines[0]
    markers = re.findall(r"\([^)]+\)", owner_ace)
    if (
        _acl_principal(path, owner_ace).casefold() != identity.casefold()
        or len(markers) != 3
        or set(markers) != {"(OI)", "(CI)", "(F)"}
    ):
        raise PermissionError("owner-only LSP ACL verification failed")


def _run_windows_owner_acl(
    command: Sequence[str], deadline: float
) -> _subprocess.CompletedProcess[bytes]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise PermissionError("owner-only LSP ACL deadline expired")
    try:
        result = _subprocess.run(
            list(command),
            shell=False,
            check=False,
            capture_output=True,
            timeout=remaining,
        )
    except (OSError, _subprocess.TimeoutExpired) as exc:
        raise PermissionError("owner-only LSP ACL command failed") from exc
    if len(result.stdout or b"") + len(result.stderr or b"") > _MAX_ACL_OUTPUT_BYTES:
        raise PermissionError("owner-only LSP ACL output exceeded its byte bound")
    return result


def _write_all_descriptor(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError("LSP evidence write made no progress")
        offset += written


def _new_generation_nonce() -> str:
    nonce = secrets.token_hex(16)
    if re.fullmatch(r"[0-9a-f]{32}", nonce) is None:
        raise ValueError("generation nonce must be 32 lowercase hexadecimal characters")
    return nonce


def _write_owner_record(
    owner_directory: _OwnerDirectory,
    record: Mapping[str, object],
) -> None:
    owner_directory.write_record("owner.json", record)


def _drain_stderr(
    stream: BinaryIO,
    chunks: deque[bytes],
    size: list[int],
    lock: threading.Lock,
) -> None:
    try:
        while True:
            chunk = stream.read(_STDERR_CHUNK_BYTES)
            if not chunk:
                return
            with lock:
                chunks.append(chunk)
                size[0] += len(chunk)
                excess = size[0] - MAX_STDERR_BYTES
                while excess > 0:
                    oldest = chunks[0]
                    if len(oldest) <= excess:
                        chunks.popleft()
                        size[0] -= len(oldest)
                        excess -= len(oldest)
                    else:
                        chunks[0] = oldest[excess:]
                        size[0] -= excess
                        excess = 0
    except (OSError, ValueError):
        return
    finally:
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def _validated_deadline(deadline: float) -> float:
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
        raise TypeError("deadline must be a monotonic timestamp")
    if not math.isfinite(deadline):
        raise ValueError("deadline must be finite")
    return float(deadline)


def _write_failure_record(
    owner_directory: _OwnerDirectory,
    *,
    code: str,
    owner_nonce: str,
    generation_nonce: str,
    pid: int | None,
) -> None:
    failure_record: dict[str, object] = {
        "code": code,
        "generation_nonce": generation_nonce,
        "owner_nonce": owner_nonce,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    if pid is not None:
        failure_record["server_pid"] = pid
    _validate_failure_record(
        failure_record,
        code=code,
        owner_nonce=owner_nonce,
        generation_nonce=generation_nonce,
        pid=pid,
    )
    # A verified Windows owner DACL has one inheritable owner-only (OI)(CI) ACE.
    owner_directory.write_record("failure.json", failure_record)


def _validate_failure_record(
    record: Mapping[str, object],
    *,
    code: str,
    owner_nonce: str,
    generation_nonce: str,
    pid: int | None,
) -> None:
    required = {"code", "generation_nonce", "owner_nonce", "timestamp"}
    if set(record) not in (required, required | {"server_pid"}):
        raise ValueError("LSP failure evidence has an invalid shape")
    observed_code = record["code"]
    if not isinstance(observed_code, str) or re.fullmatch(
        r"[a-z0-9_]{1,64}", observed_code
    ) is None:
        raise ValueError("LSP failure evidence has an invalid code")
    for name in ("generation_nonce", "owner_nonce"):
        value = record[name]
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{32}", value) is None:
            raise ValueError(f"LSP failure evidence has an invalid {name}")
    timestamp = record["timestamp"]
    if not isinstance(timestamp, str) or re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{6})?Z", timestamp
    ) is None:
        raise ValueError("LSP failure evidence has an invalid timestamp")
    try:
        parsed = datetime.fromisoformat(timestamp[:-1] + "+00:00")
    except ValueError as error:
        raise ValueError("LSP failure evidence has an invalid timestamp") from error
    if (
        parsed.tzinfo != timezone.utc
        or parsed.isoformat().replace("+00:00", "Z") != timestamp
    ):
        raise ValueError("LSP failure evidence has an invalid timestamp")
    if "server_pid" in record:
        server_pid = record["server_pid"]
        if (
            isinstance(server_pid, bool)
            or not isinstance(server_pid, int)
            or server_pid <= 0
        ):
            raise ValueError("LSP failure evidence has an invalid server_pid")
    expected_fields = required | ({"server_pid"} if pid is not None else set())
    if (
        set(record) != expected_fields
        or observed_code != code
        or record["owner_nonce"] != owner_nonce
        or record["generation_nonce"] != generation_nonce
        or (pid is not None and record.get("server_pid") != pid)
    ):
        raise ValueError("LSP failure evidence does not match expected terminal identity")


atexit.register(_atexit_cleanup_startups)
