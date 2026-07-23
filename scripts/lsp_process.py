"""Own one minimally privileged, bounded LSP child process."""

from __future__ import annotations

import atexit
import json
import math
import os
import re
import secrets
import shutil
import stat
import subprocess as _subprocess
import threading
import time
import weakref
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
from lsp_protocol import CancellationToken, LspProtocol, ProtocolViolation

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


class StartupCleanupError(RuntimeError):
    """Startup failed and the immediate child could not be proven dead."""


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
    _closed: bool = False

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

        owner = _windows_workspace.create_directory(self.parent_handle, self.owner_root.name)
        self.owner_handle = owner
        self.owner_identity = _windows_workspace.identity(owner, directory=True)
        _secure_windows_owner_root(self.owner_root, deadline)
        if _windows_workspace.identity(owner, directory=True) != self.owner_identity:
            raise PermissionError("LSP owner root identity changed during ACL setup")
        self.owner_permissions_verified = True
        cancellation = _windows_workspace.create_directory(owner, "cancellation")
        try:
            cancellation_identity = _windows_workspace.identity(cancellation, directory=True)
            if _windows_workspace.identity(cancellation, directory=True) != cancellation_identity:
                raise PermissionError("LSP cancellation root identity changed during ACL setup")
        finally:
            _windows_workspace.close_handle(cancellation)

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
        if os.name == "posix":
            descriptor = os.open(
                name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=self.owner_handle,
            )
            try:
                identity = _identity_from_stat(os.fstat(descriptor))
                _require_file_identity(identity, "LSP evidence record")
                _write_all_descriptor(descriptor, payload)
                os.fchmod(descriptor, 0o600)
                os.fsync(descriptor)
                _verify_descriptor(descriptor, identity, mode=0o600, directory=False)
            finally:
                os.close(descriptor)
            os.fsync(self.owner_handle)
            return

        if not self.owner_permissions_verified:
            raise PermissionError("LSP owner ACL was not verified before evidence creation")
        handle = _windows_workspace.create_file(self.owner_handle, name)
        try:
            identity = _windows_workspace.identity(handle, directory=False)
            _windows_workspace.write_all(handle, payload, chunk_bytes=_MAX_EVIDENCE_BYTES)
            _windows_workspace.flush_file(handle)
            if _windows_workspace.identity(handle, directory=False) != identity:
                raise PermissionError("LSP evidence identity changed during write")
        finally:
            _windows_workspace.close_handle(handle)

    def write_lease(self, record: Mapping[str, object]) -> None:
        if self.owner_handle is None:
            raise RuntimeError("LSP owner directory is closed")
        payload = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(payload) > _MAX_EVIDENCE_BYTES:
            raise ValueError("LSP lease exceeds its byte bound")
        temporary = f".lease-{secrets.token_hex(8)}.tmp"
        if os.name == "posix":
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
            try:
                identity = _identity_from_stat(os.fstat(descriptor))
                _require_file_identity(identity, "LSP lease")
                _write_all_descriptor(descriptor, payload)
                os.fchmod(descriptor, 0o600)
                os.fsync(descriptor)
                _verify_descriptor(descriptor, identity, mode=0o600, directory=False)
            finally:
                os.close(descriptor)
            try:
                os.replace(
                    temporary,
                    "lease.json",
                    src_dir_fd=self.owner_handle,
                    dst_dir_fd=self.owner_handle,
                )
                os.fsync(self.owner_handle)
            except BaseException:
                try:
                    os.unlink(temporary, dir_fd=self.owner_handle)
                except OSError:
                    pass
                raise
            return

        handle = _windows_workspace.create_file(self.owner_handle, temporary)
        try:
            _windows_workspace.write_all(handle, payload, chunk_bytes=_MAX_EVIDENCE_BYTES)
            _windows_workspace.flush_file(handle)
            _windows_workspace.replace_file(handle, self.owner_handle, "lease.json")
        except BaseException:
            try:
                _windows_workspace.delete_handle(handle)
            except OSError:
                pass
            raise
        finally:
            _windows_workspace.close_handle(handle)
        _windows_workspace.flush_directory(self.owner_handle)

    def remove_lease(self) -> None:
        if self.owner_handle is None:
            return
        if os.name == "posix":
            try:
                os.unlink("lease.json", dir_fd=self.owner_handle)
                os.fsync(self.owner_handle)
            except FileNotFoundError:
                pass
            return
        try:
            handle = _windows_workspace.open_deletable_file(
                self.owner_handle, "lease.json"
            )
        except FileNotFoundError:
            return
        try:
            _windows_workspace.delete_handle(handle)
        finally:
            _windows_workspace.close_handle(handle)

    def remove_success_scratch(self) -> None:
        if self.owner_handle is None:
            return
        self.remove_lease()
        if os.name == "posix":
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
            os.rmdir(self.owner_root.name, dir_fd=self.parent_handle)
            os.fsync(self.parent_handle)
            return
        for name in ("owner.json", "failure.json"):
            try:
                handle = _windows_workspace.open_deletable_file(self.owner_handle, name)
            except FileNotFoundError:
                continue
            try:
                _windows_workspace.delete_handle(handle)
            finally:
                _windows_workspace.close_handle(handle)
        try:
            cancellation = _windows_workspace.open_deletable_directory(
                self.owner_handle, "cancellation"
            )
        except FileNotFoundError:
            cancellation = None
        if cancellation is not None:
            try:
                _windows_workspace.delete_handle(cancellation)
            finally:
                _windows_workspace.close_handle(cancellation)
        original = self.owner_handle
        expected = self.owner_identity
        self.owner_handle = None
        _windows_workspace.close_handle(original)
        owner = _windows_workspace.open_deletable_directory(
            self.parent_handle, self.owner_root.name
        )
        try:
            if _windows_workspace.identity(owner, directory=True) != expected:
                raise PermissionError("LSP owner root identity changed before deletion")
            _windows_workspace.delete_handle(owner)
        finally:
            _windows_workspace.close_handle(owner)

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

        parent = _windows_workspace.open_directory_path(self.owner_root.parent)
        named: int | None = None
        try:
            if _windows_workspace.identity(parent, directory=True) != self.parent_identity:
                raise RuntimeError("owner_root parent identity changed during startup")
            named = _windows_workspace.open_directory(parent, self.owner_root.name)
            if _windows_workspace.identity(named, directory=True) != self.owner_identity:
                raise RuntimeError("LSP owner root identity changed during startup")
            if (
                _windows_workspace.identity(self.owner_handle, directory=True)
                != self.owner_identity
            ):
                raise RuntimeError("held LSP owner root identity changed during startup")
        finally:
            if named is not None:
                _windows_workspace.close_handle(named)
            _windows_workspace.close_handle(parent)

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            owner = self.owner_handle
            self.owner_handle = None
            parent = self.parent_handle
            self.parent_handle = -1
        first_error: BaseException | None = None
        for handle in (owner, parent):
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
        if first_error is not None:
            raise first_error


