"""Own one minimally privileged, bounded LSP child process."""

from __future__ import annotations

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
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import BinaryIO

from compile_cache import _restrict_owner_only, _verify_owner_only
from lsp_protocol import CancellationToken, LspProtocol


class _SubprocessFacade:
    Popen = _subprocess.Popen
    PIPE = _subprocess.PIPE
    TimeoutExpired = _subprocess.TimeoutExpired


subprocess = _SubprocessFacade()

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


class StartupCleanupError(RuntimeError):
    """Startup failed and the immediate child could not be proven dead."""


@dataclass(frozen=True, slots=True)
class _ObjectIdentity:
    device: int
    inode: int
    file_type: int
    reparse_attributes: int


@dataclass(frozen=True, slots=True)
class _CreatedArtifact:
    path: Path
    identity: _ObjectIdentity
    is_directory: bool


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
    _stderr: deque[bytes] = field(default_factory=deque, init=False, repr=False)
    _stderr_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )
    _state_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )
    _stderr_thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _exit_thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _parent_identity: _ObjectIdentity | None = field(default=None, init=False, repr=False)
    _artifacts: list[_CreatedArtifact] = field(default_factory=list, init=False, repr=False)

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
        parent_identity = _parent_identity(owner_root.parent)
        generation_nonce = _new_generation_nonce()
        started_monotonic = time.monotonic()
        started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        process: subprocess.Popen[bytes] | None = None
        protocol: LspProtocol | None = None
        stderr_thread: threading.Thread | None = None
        exit_thread: threading.Thread | None = None
        instance: LspProcess | None = None
        instance_lock = threading.Lock()
        failed_before_instance = False
        startup_complete = False
        created_artifacts: list[_CreatedArtifact] = []

        def protocol_failed(_reason: str) -> None:
            nonlocal failed_before_instance
            with instance_lock:
                current = instance
                if current is None or not startup_complete:
                    failed_before_instance = True
                    return
            current._mark_failed()

        try:
            _create_owner_root(owner_root, created_artifacts, parent_identity)
            process = subprocess.Popen(
                arguments,
                cwd=cwd,
                env=environment,
                shell=False,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True,
            )
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
            _verify_startup_fence(owner_root.parent, parent_identity, created_artifacts)

            owner_record: dict[str, object] = {
                "command_basename": Path(arguments[0]).name,
                "generation_nonce": generation_nonce,
                "owner_nonce": owner_nonce,
                "owner_pid": process.pid,
                "started_at": started_at,
                "state": ProcessState.PROCESS_RUNNING.value,
            }
            _verify_startup_fence(owner_root.parent, parent_identity, created_artifacts)
            _write_owner_record(
                owner_root,
                owner_record,
                created_artifacts=created_artifacts,
            )
            _verify_startup_fence(owner_root.parent, parent_identity, created_artifacts)
            protocol = LspProtocol(
                process.stdout,
                process.stdin,
                generation_nonce,
                fatal_callback=protocol_failed,
            )
            _verify_startup_fence(owner_root.parent, parent_identity, created_artifacts)
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
            new_instance._parent_identity = parent_identity
            new_instance._artifacts = created_artifacts
            with instance_lock:
                instance = new_instance
            exit_thread = threading.Thread(
                target=instance._monitor_exit,
                name=f"lsp-exit-{generation_nonce}",
                daemon=True,
            )
            instance._exit_thread = exit_thread
            exit_thread.start()
            _verify_startup_fence(owner_root.parent, parent_identity, created_artifacts)
            with instance_lock:
                startup_complete = True
                failed_during_startup = failed_before_instance
            if failed_during_startup:
                instance._mark_failed()
            return instance
        except BaseException as startup_error:
            try:
                _rollback_startup(
                    process,
                    protocol,
                    (stderr_thread, exit_thread),
                    created_artifacts,
                    owner_root=owner_root,
                    parent_identity=parent_identity,
                    owner_nonce=owner_nonce,
                    generation_nonce=generation_nonce,
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
        if self.process.poll() is not None:
            self.protocol._become_fatal("LSP process exited unexpectedly")
            self._mark_failed()
            raise RuntimeError("LSP process has exited")
        with self._state_lock:
            if self.state is ProcessState.FAILED:
                raise RuntimeError("LSP process has exited")
            self.last_used_monotonic = time.monotonic()
        return self.protocol.request(
            method, params, deadline=deadline, cancellation=cancellation
        )

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
        return return_code

    def _monitor_exit(self) -> None:
        self.process.wait()
        self.protocol._become_fatal("LSP process exited unexpectedly")
        self._mark_failed()

    def _mark_failed(self) -> None:
        with self._state_lock:
            if self.state is ProcessState.FAILED:
                return
            self.state = ProcessState.FAILED
            try:
                if self._parent_identity is None:
                    return
                _write_failure_record(
                    self.owner_root,
                    parent_identity=self._parent_identity,
                    artifacts=self._artifacts,
                    code=_PROCESS_EXITED,
                    owner_nonce=self.owner_nonce,
                    generation_nonce=self.generation_nonce,
                    pid=self.process.pid,
                )
            except (FileExistsError, OSError, ValueError, RuntimeError):
                pass


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
    info = path.lstat()
    return _ObjectIdentity(
        int(info.st_dev),
        int(info.st_ino),
        stat.S_IFMT(info.st_mode),
        int(getattr(info, "st_file_attributes", 0)) & _FILE_ATTRIBUTE_REPARSE_POINT,
    )


def _parent_identity(parent: Path) -> _ObjectIdentity:
    identity = _object_identity(parent)
    if identity.file_type == stat.S_IFLNK or identity.reparse_attributes:
        raise ValueError("owner_root parent must not be a symlink or reparse point")
    if identity.file_type != stat.S_IFDIR:
        raise ValueError("owner_root parent must be a directory")
    return identity


def _verify_parent_identity(parent: Path, expected: _ObjectIdentity) -> None:
    if _parent_identity(parent) != expected:
        raise RuntimeError("owner_root parent identity changed during startup")


def _verify_startup_fence(
    parent: Path,
    parent_identity: _ObjectIdentity,
    artifacts: Sequence[_CreatedArtifact],
) -> None:
    _verify_parent_identity(parent, parent_identity)
    for artifact in artifacts:
        if _current_identity(artifact.path) != artifact.identity:
            raise RuntimeError("LSP owner artifact identity changed during startup")


def _current_identity(path: Path) -> _ObjectIdentity | None:
    try:
        return _object_identity(path)
    except (FileNotFoundError, OSError):
        return None


def _record_artifact(path: Path, *, is_directory: bool) -> _CreatedArtifact:
    identity = _object_identity(path)
    expected_type = stat.S_IFDIR if is_directory else stat.S_IFREG
    if identity.file_type != expected_type or identity.reparse_attributes:
        raise PermissionError("created LSP artifact has an unsafe identity")
    return _CreatedArtifact(path, identity, is_directory)


def _new_generation_nonce() -> str:
    nonce = secrets.token_hex(16)
    if re.fullmatch(r"[0-9a-f]{32}", nonce) is None:
        raise ValueError("generation nonce must be 32 lowercase hexadecimal characters")
    return nonce


def _create_owner_root(
    owner_root: Path,
    created_artifacts: list[_CreatedArtifact],
    parent_identity: _ObjectIdentity,
) -> None:
    owner_root.mkdir(mode=0o700)
    created_artifacts.append(_record_artifact(owner_root, is_directory=True))
    _verify_startup_fence(owner_root.parent, parent_identity, created_artifacts)
    _restrict_owner_only(owner_root, 0o700)
    _verify_owner_only(owner_root, 0o700)
    cancellation_root = owner_root / "cancellation"
    cancellation_root.mkdir(mode=0o700)
    created_artifacts.append(_record_artifact(cancellation_root, is_directory=True))
    _verify_startup_fence(owner_root.parent, parent_identity, created_artifacts)
    _restrict_owner_only(cancellation_root, 0o700)
    _verify_owner_only(cancellation_root, 0o700)


def _write_owner_record(
    owner_root: Path,
    record: Mapping[str, object],
    *,
    created_artifacts: list[_CreatedArtifact] | None = None,
) -> _CreatedArtifact:
    return _write_create_only_record(
        owner_root / "owner.json",
        record,
        created_artifacts=created_artifacts,
    )


def _write_create_only_record(
    target: Path,
    record: Mapping[str, object],
    *,
    created_artifacts: list[_CreatedArtifact] | None = None,
) -> _CreatedArtifact:
    payload = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
    descriptor: int | None = None
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _restrict_owner_only(target, 0o600)
        _verify_owner_only(target, 0o600)
        artifact = _record_artifact(target, is_directory=False)
        if created_artifacts is not None:
            created_artifacts.append(artifact)
        return artifact
    finally:
        if descriptor is not None:
            os.close(descriptor)


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
    created_artifacts: list[_CreatedArtifact],
    *,
    owner_root: Path,
    parent_identity: _ObjectIdentity,
    owner_nonce: str,
    generation_nonce: str,
) -> None:
    # Task 5 retains bounded immutable evidence; later lifecycle/doctor tasks own removal.
    deadline = time.monotonic() + _STARTUP_WAIT_SECONDS
    if protocol is not None:
        try:
            protocol._stop_io_for_process_cleanup()
        except BaseException:
            pass
    child_alive = process is not None and process.poll() is None
    if child_alive:
        try:
            process.terminate()
        except OSError:
            pass
        remaining = max(0.0, deadline - time.monotonic())
        try:
            process.wait(timeout=remaining / 2)
        except (subprocess.TimeoutExpired, OSError):
            try:
                process.kill()
            except OSError:
                pass
            remaining = max(0.0, deadline - time.monotonic())
            try:
                process.wait(timeout=remaining)
            except (subprocess.TimeoutExpired, OSError):
                pass
    child_still_alive = process is not None and process.poll() is None
    if child_still_alive:
        try:
            _write_failure_record(
                owner_root,
                parent_identity=parent_identity,
                artifacts=created_artifacts,
                code=_STARTUP_FAILED,
                owner_nonce=owner_nonce,
                generation_nonce=generation_nonce,
                pid=process.pid,
            )
        except FileExistsError:
            pass
        except BaseException as evidence_error:
            raise StartupCleanupError(
                "LSP direct child remains alive and failure evidence could not be written"
            ) from evidence_error
        raise StartupCleanupError("LSP direct child remains alive after startup cleanup")

    cleanup_deadline = max(deadline, time.monotonic() + 0.25)
    if protocol is not None:
        try:
            protocol._finish_io_after_process_exit(cleanup_deadline)
        except BaseException:
            pass
    if process is not None:
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
    for thread in threads:
        if thread is not None and (
            thread.ident is not None or thread in threading.enumerate()
        ):
            _join_partially_started_thread(thread, cleanup_deadline)
    try:
        _write_failure_record(
            owner_root,
            parent_identity=parent_identity,
            artifacts=created_artifacts,
            code=_STARTUP_FAILED,
            owner_nonce=owner_nonce,
            generation_nonce=generation_nonce,
            pid=process.pid if process is not None else None,
        )
    except FileExistsError:
        pass
    except BaseException as evidence_error:
        raise StartupCleanupError(
            "LSP startup failed and retained evidence could not be written safely"
        ) from evidence_error


def _write_failure_record(
    owner_root: Path,
    *,
    parent_identity: _ObjectIdentity,
    artifacts: list[_CreatedArtifact],
    code: str,
    owner_nonce: str,
    generation_nonce: str,
    pid: int | None,
) -> None:
    root_artifact = next(
        (
            artifact
            for artifact in artifacts
            if artifact.path == owner_root and artifact.is_directory
        ),
        None,
    )
    if root_artifact is None:
        raise RuntimeError("original LSP owner root identity is unavailable")
    _verify_startup_fence(owner_root.parent, parent_identity, artifacts)
    failure_record: dict[str, object] = {
        "code": code,
        "generation_nonce": generation_nonce,
        "owner_nonce": owner_nonce,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    if pid is not None:
        failure_record["owner_pid"] = pid
    _write_create_only_record(
        owner_root / "failure.json",
        failure_record,
    )


def _join_partially_started_thread(
    thread: threading.Thread, deadline: float
) -> None:
    while thread in threading.enumerate():
        try:
            thread.join(max(0.0, deadline - time.monotonic()))
            return
        except RuntimeError:
            if time.monotonic() >= deadline:
                return
            time.sleep(0.001)