class ProcessState(str, Enum):
    PROCESS_RUNNING = "process_running"
    PROTOCOL_INITIALIZED = "protocol_initialized"
    WORKSPACE_READY = "workspace_ready"
    DEGRADED = "degraded"
    FAILED = "failed"


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


@dataclass
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
    _stderr: deque[bytes] = field(default_factory=deque, init=False, repr=False)
    _stderr_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )
    _state_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )
    _owner_handle_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )
    _stderr_thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _exit_thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _owner_directory: _OwnerDirectory | None = field(default=None, init=False, repr=False)
    _startup_complete: bool = field(default=False, init=False, repr=False)
    _tree: ProcessTree | None = field(default=None, init=False, repr=False)
    _command: tuple[str, ...] = field(default=(), init=False, repr=False)
    _cwd: Path | None = field(default=None, init=False, repr=False)
    _environment: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _lifecycle_lock: threading.RLock = field(
        default_factory=threading.RLock, init=False, repr=False
    )
    _closing: bool = field(default=False, init=False, repr=False)
    _heartbeat_stop: threading.Event = field(
        default_factory=threading.Event, init=False, repr=False
    )
    _heartbeat_thread: threading.Thread | None = field(default=None, init=False, repr=False)

    @classmethod
    def start(
        cls, command: Sequence[str], *, cwd: Path, owner_root: Path
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

        owner_directory: _OwnerDirectory | None = None
        tree: ProcessTree | None = None
        process: subprocess.Popen[bytes] | None = None
        protocol: LspProtocol | None = None
        stderr_thread: threading.Thread | None = None
        exit_thread: threading.Thread | None = None
        instance: LspProcess | None = None
        instance_lock = threading.Lock()
        failed_before_instance = False
        startup_complete = False
        startup_deadline: float | None = None

        def protocol_failed(_reason: str) -> None:
            nonlocal failed_before_instance
            with instance_lock:
                current = instance
                if current is None or not startup_complete:
                    failed_before_instance = True
                    return
            current._generation_failed(generation_nonce)

        try:
            owner_directory = _OwnerDirectory.open(owner_root)
            startup_deadline = time.monotonic() + _STARTUP_WAIT_SECONDS
            owner_directory.create(startup_deadline)
            tree = ProcessTree.spawn(arguments, cwd=cwd, env=environment)
            process = tree.process
            if process.stdin is None or process.stdout is None or process.stderr is None:
                raise RuntimeError("LSP process pipes were not created")

            stderr_parts: deque[bytes] = deque()
            stderr_lock = threading.Lock()
            stderr_size = [0]
            stderr_thread = threading.Thread(
                target=_drain_stderr,
                args=(process.stderr, stderr_parts, stderr_size, stderr_lock),
                name=f"lsp-stderr-{generation_nonce}",
                daemon=True,
            )
            stderr_thread.start()
            owner_directory.verify_lexical_identity()

            owner_record: dict[str, object] = {
                "command_basename": Path(arguments[0]).name,
                "generation_nonce": generation_nonce,
                "owner_nonce": owner_nonce,
                "owner_pid": process.pid,
                "started_at": started_at,
                "state": ProcessState.PROCESS_RUNNING.value,
            }
            _write_owner_record(
                owner_directory,
                owner_record,
            )
            owner_directory.verify_lexical_identity()
            protocol = LspProtocol(
                process.stdout,
                process.stdin,
                generation_nonce,
                fatal_callback=protocol_failed,
            )
            owner_directory.verify_lexical_identity()
            new_instance = cls(
                process=process,
                protocol=protocol,
                owner_root=owner_root,
                owner_nonce=owner_nonce,
                generation_nonce=generation_nonce,
                state=ProcessState.PROCESS_RUNNING,
                started_monotonic=started_monotonic,
                last_used_monotonic=started_monotonic,
            )
            new_instance._stderr = stderr_parts
            new_instance._stderr_lock = stderr_lock
            new_instance._stderr_thread = stderr_thread
            new_instance._owner_directory = owner_directory
            new_instance._tree = tree
            new_instance._command = tuple(arguments)
            new_instance._cwd = cwd
            new_instance._environment = environment
            with instance_lock:
                instance = new_instance
            exit_thread = threading.Thread(
                target=instance._monitor_exit,
                args=(process, protocol, generation_nonce),
                name=f"lsp-exit-{generation_nonce}",
                daemon=True,
            )
            instance._exit_thread = exit_thread
            exit_thread.start()
            instance._start_heartbeat()
            owner_directory.verify_lexical_identity()
            with instance_lock:
                if process.poll() is not None or failed_before_instance:
                    raise RuntimeError("LSP process exited during startup")
                with instance._owner_handle_lock:
                    instance._startup_complete = True
                startup_complete = True
            atexit.register(_atexit_close, weakref.ref(instance))
            return instance
        except BaseException as startup_error:
            if instance is not None:
                instance._stop_heartbeat(
                    startup_deadline
                    if startup_deadline is not None
                    else time.monotonic()
                )
            if owner_directory is not None:
                try:
                    owner_directory.remove_lease()
                except OSError:
                    pass
            try:
                _rollback_startup(
                    process,
                    protocol,
                    (stderr_thread, exit_thread),
                    owner_directory,
                    deadline=(
                        startup_deadline
                        if startup_deadline is not None
                        else time.monotonic()
                    ),
                    owner_nonce=owner_nonce,
                    generation_nonce=generation_nonce,
                    tree=tree,
                )
            except StartupCleanupError as cleanup_error:
                raise cleanup_error from startup_error
            raise

    def request(
        self,
        method: str,
        params: object,
        *,
        deadline: float,
        cancellation: CancellationToken | None = None,
    ) -> object:
        deadline = _validated_deadline(deadline)
        attempted_generation = self.generation_nonce
        for attempt in range(2):
            with self._lifecycle_lock:
                if self.state is ProcessState.FAILED:
                    raise RuntimeError("LSP process has exited")
                protocol = self.protocol
                process = self.process
                expired = protocol.expired_drain_keys(time.monotonic())
                if expired or process.poll() is not None or protocol.fatal:
                    self.restart(deadline)
                    protocol = self.protocol
                    process = self.process
                with self._state_lock:
                    self.last_used_monotonic = time.monotonic()
            try:
                return protocol.request(
                    method, params, deadline=deadline, cancellation=cancellation
                )
            except ProtocolViolation as error:
                if isinstance(error.__cause__, TimeoutError):
                    raise
                with self._lifecycle_lock:
                    changed = self.generation_nonce != attempted_generation
                    if attempt or self.restart_count >= 1 and not changed:
                        self._terminal_failure(_PROCESS_EXITED, deadline)
                        raise
                    if not changed:
                        self.restart(deadline)
                    attempted_generation = self.generation_nonce
        raise RuntimeError("LSP request retry invariant breached")

    def shutdown(self, deadline: float) -> None:
        deadline = _validated_deadline(deadline)
        with self._lifecycle_lock:
            if self._closing and self.process.poll() is not None:
                return
            self._closing = True
            graceful_deadline = min(deadline, time.monotonic() + _GRACEFUL_CLEANUP_SECONDS)
            graceful = False
            if self.process.poll() is None and time.monotonic() < graceful_deadline:
                try:
                    self.protocol.request(
                        "shutdown", {}, deadline=graceful_deadline
                    )
                    self.protocol.notify("exit", {}, deadline=graceful_deadline)
                    self.wait_for_exit(graceful_deadline)
                    graceful = True
                except (OSError, RuntimeError, TimeoutError):
                    pass
            if not graceful and self.process.poll() is None:
                tree = self._tree
                if tree is not None:
                    tree.terminate(deadline=deadline)
            self._finish_generation(deadline)

    def cancel_all(self, reason: str) -> None:
        with self._lifecycle_lock:
            if self.state is ProcessState.FAILED:
                return
            self.protocol.cancel_all(reason)

    def restart(self, deadline: float) -> None:
        deadline = _validated_deadline(deadline)
        with self._lifecycle_lock:
            if self.restart_count >= 1:
                self._terminal_failure(_PROCESS_EXITED, deadline)
                raise ProtocolViolation("LSP process restart limit exceeded")
            if self._cwd is None or not self._command:
                raise RuntimeError("LSP restart inputs are unavailable")
            old_protocol = self.protocol
            old_tree = self._tree
            old_process = self.process
            old_protocol.close(deadline)
            if old_tree is not None and old_process.poll() is None:
                old_tree.terminate(deadline=deadline)
            if old_tree is not None:
                old_tree.close()
            self._join_generation_threads(deadline)

            generation_nonce = _new_generation_nonce()
            self.restart_count += 1
            self.generation_nonce = generation_nonce
            try:
                tree = ProcessTree.spawn(
                    self._command, cwd=self._cwd, env=self._environment
                )
            except BaseException:
                with self._state_lock:
                    self.state = ProcessState.FAILED
                owner = self._owner_directory
                if owner is not None:
                    try:
                        _write_failure_record(
                            owner,
                            code="restart_failed",
                            owner_nonce=self.owner_nonce,
                            generation_nonce=generation_nonce,
                            pid=old_process.pid,
                        )
                    except (FileExistsError, OSError, RuntimeError, ValueError):
                        pass
                    self._stop_heartbeat(deadline)
                    owner.remove_lease()
                    self._owner_directory = None
                    owner.close()
                raise
            process = tree.process
            self.process = process
            self._tree = tree
            protocol: LspProtocol | None = None
            stderr_thread: threading.Thread | None = None
            try:
                if (
                    process.stdin is None
                    or process.stdout is None
                    or process.stderr is None
                ):
                    raise RuntimeError("LSP process pipes were not created")
                stderr_parts: deque[bytes] = deque()
                stderr_lock = threading.Lock()
                stderr_size = [0]
                stderr_thread = threading.Thread(
                    target=_drain_stderr,
                    args=(process.stderr, stderr_parts, stderr_size, stderr_lock),
                    name=f"lsp-stderr-{generation_nonce}",
                    daemon=True,
                )
                stderr_thread.start()

                def failed(_reason: str) -> None:
                    self._generation_failed(generation_nonce)

                protocol = LspProtocol(
                    process.stdout,
                    process.stdin,
                    generation_nonce,
                    fatal_callback=failed,
                )
                self.protocol = protocol
                self._stderr = stderr_parts
                self._stderr_lock = stderr_lock
                self._stderr_thread = stderr_thread
                self.state = ProcessState.PROCESS_RUNNING
                self._write_live_lease()
                exit_thread = threading.Thread(
                    target=self._monitor_exit,
                    args=(process, protocol, generation_nonce),
                    name=f"lsp-exit-{generation_nonce}",
                    daemon=True,
                )
                self._exit_thread = exit_thread
                exit_thread.start()
            except BaseException:
                if protocol is not None:
                    protocol.close(deadline)
                try:
                    if process.poll() is None:
                        tree.terminate(deadline=deadline)
                except (OSError, TimeoutError):
                    pass
                finally:
                    tree.close()
                    self._tree = None
                if stderr_thread is not None and stderr_thread.ident is not None:
                    remaining = deadline - time.monotonic()
                    if remaining > 0:
                        stderr_thread.join(remaining)
                with self._state_lock:
                    self.state = ProcessState.FAILED
                owner = self._owner_directory
                if owner is not None:
                    try:
                        _write_failure_record(
                            owner,
                            code="restart_failed",
                            owner_nonce=self.owner_nonce,
                            generation_nonce=generation_nonce,
                            pid=process.pid,
                        )
                    except (FileExistsError, OSError, RuntimeError, ValueError):
                        pass
                    self._stop_heartbeat(deadline)
                    owner.remove_lease()
                    self._owner_directory = None
                    owner.close()
                raise

    def close(self, deadline: float) -> None:
        deadline = _validated_deadline(deadline)
        with self._lifecycle_lock:
            if self._owner_directory is None and self.process.poll() is not None:
                return
        self.shutdown(deadline)
        self._stop_heartbeat(deadline)
        owner_directory = self._take_owner_directory()
        if owner_directory is not None:
            try:
                owner_directory.remove_success_scratch()
            finally:
                owner_directory.close()
        tree = self._tree
        self._tree = None
        if tree is not None:
            tree.close()

    def idle_expired(self, now: float) -> bool:
        now = _validated_deadline(now)
        with self._state_lock:
            return now - self.last_used_monotonic >= _IDLE_SECONDS

    def stderr_bytes(self) -> bytes:
        with self._stderr_lock:
            return b"".join(self._stderr)

    def wait_for_exit(self, deadline: float) -> int:
        deadline = _validated_deadline(deadline)
        remaining = deadline - time.monotonic()
        if remaining <= 0 and self.process.poll() is None:
            raise TimeoutError("LSP process did not exit before deadline")
        try:
            return_code = self.process.wait(timeout=max(0.0, remaining))
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError("LSP process did not exit before deadline") from exc

        for owner in (self._stderr_thread, self._exit_thread):
            if owner is None or owner is threading.current_thread():
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("LSP process streams did not drain before deadline")
            owner.join(remaining)
            if owner.is_alive():
                raise TimeoutError("LSP process streams did not drain before deadline")
        with self._owner_handle_lock:
            pass
        return return_code

    def _monitor_exit(
        self,
        process: subprocess.Popen[bytes],
        protocol: LspProtocol,
        generation_nonce: str,
    ) -> None:
        process.wait()
        if self._closing or generation_nonce != self.generation_nonce:
            return
        protocol._become_fatal("LSP process exited unexpectedly")
        self._generation_failed(generation_nonce)

    def _generation_failed(self, generation_nonce: str) -> None:
        if not self._lifecycle_lock.acquire(blocking=False):
            return
        try:
            if self._closing or generation_nonce != self.generation_nonce:
                return
            if self.restart_count >= 1:
                self._terminal_failure(
                    _PROCESS_EXITED, time.monotonic() + _GRACEFUL_CLEANUP_SECONDS
                )
            else:
                with self._state_lock:
                    self.state = ProcessState.DEGRADED
        finally:
            self._lifecycle_lock.release()

    def _terminal_failure(self, code: str, deadline: float) -> None:
        with self._owner_handle_lock:
            if not self._startup_complete:
                return
            with self._state_lock:
                self.state = ProcessState.FAILED
            owner_directory = self._owner_directory
            if owner_directory is None:
                return
            try:
                _write_failure_record(
                    owner_directory,
                    code=code,
                    owner_nonce=self.owner_nonce,
                    generation_nonce=self.generation_nonce,
                    pid=self.process.pid,
                )
            except (FileExistsError, OSError, ValueError, RuntimeError):
                pass
            finally:
                self._stop_heartbeat(deadline)
                owner_directory.remove_lease()
                tree = self._tree
                if tree is not None and self.process.poll() is None:
                    try:
                        tree.terminate(deadline=deadline)
                    except (OSError, TimeoutError):
                        pass
                if tree is not None:
                    try:
                        tree.close()
                    except OSError:
                        pass
                    self._tree = None
                try:
                    self.protocol.close(deadline)
                except (OSError, RuntimeError):
                    pass
                self._owner_directory = None
                try:
                    owner_directory.close()
                except OSError:
                    pass

    def _start_heartbeat(self) -> None:
        self._write_live_lease()
        thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"lsp-heartbeat-{self.owner_nonce}",
            daemon=True,
        )
        self._heartbeat_thread = thread
        thread.start()

    def _heartbeat_loop(self) -> None:
        while not self._heartbeat_stop.wait(_HEARTBEAT_SECONDS):
            try:
                self._write_live_lease()
            except (OSError, RuntimeError, ValueError):
                self._generation_failed(self.generation_nonce)
                return

    def _write_live_lease(self) -> None:
        owner = self._owner_directory
        if owner is None:
            return
        heartbeat = datetime.now(timezone.utc)
        expires = heartbeat + timedelta(seconds=_LEASE_EXPIRY_SECONDS)
        owner.write_lease(
            {
                "expires_at": expires.isoformat().replace("+00:00", "Z"),
                "generation_nonce": self.generation_nonce,
                "heartbeat_at": heartbeat.isoformat().replace("+00:00", "Z"),
                "manager_pid": os.getpid(),
                "owner_nonce": self.owner_nonce,
                "schema_version": 1,
                "server_pid": self.process.pid,
                "state": "live",
            }
        )

    def _stop_heartbeat(self, deadline: float) -> None:
        self._heartbeat_stop.set()
        thread = self._heartbeat_thread
        if thread is None or thread is threading.current_thread():
            return
        if thread.ident is None and thread not in threading.enumerate():
            return
        remaining = deadline - time.monotonic()
        if remaining > 0:
            thread.join(remaining)

    def _finish_generation(self, deadline: float) -> None:
        try:
            self.protocol._finish_io_after_process_exit(deadline)
        except (OSError, RuntimeError):
            pass
        self._join_generation_threads(deadline)

    def _join_generation_threads(self, deadline: float) -> None:
        for thread in (self._stderr_thread, self._exit_thread):
            if thread is None or thread is threading.current_thread():
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            thread.join(remaining)

    def _take_owner_directory(self) -> _OwnerDirectory | None:
        with self._owner_handle_lock:
            owner = self._owner_directory
            self._owner_directory = None
            return owner


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


def _rollback_startup(
    process: subprocess.Popen[bytes] | None,
    protocol: LspProtocol | None,
    threads: tuple[threading.Thread | None, ...],
    owner_directory: _OwnerDirectory | None,
    *,
    deadline: float,
    owner_nonce: str,
    generation_nonce: str,
    tree: ProcessTree | None = None,
) -> None:
    # Task 5 retains bounded immutable evidence; later lifecycle/doctor tasks own removal.
    evidence_error: BaseException | None = None
    if (
        deadline - time.monotonic() > 0
        and owner_directory is not None
        and owner_directory.owner_handle is not None
        and owner_directory.owner_permissions_verified
    ):
        try:
            _write_failure_record(
                owner_directory,
                code=_STARTUP_FAILED,
                owner_nonce=owner_nonce,
                generation_nonce=generation_nonce,
                pid=process.pid if process is not None else None,
            )
        except FileExistsError:
            pass
        except BaseException as error:
            evidence_error = error
    if protocol is not None:
        try:
            protocol._stop_io_for_process_cleanup()
        except BaseException:
            pass
    child_alive = process is not None and process.poll() is None
    if child_alive:
        if tree is not None:
            try:
                tree.terminate(deadline=deadline)
            except (OSError, TimeoutError):
                pass
        else:
            try:
                process.terminate()
            except OSError:
                pass
            remaining = deadline - time.monotonic()
            if remaining > 0:
                try:
                    process.wait(timeout=remaining / 2)
                except (subprocess.TimeoutExpired, OSError):
                    pass
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    try:
                        process.wait(timeout=remaining)
                    except (subprocess.TimeoutExpired, OSError):
                        pass
    try:
        child_still_alive = process is not None and process.poll() is None
        if child_still_alive:
            if evidence_error is not None:
                raise StartupCleanupError(
                    "LSP direct child remains alive and failure evidence could not be written"
                ) from evidence_error
            raise StartupCleanupError("LSP direct child remains alive after startup cleanup")

        if protocol is not None and deadline - time.monotonic() > 0:
            try:
                protocol._finish_io_after_process_exit(deadline)
            except BaseException:
                pass
        if process is not None:
            for stream in (process.stdin, process.stdout, process.stderr):
                if deadline - time.monotonic() <= 0:
                    break
                if stream is not None:
                    try:
                        stream.close()
                    except (OSError, ValueError):
                        pass
        for thread in threads:
            if deadline - time.monotonic() <= 0:
                break
            if thread is not None and (
                thread.ident is not None or thread in threading.enumerate()
            ):
                _join_partially_started_thread(thread, deadline)
        if evidence_error is not None:
            raise StartupCleanupError(
                "LSP startup failed and retained evidence could not be written safely"
            ) from evidence_error
    finally:
        if tree is not None:
            try:
                tree.close()
            except OSError:
                pass
        if owner_directory is not None:
            try:
                owner_directory.close()
            except OSError:
                pass


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
    # A verified Windows owner DACL has one inheritable owner-only (OI)(CI) ACE.
    owner_directory.write_record("failure.json", failure_record)


def _join_partially_started_thread(
    thread: threading.Thread, deadline: float
) -> None:
    while thread in threading.enumerate():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        try:
            thread.join(remaining)
            return
        except RuntimeError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.001, remaining))


def _atexit_close(reference: weakref.ReferenceType[LspProcess]) -> None:
    instance = reference()
    if instance is None:
        return
    try:
        instance.close(time.monotonic() + _GRACEFUL_CLEANUP_SECONDS)
    except BaseException:
        pass
