"""Bounded live workspace revisions and content-proven deltas."""

from __future__ import annotations

import hashlib
import math
import os
import re
import shutil
import signal
import stat
import struct
import subprocess
import sys
import threading
import time
import unicodedata
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows has no fcntl module
    _fcntl = None

from reliable_memory import canonical_json_bytes
from repository_scope import RepositoryScope, sanitized_git_environment

PYTHON_CONFIG_NAMES = frozenset(
    {
        ".python-version",
        "Pipfile",
        "Pipfile.lock",
        "poetry.lock",
        "pyproject.toml",
        "pyrightconfig.json",
        "setup.cfg",
        "tox.ini",
        "uv.lock",
    }
)
MAX_REVISION_FILES = 100_000
MAX_REVISION_BYTES = 2 * 1024 * 1024 * 1024
MAX_GIT_STATUS_BYTES = 16 * 1024 * 1024
GIT_STATUS_TIMEOUT_SECONDS = 5.0
_MAX_GIT_HEAD_BYTES = 65
_GIT_COMMIT_RE = re.compile(rb"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_WINDOWS_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
_PROCESS_CLEANUP_SECONDS = 0.2
_MAX_PRIVATE_INDEX_BYTES = 64 * 1024 * 1024
_MAX_PRIVATE_UNMATCHED_TRACKED_FILES = 4096
_MAX_PRIVATE_UNMATCHED_TRACKED_BYTES = 32 * 1024 * 1024
_MAX_PRIVATE_CONFIG_BYTES = 256 * 1024
_MAX_PRIVATE_ATTRIBUTES_BYTES = 256 * 1024
_MAX_PRIVATE_IGNORE_BYTES = 256 * 1024
_MAX_HEAD_FENCE_BYTES = 4096
_MAX_PRIVATE_INDEX_PATH_BYTES = 4096
_HASH_CHECK_CHUNK_BYTES = 64 * 1024
_PRIVATE_WRITE_CHUNK_BYTES = 1024 * 1024
_MAX_INVENTORY_HINT_ENTRIES = 4096
_MAX_INVENTORY_HINT_PATH_BYTES = 1024 * 1024
_UNSUPPORTED_INDEX_EXTENSIONS = frozenset({b"link", b"sdir", b"UNTR", b"FSMN"})
_SUPPORTED_INDEX_EXTENSIONS = frozenset({b"TREE", b"EOIE", b"IEOT"})
_PRIVATE_GIT_SELECTOR_ENVIRONMENT = frozenset(
    {
        "GIT_ATTR_NOSYSTEM",
        "GIT_ATTR_SOURCE",
        "GIT_CONFIG",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_SYSTEM",
        "GIT_EXEC_PATH",
    }
)
_SYSTEM_GIT_CONFIG_PATHS: tuple[Path, ...] | None = None
_SYSTEM_GIT_ATTRIBUTE_PATHS: tuple[Path, ...] | None = None


class _RevisionStopped(TimeoutError):
    """Caller-requested cancellation or deadline expiry."""


@dataclass(frozen=True, slots=True)
class RevisionEntry:
    path: str
    kind: str
    sha256: str | None
    size: int


@dataclass(frozen=True, slots=True)
class WorkspaceRevision:
    repository_id: str
    checkout_id: str
    git_head: str | None
    entries: tuple[RevisionEntry, ...]
    revision_sha256: str


@dataclass(frozen=True, slots=True)
class WorkspaceDelta:
    created: tuple[str, ...]
    changed: tuple[str, ...]
    renamed: tuple[tuple[str, str], ...]
    deleted: tuple[str, ...]
    configuration_changed: bool


_Identity = tuple[int, int, int, int, int, int]


@dataclass(frozen=True, slots=True)
class _DirectorySnapshot:
    path: Path
    resolved: Path
    identity: _Identity
    change_time_ns: int


@dataclass(frozen=True, slots=True)
class _FileSnapshot:
    path: Path
    resolved: Path
    identity: _Identity
    parents: tuple[_DirectorySnapshot, ...]


@dataclass(frozen=True, slots=True)
class _VerificationHash:
    sha256: str
    size: int
    snapshot: _FileSnapshot
    git_oid: bytes | None
    info: os.stat_result
    change_time_ns: int


@dataclass(frozen=True, slots=True)
class _GitIndexEntry:
    path: str
    offset: int
    oid: bytes
    mode: int


@dataclass(frozen=True, slots=True)
class _ParsedGitIndex:
    content: bytes | bytearray
    entries_end: int
    checksum_offset: int
    entries: tuple[_GitIndexEntry, ...]


_StrongIdentity = tuple[int, int, int, int, int, int, int]


@dataclass(frozen=True, slots=True)
class _OwnedFileFence:
    path: Path
    sha256: str
    identity: _StrongIdentity


@dataclass(frozen=True, slots=True)
class _OwnedFileRead:
    content: bytes | bytearray
    fence: _OwnedFileFence


@dataclass(frozen=True, slots=True)
class _HeadFence:
    oid: str
    head: _OwnedFileFence
    reference: _OwnedFileFence | None


@dataclass(frozen=True, slots=True)
class _SemanticsFileFence:
    root: Path
    file: _OwnedFileFence
    maximum_bytes: int


@dataclass(frozen=True, slots=True)
class _RawSemanticsProof:
    files: tuple[_SemanticsFileFence, ...]
    absent: tuple[tuple[Path, Path], ...]
    environment: tuple[tuple[str, str], ...]
    installation: _PrivateGitInstallation


@dataclass(frozen=True, slots=True)
class _PrivateGitInstallation:
    executable: Path
    identity: _StrongIdentity
    system_config_paths: tuple[Path, ...]
    system_attribute_paths: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class _PrivateGitProof:
    index: _OwnedFileFence
    head: _HeadFence
    raw_semantics: _RawSemanticsProof
    directory_change_times: tuple[tuple[_DirectorySnapshot, int], ...]
    file_change_times: tuple[tuple[_FileSnapshot, int], ...]


@dataclass(frozen=True, slots=True)
class _PrivateGitState:
    head: str | None
    status: bytes
    proof: _PrivateGitProof


@dataclass(frozen=True, slots=True)
class _RevisionInventoryHint:
    repository_id: str
    checkout_id: str
    revision_sha256: str
    root: Path
    resolved_root: Path
    directory_snapshots: tuple[_DirectorySnapshot, ...]
    entry_snapshots: tuple[tuple[Path, _StrongIdentity], ...]
    prepared_files: tuple[tuple[str, _FileSnapshot], ...]
    relevant_files: tuple[tuple[str, Path], ...]
    private_inventory_safe: bool


_INVENTORY_HINT_LOCK = threading.Lock()
_INVENTORY_HINT: _RevisionInventoryHint | None = None


def _publish_inventory_hint(hint: _RevisionInventoryHint) -> None:
    global _INVENTORY_HINT

    paths = (
        *(snapshot.path for snapshot in hint.directory_snapshots),
        *(path for path, _identity_value in hint.entry_snapshots),
    )
    retained_path_bytes = sum(len(os.fsencode(path)) for path in paths)
    with _INVENTORY_HINT_LOCK:
        if (
            len(paths) > _MAX_INVENTORY_HINT_ENTRIES
            or retained_path_bytes > _MAX_INVENTORY_HINT_PATH_BYTES
        ):
            _INVENTORY_HINT = None
        else:
            _INVENTORY_HINT = hint


def _matching_inventory_hint(
    repository: RepositoryScope,
    expected: WorkspaceRevision,
    *,
    root: Path,
    resolved_root: Path,
) -> _RevisionInventoryHint | None:
    with _INVENTORY_HINT_LOCK:
        hint = _INVENTORY_HINT
    if hint is None or (
        hint.repository_id,
        hint.checkout_id,
        hint.revision_sha256,
        hint.root,
        hint.resolved_root,
    ) != (
        repository.repository_id,
        repository.checkout_id,
        expected.revision_sha256,
        root,
        resolved_root,
    ):
        return None
    return hint


def _restore_inventory_hint(
    hint: _RevisionInventoryHint,
    *,
    root: Path,
    directory_snapshots: dict[Path, _DirectorySnapshot],
    entry_snapshots: dict[Path, _StrongIdentity] | None,
    prepared_files: dict[str, _FileSnapshot] | None,
    private_inventory_safe: list[bool] | None,
    current_relevant: set[str],
    current_relevant_paths: dict[str, Path],
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> bool:
    fresh_entry_snapshots: list[tuple[Path, _StrongIdentity]] = []
    try:
        for snapshot in hint.directory_snapshots:
            _check_stop(deadline, cancelled)
            _validate_directory_snapshot(root, snapshot)
        if entry_snapshots is not None:
            for path, _identity_value in hint.entry_snapshots:
                _check_stop(deadline, cancelled)
                info = path.lstat()
                if _is_reparse(info) or stat.S_ISDIR(info.st_mode):
                    return False
                fresh_entry_snapshots.append((path, _strong_identity(info)))
    except _RevisionStopped:
        raise
    except OSError:
        return False
    directory_snapshots.update((snapshot.path, snapshot) for snapshot in hint.directory_snapshots)
    if entry_snapshots is not None:
        entry_snapshots.update(fresh_entry_snapshots)
    if prepared_files is not None:
        prepared_files.update(hint.prepared_files)
    if private_inventory_safe is not None:
        private_inventory_safe[0] = hint.private_inventory_safe
    current_relevant.update(path for path, _file_path in hint.relevant_files)
    current_relevant_paths.update(hint.relevant_files)
    return True


def _check_stop(
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> None:
    if deadline is not None and (
        isinstance(deadline, bool)
        or not isinstance(deadline, (int, float))
        or not math.isfinite(deadline)
    ):
        raise ValueError("deadline must be a finite monotonic timestamp or None")
    if cancelled is not None and not callable(cancelled):
        raise TypeError("cancelled must be callable or None")
    if cancelled is not None and cancelled():
        raise _RevisionStopped("workspace revision cancelled")
    if deadline is not None and time.monotonic() >= deadline:
        raise _RevisionStopped("workspace revision deadline reached")


def _checked_digest(
    hash_name: str,
    content: bytes | bytearray,
    *,
    length: int,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> bytes:
    digest = hashlib.new(hash_name)
    view = memoryview(content)
    try:
        for offset in range(0, length, _HASH_CHECK_CHUNK_BYTES):
            _check_stop(deadline, cancelled)
            digest.update(view[offset : min(offset + _HASH_CHECK_CHUNK_BYTES, length)])
            _check_stop(deadline, cancelled)
    finally:
        view.release()
    return digest.digest()


def _normalized_path(raw: str) -> str:
    normalized = unicodedata.normalize("NFC", raw)
    path = PurePosixPath(normalized)
    if (
        not normalized
        or "\\" in normalized
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("workspace revision path must be normalized relative POSIX text")
    return path.as_posix()


def _is_configuration(path: str) -> bool:
    return "/" not in path and (
        path in PYTHON_CONFIG_NAMES
        or (path.startswith("requirements") and path.endswith(".txt"))
    )


def _is_relevant_path(path: str) -> bool:
    return PurePosixPath(path).suffix in {".py", ".pyi"} or _is_configuration(path)


def _private_path_text_safe(path: str) -> bool:
    return "\\" not in path and not any(0xD800 <= ord(character) <= 0xDFFF for character in path)


def _identity(info: os.stat_result) -> _Identity:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        getattr(info, "st_file_attributes", 0),
    )


def _is_reparse(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & _REPARSE_POINT
    )


def _directory_snapshot(
    root: Path,
    path: Path,
    *,
    resolved_root: Path | None = None,
) -> _DirectorySnapshot:
    try:
        info = path.lstat()
        if _is_reparse(info) or not stat.S_ISDIR(info.st_mode):
            raise PermissionError("workspace revision directory is a symlink or reparse point")
        resolved_root = resolved_root or root.resolve(strict=True)
        resolved = path.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise PermissionError("workspace revision directory changed or escapes checkout") from exc
    return _DirectorySnapshot(path, resolved, _identity(info), info.st_ctime_ns)


def _validate_directory_snapshot(root: Path, snapshot: _DirectorySnapshot) -> None:
    try:
        info = snapshot.path.lstat()
    except OSError as exc:
        raise PermissionError("workspace revision directory identity changed") from exc
    if (
        _is_reparse(info)
        or not stat.S_ISDIR(info.st_mode)
        or _identity(info) != snapshot.identity
        or info.st_ctime_ns != snapshot.change_time_ns
    ):
        raise PermissionError("workspace revision directory identity changed")


def _file_snapshot(
    root: Path,
    path: Path,
    *,
    resolved_root: Path | None = None,
    directory_snapshots: dict[Path, _DirectorySnapshot] | None = None,
) -> _FileSnapshot:
    resolved_root = resolved_root or root.resolve(strict=True)
    directory_snapshots = directory_snapshots if directory_snapshots is not None else {}
    parents = []
    for parent in path.parents:
        parent_snapshot = directory_snapshots.get(parent)
        if parent_snapshot is None:
            parent_snapshot = _directory_snapshot(
                root,
                parent,
                resolved_root=resolved_root,
            )
            directory_snapshots[parent] = parent_snapshot
        parents.append(parent_snapshot)
        if parent == root:
            break
    else:
        raise PermissionError("workspace revision source is outside checkout")
    try:
        info = path.lstat()
        if _is_reparse(info) or not stat.S_ISREG(info.st_mode):
            raise PermissionError("workspace revision source must be a regular non-symlink file")
        resolved = path.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise PermissionError("workspace revision source escapes checkout or changed") from exc
    return _FileSnapshot(path, resolved, _identity(info), tuple(parents))


def _validate_file_identity(snapshot: _FileSnapshot) -> os.stat_result:
    try:
        info = snapshot.path.lstat()
    except OSError as exc:
        raise PermissionError("workspace revision file snapshot changed") from exc
    if (
        _is_reparse(info)
        or not stat.S_ISREG(info.st_mode)
        or _identity(info) != snapshot.identity
    ):
        raise PermissionError("workspace revision file snapshot changed")
    return info


def _validate_file_snapshot(root: Path, snapshot: _FileSnapshot) -> None:
    for parent in snapshot.parents:
        _validate_directory_snapshot(root, parent)
    _validate_file_identity(snapshot)


def _terminate_process_tree(
    process: subprocess.Popen[bytes], *, platform_name: str | None = None
) -> None:
    platform_name = platform_name or os.name
    if platform_name == "nt":
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        taskkill = str(PureWindowsPath(system_root) / "System32" / "taskkill.exe")
        try:
            terminator = subprocess.Popen(
                [taskkill, "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
            )
            try:
                terminator.communicate(timeout=_PROCESS_CLEANUP_SECONDS)
            except subprocess.TimeoutExpired:
                terminator.kill()
                terminator.wait(timeout=_PROCESS_CLEANUP_SECONDS)
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except OSError:
            pass
        try:
            os.killpg(process.pid, getattr(signal, "SIGKILL", 9))
        except OSError:
            pass
    try:
        process.wait(timeout=_PROCESS_CLEANUP_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
            process.wait(timeout=_PROCESS_CLEANUP_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _git_command(root: Path, arguments: list[str], executable: str) -> list[str]:
    """The full argv for a git invocation that touches no optional locks."""
    return [
        executable,
        "--no-optional-locks",
        "-c",
        "core.fsmonitor=false",
        "-C",
        root.as_posix(),
        *arguments,
    ]


def _git_popen_options(
    environment: dict[str, str] | None, pass_fds: tuple[int, ...]
) -> dict[str, object]:
    """The Popen options for a bounded, session-isolated git invocation."""
    options: dict[str, object] = dict(
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        shell=False,
        close_fds=True,
        env=environment or sanitized_git_environment(),
    )
    if os.name == "nt":
        options["creationflags"] = _WINDOWS_NEW_PROCESS_GROUP
        return options
    options["start_new_session"] = True
    if pass_fds:
        options["pass_fds"] = pass_fds
    return options


class _GitRun:
    """One bounded git invocation, the thread draining it, and why it stopped."""

    def __init__(
        self,
        process: subprocess.Popen,
        maximum_bytes: int,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> None:
        self.process = process
        self.maximum_bytes = maximum_bytes
        self.deadline = deadline
        self.cancelled = cancelled
        self.stop_reason: list[str] = []
        self._termination_started = threading.Event()
        self._termination_lock = threading.Lock()
        self._read_done = threading.Event()
        self._output: list[bytes] = []
        self._errors: list[BaseException] = []
        self._reader = threading.Thread(target=self._read_output, daemon=True)
        self._reader.start()

    @property
    def terminating(self) -> bool:
        """Whether something has already started killing this process tree."""
        return self._termination_started.is_set()

    def terminate(self) -> None:
        """Kill the process tree, once, however many callers ask for it."""
        with self._termination_lock:
            if self._termination_started.is_set():
                return
            self._termination_started.set()
        _terminate_process_tree(self.process)

    def _read_output(self) -> None:
        try:
            assert self.process.stdout is not None
            self._output.append(self.process.stdout.read(self.maximum_bytes + 1))
        except BaseException as exc:  # noqa: BLE001 - re-raised on the owning thread
            self._errors.append(exc)
        finally:
            self._read_done.set()

    def _remaining(self) -> float:
        """Seconds left before this run's deadline."""
        return self.deadline - time.monotonic()

    def _stop_because(self, remaining: float) -> bool:
        """Whether to stop waiting now, recording why and killing the tree."""
        if self.cancelled is not None and self.cancelled():
            self.stop_reason.append("cancelled")
            self.terminate()
            return True
        if remaining <= 0:
            self.stop_reason.append("deadline")
            self.terminate()
            return True
        return False

    def wait_for_output(self) -> None:
        """Let the reader drain the pipe, or stop it on cancellation or timeout."""
        while not self._read_done.is_set():
            remaining = self._remaining()
            if self._stop_because(remaining):
                return
            self._read_done.wait(min(0.01, remaining))

    def _close_stdout(self) -> None:
        """Close the pipe, whether or not it is still usable."""
        if self.process.stdout is None:
            return
        try:
            self.process.stdout.close()
        except OSError:
            pass

    def output(self) -> bytes:
        """The bytes read, once the reader thread has been joined."""
        if self.stop_reason and self.process.stdout is not None:
            self.process.stdout.close()
        self._reader.join(timeout=_PROCESS_CLEANUP_SECONDS)
        if self._errors and not self.stop_reason:
            raise self._errors[0]
        return self._output[0] if self._output else b""

    def reap_holding_fds(self) -> None:
        """Wait for the child to exit without reaping it, so its fds stay held."""
        wait_options = os.WEXITED | os.WNOHANG | os.WNOWAIT
        while True:
            remaining = self._remaining()
            if self._stop_because(remaining):
                return
            waited = os.waitid(os.P_PID, self.process.pid, wait_options)
            if waited is not None and waited.si_pid == self.process.pid:
                self.terminate()
                return
            time.sleep(min(0.001, remaining))

    def reap(self) -> None:
        """Let the child finish; kill it if it will not."""
        try:
            self.process.wait(timeout=_PROCESS_CLEANUP_SECONDS)
        except subprocess.TimeoutExpired:
            self.terminate()

    def release(self, *, holds_fds: bool) -> None:
        """Release the process and its pipe, whatever happened."""
        if holds_fds and not self.terminating:
            self.terminate()
        elif self.process.poll() is None:
            self.terminate()
        self._close_stdout()
        self._reader.join(timeout=_PROCESS_CLEANUP_SECONDS)


def _reap_git_run(run: _GitRun, *, holds_fds: bool) -> None:
    """Wait for the child to finish, in whichever way this run requires."""
    if run.terminating:
        return
    if holds_fds:
        run.reap_holding_fds()
        return
    run.reap()


def _git_stop_error(reason: str, label: str, deadline: float | None) -> Exception:
    """The error for a run that was stopped rather than finished."""
    message = f"workspace revision {reason} during {label}"
    if reason == "cancelled":
        return _RevisionStopped(message)
    if deadline is not None and time.monotonic() >= deadline:
        return _RevisionStopped(message)
    return TimeoutError(message)


def _git_run_outcome(
    run: _GitRun,
    command: list[str],
    output: bytes,
    *,
    maximum_bytes: int,
    label: str,
    deadline: float | None,
) -> bytes:
    """The output this run produced, or the failure it has to report."""
    if run.stop_reason:
        raise _git_stop_error(run.stop_reason[0], label, deadline)
    if len(output) > maximum_bytes:
        raise ValueError(f"{label} output exceeds the byte ceiling")
    if run.process.returncode != 0:
        raise subprocess.CalledProcessError(run.process.returncode, command)
    return output


def _git_output(
    root: Path,
    arguments: list[str],
    *,
    maximum_bytes: int,
    label: str,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
    environment: dict[str, str] | None = None,
    pass_fds: tuple[int, ...] = (),
    executable: str = "git",
) -> bytes:
    """Run one bounded git command and answer its output, or refuse."""
    _check_stop(deadline, cancelled)
    command = _git_command(root, arguments, executable)
    process = subprocess.Popen(command, **_git_popen_options(environment, pass_fds))
    holds_fds = bool(pass_fds) and os.name != "nt"
    local_deadline = time.monotonic() + GIT_STATUS_TIMEOUT_SECONDS
    run = _GitRun(
        process,
        maximum_bytes,
        local_deadline if deadline is None else min(local_deadline, deadline),
        cancelled,
    )
    try:
        run.wait_for_output()
        output = run.output()
        if len(output) > maximum_bytes:
            run.terminate()
        _reap_git_run(run, holds_fds=holds_fds)
    finally:
        run.release(holds_fds=holds_fds)
    return _git_run_outcome(
        run, command, output, maximum_bytes=maximum_bytes, label=label, deadline=deadline
    )

def _git_status(
    root: Path,
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> bytes:
    return _git_output(
        root,
        [
            "status",
            "--porcelain=v2",
            "-z",
            "--untracked-files=all",
            "--ignore-submodules=all",
        ],
        maximum_bytes=MAX_GIT_STATUS_BYTES,
        label="Git status",
        deadline=deadline,
        cancelled=cancelled,
    )


def _parse_git_state_output(
    output: bytes,
    *,
    allow_missing_head: bool,
) -> tuple[str | None, bytes]:
    prefix = b"# branch.oid "
    identities = [record[len(prefix) :] for record in output.split(b"\0") if record.startswith(prefix)]
    if len(identities) != 1:
        raise ValueError("Git state returned an invalid HEAD identity")
    identity = identities[0]
    if identity == b"(initial)":
        if allow_missing_head:
            return None, output
        raise ValueError("Git state returned a missing HEAD identity")
    if _GIT_COMMIT_RE.fullmatch(identity) is None:
        raise ValueError("Git state returned an invalid HEAD identity")
    return identity.decode("ascii"), output


def _git_state(
    root: Path,
    *,
    allow_missing_head: bool,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> tuple[str | None, bytes]:
    output = _git_output(
        root,
        [
            "status",
            "--porcelain=v2",
            "--branch",
            "-z",
            "--untracked-files=all",
            "--ignore-submodules=all",
        ],
        maximum_bytes=MAX_GIT_STATUS_BYTES,
        label="Git state",
        deadline=deadline,
        cancelled=cancelled,
    )
    return _parse_git_state_output(output, allow_missing_head=allow_missing_head)


def _git_state_with_private_index(
    root: Path,
    descriptor: int,
    *,
    git_executable: Path,
    allow_missing_head: bool,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> tuple[str | None, bytes]:
    environment = sanitized_git_environment()
    environment["GIT_INDEX_FILE"] = f"/proc/self/fd/{descriptor}"
    environment["GIT_ATTR_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_SYSTEM"] = os.devnull
    output = _git_output(
        root,
        [
            "status",
            "--porcelain=v2",
            "--branch",
            "-z",
            "--untracked-files=all",
            "--ignore-submodules=all",
        ],
        maximum_bytes=MAX_GIT_STATUS_BYTES,
        label="Git state",
        deadline=deadline,
        cancelled=cancelled,
        environment=environment,
        pass_fds=(descriptor,),
        executable=os.fspath(git_executable),
    )
    return _parse_git_state_output(output, allow_missing_head=allow_missing_head)


def _git_head(
    root: Path,
    *,
    allow_missing: bool,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> str | None:
    try:
        output = _git_output(
            root,
            ["rev-parse", "--verify", "HEAD^{commit}"],
            maximum_bytes=_MAX_GIT_HEAD_BYTES,
            label="Git HEAD",
            deadline=deadline,
            cancelled=cancelled,
        )
    except subprocess.CalledProcessError:
        if allow_missing:
            return None
        raise
    value = output.strip()
    if _GIT_COMMIT_RE.fullmatch(value) is None:
        raise ValueError("Git HEAD returned an invalid commit identity")
    return value.decode("ascii")


def _status_paths(output: bytes) -> list[tuple[str, str]]:
    records = output.split(b"\0")
    if records and records[-1] == b"":
        records.pop()
    result: list[tuple[str, str]] = []
    index = 0
    while index < len(records):
        record = records[index]
        marker = record[:1]
        try:
            if marker == b"1":
                fields = record.split(b" ", 8)
                change = fields[1]
                if len(change) != 2:
                    raise ValueError("malformed Git status change code")
                raw_path = fields[8]
                kind = "deleted" if b"D" in change else "modified"
                result.append((raw_path.decode("utf-8", errors="strict"), kind))
            elif marker == b"2":
                fields = record.split(b" ", 9)
                change = fields[1]
                score = fields[8]
                if len(change) != 2:
                    raise ValueError("malformed Git status change code")
                raw_path = fields[9]
                index += 1
                original = records[index]
                if score.startswith(b"R"):
                    result.append((original.decode("utf-8", errors="strict"), "deleted"))
                kind = "deleted" if b"D" in change else "modified"
                result.append((raw_path.decode("utf-8", errors="strict"), kind))
            elif marker == b"u":
                raw_path = record.split(b" ", 10)[10]
                result.append((raw_path.decode("utf-8", errors="strict"), "modified"))
            elif marker == b"?":
                result.append((record[2:].decode("utf-8", errors="strict"), "untracked"))
            elif marker == b"#":
                pass
            elif marker != b"!":
                raise ValueError("unknown Git status record")
        except (IndexError, UnicodeError) as exc:
            raise ValueError("malformed Git status output") from exc
        index += 1
    return result


def _git_state_matches_revision(
    current_head: str | None,
    git_state: bytes,
    expected: WorkspaceRevision,
    entries: dict[str, RevisionEntry],
) -> bool:
    if current_head != expected.git_head:
        return False
    current_status: dict[str, str] = {}
    normalized_inputs: dict[str, str] = {}
    for raw, kind in _status_paths(git_state):
        normalized = _normalized_path(raw)
        previous_raw = normalized_inputs.get(normalized)
        if previous_raw is not None and previous_raw != raw:
            return False
        normalized_inputs[normalized] = raw
        current_status[normalized] = kind
    if any(path not in entries for path in current_status):
        return False
    for path, entry in entries.items():
        status = current_status.get(path)
        if entry.kind in {"modified", "untracked", "deleted"}:
            if status != entry.kind:
                return False
        elif entry.kind == "source" and status is not None:
            return False
        elif entry.kind == "configuration" and status == "deleted":
            return False
    return True


# The files whose contents change how git reads a tree.
_SEMANTICS_NAMES = frozenset({".gitattributes", ".gitignore"})


@dataclass
class _RelevantScan:
    """The bookkeeping one relevant-files walk carries across directories."""

    root: Path
    resolved_root: Path
    directory_snapshots: dict[Path, _DirectorySnapshot]
    entry_snapshots: dict[Path, _StrongIdentity] | None
    prepared_files: dict[str, _FileSnapshot] | None
    prepared_paths: set[str] | None
    private_inventory_safe: list[bool] | None
    relevant_paths: set[str] | None
    deadline: float | None
    cancelled: Callable[[], bool] | None
    examined: int = 0
    relevant_examined: int = 0


@dataclass(frozen=True, slots=True)
class _ScannedEntry:
    """One directory entry, with what the walk decided about it."""

    entry: os.DirEntry
    path: Path
    relative: str
    info: os.stat_result
    unsafe: bool
    relevant: bool


def _directory_snapshot_for(scan: _RelevantScan, current: Path) -> _DirectorySnapshot:
    """The snapshot for this directory, taken once and then remembered."""
    snapshot = scan.directory_snapshots.get(current)
    if snapshot is None:
        snapshot = _directory_snapshot(
            scan.root, current, resolved_root=scan.resolved_root
        )
        scan.directory_snapshots[current] = snapshot
    snapshot.resolved.relative_to(scan.resolved_root)
    return snapshot


def _refuse_unsafe_entry(
    entry: os.DirEntry, info: os.stat_result, relevant_name: bool
) -> None:
    """A link or reparse point where it matters stops the walk outright."""
    try:
        linked_directory = entry.is_dir(follow_symlinks=True)
    except OSError:
        linked_directory = False
    if relevant_name or linked_directory or stat.S_ISDIR(info.st_mode):
        raise PermissionError(
            "workspace revision relevant path is a symlink or reparse directory"
        )


def _count_scanned_entry(scan: _RelevantScan, *, relevant: bool) -> None:
    """Count this entry, and refuse a tree bigger than either ceiling."""
    scan.examined += 1
    if relevant:
        scan.relevant_examined += 1
    if scan.relevant_examined > MAX_REVISION_FILES:
        raise ValueError("workspace revision exceeds the file-count ceiling")
    if scan.examined > MAX_REVISION_FILES:
        raise ValueError("workspace revision exceeds the examined-entry ceiling")


def _scanned_entry(scan: _RelevantScan, entry: os.DirEntry) -> _ScannedEntry:
    """Classify one directory entry, counting it against both ceilings."""
    info = entry.stat(follow_symlinks=False)
    path = Path(entry.path)
    relative = path.relative_to(scan.root).as_posix()
    unsafe = entry.is_symlink() or _is_reparse(info)
    relevant_name = _is_relevant_path(relative)
    if unsafe:
        _refuse_unsafe_entry(entry, info, relevant_name)
    relevant = not unsafe and stat.S_ISREG(info.st_mode) and relevant_name
    _count_scanned_entry(scan, relevant=relevant)
    return _ScannedEntry(entry, path, relative, info, unsafe, relevant)


def _scanned_directory_entries(
    scan: _RelevantScan, current: Path
) -> list[_ScannedEntry]:
    """Every entry of this directory, classified and counted."""
    items: list[_ScannedEntry] = []
    with os.scandir(current) as iterator:
        for entry in iterator:
            _check_stop(scan.deadline, scan.cancelled)
            items.append(_scanned_entry(scan, entry))
    return items


def _prepared_parent_snapshots(
    scan: _RelevantScan, current: Path
) -> tuple[_DirectorySnapshot, ...]:
    """Every snapshot from this directory up to the root, innermost first."""
    parents: list[_DirectorySnapshot] = []
    parent = current
    while True:
        snapshot = scan.directory_snapshots.get(parent)
        if snapshot is None:
            raise PermissionError("workspace revision parent snapshot is unavailable")
        parents.append(snapshot)
        if parent == scan.root:
            return tuple(parents)
        parent = parent.parent


def _should_prepare(scan: _RelevantScan, item: _ScannedEntry) -> bool:
    """Whether the private index has any reason to stage this file."""
    if item.relevant or PurePosixPath(item.relative).name in _SEMANTICS_NAMES:
        return True
    return scan.prepared_paths is not None and item.relative in scan.prepared_paths


def _prepared_file_wanted(scan: _RelevantScan, item: _ScannedEntry) -> bool:
    """Whether this file is staged, the private index's text rules included."""
    if not _should_prepare(scan, item):
        return False
    return item.relevant or _private_path_text_safe(item.relative)


def _stage_prepared_file(
    scan: _RelevantScan,
    item: _ScannedEntry,
    current_snapshot: _DirectorySnapshot,
    prepared_parents: tuple[_DirectorySnapshot, ...],
) -> None:
    """Record this file under its normalized path, refusing a collision."""
    assert scan.prepared_files is not None
    normalized = _normalized_path(item.relative)
    if normalized in scan.prepared_files:
        raise ValueError(
            "workspace revision contains a Unicode normalization collision"
        )
    scan.prepared_files[normalized] = _FileSnapshot(
        item.path,
        current_snapshot.resolved / item.entry.name,
        _identity(item.info),
        prepared_parents,
    )


def _mark_inventory_unsafe(scan: _RelevantScan, item: _ScannedEntry) -> None:
    """A file the private index cannot name makes its inventory untrustworthy."""
    if scan.private_inventory_safe is None:
        return
    if _private_path_text_safe(item.relative):
        return
    scan.private_inventory_safe[0] = False


def _prepare_file(
    scan: _RelevantScan,
    item: _ScannedEntry,
    current_snapshot: _DirectorySnapshot,
    prepared_parents: tuple[_DirectorySnapshot, ...] | None,
) -> None:
    """Stage one file for the private index, or mark its inventory unsafe."""
    if scan.prepared_files is None or not stat.S_ISREG(item.info.st_mode):
        return
    if prepared_parents is None:
        raise AssertionError("workspace revision parent snapshots are unavailable")
    if _prepared_file_wanted(scan, item):
        _stage_prepared_file(scan, item, current_snapshot, prepared_parents)
        return
    _mark_inventory_unsafe(scan, item)


def _collect_subdirectory(item: _ScannedEntry, directories: list[Path]) -> None:
    """A subdirectory joins the walk unless it is the git marker itself."""
    if item.entry.name != ".git":
        directories.append(item.path)


def _record_entry_identity(scan: _RelevantScan, item: _ScannedEntry) -> None:
    """Remember what this entry looked like, if the pass tracks identities."""
    if scan.entry_snapshots is not None:
        scan.entry_snapshots[item.path] = _strong_identity(item.info)


def _record_relevant_path(scan: _RelevantScan, item: _ScannedEntry) -> None:
    """Remember a relevant file's normalized path, if the pass wants it."""
    if scan.relevant_paths is not None:
        scan.relevant_paths.add(_normalized_path(item.relative))


def _visit_entry(
    scan: _RelevantScan,
    item: _ScannedEntry,
    current_snapshot: _DirectorySnapshot,
    prepared_parents: tuple[_DirectorySnapshot, ...] | None,
    directories: list[Path],
) -> Iterator[Path]:
    """Handle one classified entry, yielding it when it is a relevant file."""
    if item.unsafe:
        return
    if stat.S_ISDIR(item.info.st_mode):
        _collect_subdirectory(item, directories)
        return
    _record_entry_identity(scan, item)
    _prepare_file(scan, item, current_snapshot, prepared_parents)
    if item.relevant:
        _record_relevant_path(scan, item)
        yield item.path


def _entry_sort_key(item: _ScannedEntry) -> str:
    """Directory entries are walked in normalized-name order."""
    return unicodedata.normalize("NFC", item.entry.name)


def _scan_directory(
    scan: _RelevantScan, current: Path, directories: list[Path]
) -> Iterator[Path]:
    """Yield the relevant files in this directory; collect its subdirectories."""
    current_snapshot = _directory_snapshot_for(scan, current)
    items = _scanned_directory_entries(scan, current)
    _validate_directory_snapshot(scan.root, current_snapshot)
    prepared_parents = (
        _prepared_parent_snapshots(scan, current)
        if scan.prepared_files is not None
        else None
    )
    for item in sorted(items, key=_entry_sort_key):
        _check_stop(scan.deadline, scan.cancelled)
        yield from _visit_entry(
            scan, item, current_snapshot, prepared_parents, directories
        )


def _relevant_files(
    root: Path,
    *,
    resolved_root: Path,
    directory_snapshots: dict[Path, _DirectorySnapshot],
    entry_snapshots: dict[Path, _StrongIdentity] | None = None,
    prepared_files: dict[str, _FileSnapshot] | None = None,
    prepared_paths: set[str] | None = None,
    private_inventory_safe: list[bool] | None = None,
    relevant_paths: set[str] | None = None,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> Iterator[Path]:
    """Every relevant file under the root, in a deterministic walk order."""
    scan = _RelevantScan(
        root=root,
        resolved_root=resolved_root,
        directory_snapshots=directory_snapshots,
        entry_snapshots=entry_snapshots,
        prepared_files=prepared_files,
        prepared_paths=prepared_paths,
        private_inventory_safe=private_inventory_safe,
        relevant_paths=relevant_paths,
        deadline=deadline,
        cancelled=cancelled,
    )
    stack = [root]
    while stack:
        _check_stop(deadline, cancelled)
        directories: list[Path] = []
        yield from _scan_directory(scan, stack.pop(), directories)
        stack.extend(reversed(directories))

def _hash_file_for_verification(
    path: Path,
    *,
    root: Path,
    resolved_root: Path,
    directory_snapshots: dict[Path, _DirectorySnapshot],
    remaining_bytes: int,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
    git_hash_name: str | None,
    snapshot: _FileSnapshot | None = None,
    validate_parents: bool = True,
) -> _VerificationHash:
    if snapshot is None:
        snapshot = _file_snapshot(
            root,
            path,
            resolved_root=resolved_root,
            directory_snapshots=directory_snapshots,
        )
    if snapshot.identity[3] > remaining_bytes:
        raise ValueError("workspace revision exceeds the byte ceiling")
    digest = hashlib.sha256()
    git_digest = None if git_hash_name is None else hashlib.new(git_hash_name)
    if git_digest is not None:
        git_digest.update(f"blob {snapshot.identity[3]}\0".encode("ascii"))
    size = 0
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise PermissionError("workspace revision source changed or became a symlink at open") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _identity(opened) != snapshot.identity:
            raise PermissionError("workspace revision source changed before open")
        expected_size = snapshot.identity[3]
        while size < expected_size:
            _check_stop(deadline, cancelled)
            chunk = os.read(descriptor, min(1024 * 1024, expected_size - size))
            if not chunk:
                raise PermissionError("workspace revision source changed during read")
            size += len(chunk)
            digest.update(chunk)
            if git_digest is not None:
                git_digest.update(chunk)
        _check_stop(deadline, cancelled)
        after = os.fstat(descriptor)
        if _identity(after) != snapshot.identity:
            raise PermissionError("workspace revision source changed during read")
        if validate_parents:
            _validate_file_snapshot(root, snapshot)
        else:
            _validate_file_identity(snapshot)
    finally:
        os.close(descriptor)
    return _VerificationHash(
        digest.hexdigest(),
        size,
        snapshot,
        None if git_digest is None else git_digest.digest(),
        after,
        after.st_ctime_ns,
    )


def _hash_file(
    path: Path,
    *,
    root: Path,
    resolved_root: Path,
    directory_snapshots: dict[Path, _DirectorySnapshot],
    remaining_bytes: int,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> tuple[str, int, _FileSnapshot]:
    result = _hash_file_for_verification(
        path,
        root=root,
        resolved_root=resolved_root,
        directory_snapshots=directory_snapshots,
        remaining_bytes=remaining_bytes,
        deadline=deadline,
        cancelled=cancelled,
        git_hash_name=None,
    )
    return result.sha256, result.size, result.snapshot


def _private_index_platform_supported() -> bool:
    return (
        os.name == "posix"
        and sys.platform.startswith("linux")
        and hasattr(os, "memfd_create")
        and hasattr(os, "MFD_CLOEXEC")
        and hasattr(os, "MFD_ALLOW_SEALING")
        and hasattr(os, "waitid")
        and all(hasattr(os, name) for name in ("P_PID", "WEXITED", "WNOHANG", "WNOWAIT"))
        and _fcntl is not None
        and all(
            hasattr(_fcntl, name)
            for name in (
                "F_ADD_SEALS",
                "F_GET_SEALS",
                "F_SEAL_GROW",
                "F_SEAL_SEAL",
                "F_SEAL_SHRINK",
                "F_SEAL_WRITE",
            )
        )
        and Path("/proc/self/fd").is_dir()
    )


def _private_git_installation() -> _PrivateGitInstallation | None:
    candidate = shutil.which("git")
    if candidate is None:
        return None
    try:
        executable = Path(candidate).resolve(strict=True)
        info = executable.lstat()
    except OSError:
        return None
    if _is_reparse(info) or not stat.S_ISREG(info.st_mode):
        return None
    if executable == Path("/usr/bin/git"):
        config_paths = (Path("/etc/gitconfig"), Path("/usr/etc/gitconfig"))
        attribute_paths = (
            Path("/etc/gitattributes"),
            Path("/usr/etc/gitattributes"),
        )
    elif executable == Path("/usr/local/bin/git"):
        config_paths = (
            Path("/etc/gitconfig"),
            Path("/usr/local/etc/gitconfig"),
        )
        attribute_paths = (
            Path("/etc/gitattributes"),
            Path("/usr/local/etc/gitattributes"),
        )
    else:
        return None
    if _SYSTEM_GIT_CONFIG_PATHS is not None:
        config_paths = _SYSTEM_GIT_CONFIG_PATHS
    if _SYSTEM_GIT_ATTRIBUTE_PATHS is not None:
        attribute_paths = _SYSTEM_GIT_ATTRIBUTE_PATHS
    return _PrivateGitInstallation(
        executable,
        _strong_identity(info),
        config_paths,
        attribute_paths,
    )


def _ordinary_index_path(root: Path) -> Path | None:
    marker = root / ".git"
    try:
        marker_info = marker.lstat()
    except OSError:
        return None
    if _is_reparse(marker_info) or not stat.S_ISDIR(marker_info.st_mode):
        return None
    index = marker / "index"
    try:
        index_info = index.lstat()
    except OSError:
        return None
    if _is_reparse(index_info) or not stat.S_ISREG(index_info.st_mode):
        return None
    return index


def _strong_identity(info: os.stat_result) -> _StrongIdentity:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        getattr(info, "st_file_attributes", 0),
    )


def _open_owned_file_parent(root: Path, path: Path) -> tuple[int, str]:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise PermissionError("workspace revision metadata escaped its repository") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise PermissionError("workspace revision metadata path is invalid")
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(root, directory_flags)
    try:
        for component in relative.parts[:-1]:
            child = os.open(component, directory_flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, relative.parts[-1]


def _read_owned_file(
    root: Path,
    path: Path,
    maximum_bytes: int,
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> _OwnedFileRead | None:
    _check_stop(deadline, cancelled)
    try:
        parent_descriptor, name = _open_owned_file_parent(root, path)
    except OSError:
        return None
    try:
        try:
            named = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        except OSError:
            return None
        if (
            _is_reparse(named)
            or not stat.S_ISREG(named.st_mode)
            or named.st_size > maximum_bytes
        ):
            return None
        flags = (
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            descriptor = os.open(name, flags, dir_fd=parent_descriptor)
        except OSError as exc:
            raise PermissionError("workspace revision owned file changed before open") from exc
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or _strong_identity(opened) != _strong_identity(named)
            ):
                raise PermissionError("workspace revision owned file changed before read")
            content = bytearray(opened.st_size)
            size = 0
            while size < opened.st_size:
                _check_stop(deadline, cancelled)
                chunk = os.read(descriptor, min(1024 * 1024, opened.st_size - size))
                if not chunk:
                    raise PermissionError("workspace revision owned file changed during read")
                content[size : size + len(chunk)] = chunk
                size += len(chunk)
            _check_stop(deadline, cancelled)
            after = os.fstat(descriptor)
            if _strong_identity(after) != _strong_identity(opened):
                raise PermissionError("workspace revision owned file changed during read")
        finally:
            os.close(descriptor)
        try:
            final_named = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        except OSError as exc:
            raise PermissionError("workspace revision owned file changed after read") from exc
        if _strong_identity(final_named) != _strong_identity(opened):
            raise PermissionError("workspace revision owned file identity changed after read")
    finally:
        os.close(parent_descriptor)
    digest = _checked_digest(
        "sha256",
        content,
        length=len(content),
        deadline=deadline,
        cancelled=cancelled,
    ).hex()
    return _OwnedFileRead(
        content,
        _OwnedFileFence(path, digest, _strong_identity(opened)),
    )


def _owned_path_exists_or_is_uncertain(root: Path, path: Path) -> bool:
    try:
        parent_descriptor, name = _open_owned_file_parent(root, path)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    try:
        try:
            os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return False
        except OSError:
            return True
        return True
    finally:
        os.close(parent_descriptor)


def _global_git_config_paths() -> tuple[Path, ...] | None:
    home_value = os.environ.get("HOME")
    xdg_value = os.environ.get("XDG_CONFIG_HOME")
    paths: list[Path] = []
    if home_value:
        home = Path(home_value)
        if not home.is_absolute():
            return None
        paths.append(home / ".gitconfig")
    if xdg_value:
        xdg = Path(xdg_value)
        if not xdg.is_absolute():
            return None
        paths.append(xdg / "git/config")
    elif home_value:
        paths.append(Path(home_value) / ".config/git/config")
    return tuple(paths)


def _global_git_attributes_path() -> Path | None:
    xdg_value = os.environ.get("XDG_CONFIG_HOME")
    home_value = os.environ.get("HOME")
    if xdg_value:
        root = Path(xdg_value)
    elif home_value:
        root = Path(home_value) / ".config"
    else:
        return None
    if not root.is_absolute():
        return None
    return root / "git/attributes"


def _global_git_ignore_path() -> Path | None:
    xdg_value = os.environ.get("XDG_CONFIG_HOME")
    home_value = os.environ.get("HOME")
    if xdg_value:
        root = Path(xdg_value)
    elif home_value:
        root = Path(home_value) / ".config"
    else:
        return None
    if not root.is_absolute():
        return None
    return root / "git/ignore"


def _safe_private_git_config(content: bytes, *, hash_name: str) -> bool:
    try:
        text = content.decode("utf-8", errors="strict")
    except UnicodeError:
        return False
    section = None
    seen: set[str] = set()
    allowed = {
        "core": {
            "repositoryformatversion",
            "filemode",
            "bare",
            "logallrefupdates",
            "trustctime",
        },
        "extensions": {"objectformat"},
        "user": {"email", "name"},
    }
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = re.fullmatch(r"\[(core|extensions|user)\]", line, flags=re.IGNORECASE)
        if match is not None:
            section = match.group(1).lower()
            continue
        if section is None or line.startswith(("#", ";")) or "=" not in line:
            return False
        key, value = (part.strip() for part in line.split("=", 1))
        key = key.lower()
        if key not in allowed[section] or not key or value.endswith("\\"):
            return False
        qualified = f"{section}.{key}"
        seen.add(qualified)
        if qualified == "core.repositoryformatversion" and value not in {"0", "1"}:
            return False
        if qualified in {
            "core.bare",
            "core.filemode",
            "core.logallrefupdates",
            "core.trustctime",
        } and value.lower() not in {"false", "true"}:
            return False
        if qualified == "core.bare" and value.lower() != "false":
            return False
        if qualified == "extensions.objectformat" and value.lower() != hash_name:
            return False
    required = {
        "core.bare",
        "core.filemode",
        "core.repositoryformatversion",
    }
    return required <= seen


def _safe_ignored_git_config(content: bytes) -> bool:
    try:
        text = content.decode("utf-8", errors="strict")
    except UnicodeError:
        return False
    in_user_section = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        section = re.fullmatch(r"\[user\]", line, flags=re.IGNORECASE)
        if section is not None:
            in_user_section = True
            continue
        if line.startswith("[") or not in_user_section or "=" not in line:
            return False
        key, value = (part.strip() for part in line.split("=", 1))
        if key.lower() not in {"email", "name"} or value.endswith("\\"):
            return False
    return True


def _inert_git_attributes(content: bytes) -> bool:
    return all(not line.strip() or line.startswith(b"#") for line in content.splitlines())


def _owned_attributes_are_inert(
    root: Path,
    path: Path,
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> bool:
    try:
        attributes = _read_owned_file(
            root,
            path,
            _MAX_PRIVATE_ATTRIBUTES_BYTES,
            deadline=deadline,
            cancelled=cancelled,
        )
    except _RevisionStopped:
        raise
    except OSError:
        return False
    return attributes is not None and _inert_git_attributes(attributes.content)


def _raw_semantics_environment() -> tuple[tuple[str, str], ...]:
    return tuple(
        sorted(
            (name, value)
            for name, value in os.environ.items()
            if name in _PRIVATE_GIT_SELECTOR_ENVIRONMENT
            or name in {"HOME", "XDG_CONFIG_HOME"}
            or name.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))
        )
    )


@dataclass
class _SemanticsScan:
    """The fences and known-absent paths one raw-semantics proof gathers."""

    files: list[_SemanticsFileFence] = field(default_factory=list)
    absent: list[tuple[Path, Path]] = field(default_factory=list)


def _content_unchecked(content: bytes) -> bool:
    """No content rule applies to this file; only its fence matters."""
    return True


def _private_git_environment_overridden() -> bool:
    """Whether the environment can steer git's configuration out from under us."""
    if any(name in os.environ for name in _PRIVATE_GIT_SELECTOR_ENVIRONMENT):
        return True
    return any(
        name.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")) for name in os.environ
    )


def _global_semantics_locations() -> tuple[tuple[Path, ...], Path, Path] | None:
    """The global config, attributes and ignore locations, when all are known."""
    configs = _global_git_config_paths()
    attributes = _global_git_attributes_path()
    ignore = _global_git_ignore_path()
    if configs is None or attributes is None or ignore is None:
        return None
    return configs, attributes, ignore


def _external_semantics_paths(
    paths: Iterable[Path],
) -> tuple[tuple[Path, Path], ...] | None:
    """Each path paired with its filesystem root, or None if one is relative."""
    pairs: list[tuple[Path, Path]] = []
    for path in paths:
        if not path.is_absolute():
            return None
        pairs.append((Path(path.anchor), path))
    return tuple(pairs)


def _deduplicated_ignore_paths(
    pairs: Sequence[tuple[Path, Path]],
) -> tuple[tuple[Path, Path], ...] | None:
    """The ignore files to fence, once each, or None if one is not absolute."""
    seen: set[Path] = set()
    unique: list[tuple[Path, Path]] = []
    for owner_root, path in pairs:
        if not path.is_absolute():
            return None
        if path in seen:
            continue
        seen.add(path)
        unique.append((owner_root, path))
    return tuple(unique)


def _add_semantics_file(
    scan: _SemanticsScan,
    owner_root: Path,
    path: Path,
    limit: int,
    inert: Callable[[bytes], bool],
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> bool:
    """Fence one semantics file; False when it cannot be trusted or read."""
    if not _owned_path_exists_or_is_uncertain(owner_root, path):
        scan.absent.append((owner_root, path))
        return True
    try:
        read = _read_owned_file(
            owner_root, path, limit, deadline=deadline, cancelled=cancelled
        )
    except _RevisionStopped:
        raise
    except OSError:
        return False
    if read is None or not inert(read.content):
        return False
    scan.files.append(_SemanticsFileFence(owner_root, read.fence, limit))
    return True


def _fence_semantics_files(
    scan: _SemanticsScan,
    pairs: Iterable[tuple[Path, Path]],
    limit: int,
    inert: Callable[[bytes], bool],
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> bool:
    """Fence every one of these files; False as soon as one is untrustworthy."""
    return all(
        _add_semantics_file(
            scan, owner_root, path, limit, inert, deadline=deadline, cancelled=cancelled
        )
        for owner_root, path in pairs
    )


def _semantics_groups(
    root: Path,
    marker: Path,
    installation: _PrivateGitInstallation,
    worktree_ignore_paths: tuple[Path, ...],
    locations: tuple[tuple[Path, ...], Path, Path],
) -> tuple[tuple[tuple[tuple[Path, Path], ...], int, Callable[[bytes], bool]], ...] | None:
    """Every semantics file to fence, in read order, grouped by the rule it obeys."""
    global_configs, global_attributes, global_ignore = locations
    ignore_pairs = _deduplicated_ignore_paths(
        (
            (root, marker / "info/exclude"),
            *((root, path) for path in worktree_ignore_paths),
            (Path(global_ignore.anchor), global_ignore),
        )
    )
    config_pairs = _external_semantics_paths(
        (*global_configs, *installation.system_config_paths)
    )
    attribute_pairs = _external_semantics_paths(
        (global_attributes, *installation.system_attribute_paths)
    )
    if ignore_pairs is None or config_pairs is None or attribute_pairs is None:
        return None
    return (
        (
            ((root, marker / "info/attributes"),),
            _MAX_PRIVATE_ATTRIBUTES_BYTES,
            _inert_git_attributes,
        ),
        (ignore_pairs, _MAX_PRIVATE_IGNORE_BYTES, _content_unchecked),
        (config_pairs, _MAX_PRIVATE_CONFIG_BYTES, _safe_ignored_git_config),
        (attribute_pairs, _MAX_PRIVATE_ATTRIBUTES_BYTES, _inert_git_attributes),
    )


def _fence_semantics_groups(
    scan: _SemanticsScan,
    groups: Iterable[tuple[Iterable[tuple[Path, Path]], int, Callable[[bytes], bool]]],
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> bool:
    """Fence every group in order; False as soon as one file cannot be trusted."""
    for pairs, limit, inert in groups:
        if not _fence_semantics_files(
            scan, pairs, limit, inert, deadline=deadline, cancelled=cancelled
        ):
            return False
    return True


def _fence_private_config(
    scan: _SemanticsScan,
    root: Path,
    marker: Path,
    hash_name: str,
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> bool:
    """Fence the repository's own config, which must name the expected hash."""
    try:
        config = _read_owned_file(
            root,
            marker / "config",
            _MAX_PRIVATE_CONFIG_BYTES,
            deadline=deadline,
            cancelled=cancelled,
        )
    except _RevisionStopped:
        raise
    except OSError:
        return False
    if config is None or not _safe_private_git_config(
        config.content, hash_name=hash_name
    ):
        return False
    scan.files.append(_SemanticsFileFence(root, config.fence, _MAX_PRIVATE_CONFIG_BYTES))
    return True


def _private_raw_semantics_safe(
    root: Path,
    *,
    hash_name: str,
    installation: _PrivateGitInstallation,
    worktree_ignore_paths: tuple[Path, ...],
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> _RawSemanticsProof | None:
    """The proof that git's raw semantics are the ones this pass assumes."""
    if _private_git_environment_overridden():
        return None
    locations = _global_semantics_locations()
    if locations is None:
        return None
    marker = root / ".git"
    groups = _semantics_groups(
        root, marker, installation, worktree_ignore_paths, locations
    )
    if groups is None:
        return None
    scan = _SemanticsScan()
    if not _fence_semantics_groups(
        scan, groups, deadline=deadline, cancelled=cancelled
    ):
        return None
    if not _fence_private_config(
        scan, root, marker, hash_name, deadline=deadline, cancelled=cancelled
    ):
        return None
    return _RawSemanticsProof(
        tuple(scan.files),
        tuple(scan.absent),
        _raw_semantics_environment(),
        installation,
    )

@dataclass(frozen=True, slots=True)
class _GitIndexHeader:
    """The fixed part of a git index that the entry loop works against."""

    hash_size: int
    count: int
    checksum_offset: int


@dataclass
class _IndexEntryScan:
    """What the entry loop remembers between one index entry and the next."""

    offset: int
    seen_collisions: set[str] = field(default_factory=set)
    previous_path: bytes | None = None
    entries: list[_GitIndexEntry] = field(default_factory=list)


def _git_index_shape(content: bytes | bytearray, hash_size: int) -> tuple[int, int] | None:
    """The entry count and checksum offset, if the header looks like an index."""
    if len(content) < 12 + hash_size or content[:4] != b"DIRC":
        return None
    version, count = struct.unpack_from("!II", content, 4)
    if version not in {2, 3} or count > MAX_REVISION_FILES:
        return None
    return count, len(content) - hash_size


def _git_index_header(
    content: bytes | bytearray,
    *,
    hash_name: str,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> _GitIndexHeader | None:
    """The index's header, once its magic, version and checksum all agree."""
    if hash_name not in {"sha1", "sha256"}:
        return None
    hash_size = hashlib.new(hash_name).digest_size
    shape = _git_index_shape(content, hash_size)
    if shape is None:
        return None
    count, checksum_offset = shape
    digest = _checked_digest(
        hash_name,
        content,
        length=checksum_offset,
        deadline=deadline,
        cancelled=cancelled,
    )
    if digest != content[checksum_offset:]:
        return None
    return _GitIndexHeader(hash_size, count, checksum_offset)


def _index_entry_fields(
    content: bytes | bytearray, entry_offset: int, hash_size: int
) -> tuple[int, bytes, int] | None:
    """The mode, object id and flags of one entry, if they are usable."""
    mode = struct.unpack_from("!I", content, entry_offset + 24)[0]
    oid = bytes(content[entry_offset + 40 : entry_offset + 40 + hash_size])
    flags = struct.unpack_from("!H", content, entry_offset + 40 + hash_size)[0]
    if mode not in {0o100644, 0o100755} or flags & 0xF000 or not any(oid):
        return None
    return mode, oid, flags


def _index_entry_path(
    content: bytes | bytearray,
    entry_offset: int,
    fixed_end: int,
    flags: int,
    checksum_offset: int,
) -> tuple[bytes, int] | None:
    """The entry's raw path and the offset just past its padding."""
    path_end = content.find(
        b"\0",
        fixed_end,
        min(fixed_end + _MAX_PRIVATE_INDEX_PATH_BYTES + 1, checksum_offset),
    )
    if path_end < 0:
        return None
    path_bytes = bytes(content[fixed_end:path_end])
    if flags & 0x0FFF != min(len(path_bytes), 0x0FFF):
        return None
    padded_end = entry_offset + ((path_end + 1 - entry_offset + 7) // 8) * 8
    if padded_end > checksum_offset or any(content[path_end:padded_end]):
        return None
    return path_bytes, padded_end


def _valid_index_path_shape(path: str, pure: PurePosixPath) -> bool:
    """Whether the path is relative and shaped exactly like its posix form."""
    if not path or "\\" in path:
        return False
    return not pure.is_absolute() and pure.as_posix() == path


def _valid_index_path(path: str) -> bool:
    """Whether this index path is one the private reader will accept."""
    pure = PurePosixPath(path)
    if not _valid_index_path_shape(path, pure):
        return False
    if unicodedata.normalize("NFC", path) != path:
        return False
    return not any(part in {"", ".", "..", ".git"} for part in pure.parts)


def _record_index_entry(
    scan: _IndexEntryScan,
    path_bytes: bytes,
    entry_offset: int,
    oid: bytes,
    mode: int,
    padded_end: int,
) -> bool:
    """Accept one entry into the scan; False when it breaks the index's order."""
    try:
        path = path_bytes.decode("utf-8", errors="strict")
    except UnicodeError:
        return False
    if not _valid_index_path(path):
        return False
    collision = path.casefold()
    if collision in scan.seen_collisions:
        return False
    if scan.previous_path is not None and path_bytes <= scan.previous_path:
        return False
    scan.seen_collisions.add(collision)
    scan.previous_path = path_bytes
    scan.entries.append(_GitIndexEntry(path, entry_offset, oid, mode))
    scan.offset = padded_end
    return True


def _parse_index_entry(
    content: bytes | bytearray, header: _GitIndexHeader, scan: _IndexEntryScan
) -> bool:
    """Read one entry into the scan; False when the index is not acceptable."""
    entry_offset = scan.offset
    fixed_end = entry_offset + 40 + header.hash_size + 2
    if fixed_end > header.checksum_offset:
        return False
    fields = _index_entry_fields(content, entry_offset, header.hash_size)
    if fields is None:
        return False
    mode, oid, flags = fields
    located = _index_entry_path(
        content, entry_offset, fixed_end, flags, header.checksum_offset
    )
    if located is None:
        return False
    path_bytes, padded_end = located
    return _record_index_entry(scan, path_bytes, entry_offset, oid, mode, padded_end)


def _known_index_extension(signature: bytes) -> bool:
    """Whether this signature names an optional extension that may be skipped."""
    if signature in _UNSUPPORTED_INDEX_EXTENSIONS:
        return False
    if signature not in _SUPPORTED_INDEX_EXTENSIONS:
        return False
    return signature[:1].isalpha() and not signature[:1].islower()


def _valid_index_extension(
    signature: bytes, extension_end: int, checksum_offset: int, seen: set[bytes]
) -> bool:
    """Whether this trailing extension is one the private reader tolerates."""
    if signature in seen or not _known_index_extension(signature):
        return False
    return signature != b"EOIE" or extension_end == checksum_offset


def _skip_index_extensions(
    content: bytes | bytearray,
    header: _GitIndexHeader,
    offset: int,
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> bool:
    """Whether every trailing extension is acceptable and the index ends exactly."""
    seen: set[bytes] = set()
    while offset < header.checksum_offset:
        _check_stop(deadline, cancelled)
        if offset + 8 > header.checksum_offset:
            return False
        signature = bytes(content[offset : offset + 4])
        extension_end = offset + 8 + struct.unpack_from("!I", content, offset + 4)[0]
        if extension_end > header.checksum_offset:
            return False
        if not _valid_index_extension(
            signature, extension_end, header.checksum_offset, seen
        ):
            return False
        seen.add(signature)
        offset = extension_end
    return offset == header.checksum_offset


def _parse_git_index(
    content: bytes | bytearray,
    *,
    hash_name: str,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> _ParsedGitIndex | None:
    """The parsed index, or None when it is not one this reader will trust."""
    _check_stop(deadline, cancelled)
    header = _git_index_header(
        content, hash_name=hash_name, deadline=deadline, cancelled=cancelled
    )
    if header is None:
        return None
    scan = _IndexEntryScan(offset=12)
    for _index in range(header.count):
        _check_stop(deadline, cancelled)
        if not _parse_index_entry(content, header, scan):
            return None
    if not _skip_index_extensions(
        content, header, scan.offset, deadline=deadline, cancelled=cancelled
    ):
        return None
    return _ParsedGitIndex(
        content, scan.offset, header.checksum_offset, tuple(scan.entries)
    )

def _index_stat_words(info: os.stat_result) -> tuple[int, ...]:
    return (
        (info.st_ctime_ns // 1_000_000_000) & 0xFFFFFFFF,
        (info.st_ctime_ns % 1_000_000_000) & 0xFFFFFFFF,
        (info.st_mtime_ns // 1_000_000_000) & 0xFFFFFFFF,
        (info.st_mtime_ns % 1_000_000_000) & 0xFFFFFFFF,
        info.st_dev & 0xFFFFFFFF,
        info.st_ino & 0xFFFFFFFF,
        info.st_uid & 0xFFFFFFFF,
        info.st_gid & 0xFFFFFFFF,
        info.st_size & 0xFFFFFFFF,
    )


def _refresh_private_index(
    parsed: _ParsedGitIndex,
    hashes: dict[str, _VerificationHash],
    *,
    hash_name: str,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> bytearray:
    hash_size = hashlib.new(hash_name).digest_size
    result = bytearray(parsed.entries_end + hash_size)
    for offset in range(0, parsed.entries_end, _HASH_CHECK_CHUNK_BYTES):
        _check_stop(deadline, cancelled)
        end = min(offset + _HASH_CHECK_CHUNK_BYTES, parsed.entries_end)
        result[offset:end] = parsed.content[offset:end]
    for entry in parsed.entries:
        _check_stop(deadline, cancelled)
        struct.pack_into("!6I", result, entry.offset, *(0,) * 6)
        struct.pack_into("!3I", result, entry.offset + 28, *(0,) * 3)
        value = hashes.get(entry.path)
        if value is None or value.git_oid != entry.oid:
            continue
        current_mode = 0o100755 if value.info.st_mode & 0o111 else 0o100644
        if current_mode != entry.mode:
            continue
        words = _index_stat_words(value.info)
        struct.pack_into("!6I", result, entry.offset, *words[:6])
        struct.pack_into("!3I", result, entry.offset + 28, *words[6:])
    result[parsed.entries_end :] = _checked_digest(
        hash_name,
        result,
        length=parsed.entries_end,
        deadline=deadline,
        cancelled=cancelled,
    )
    return result


def _object_hash_name(expected_head: str | None) -> str | None:
    if expected_head is None:
        return None
    if len(expected_head) == 40:
        return "sha1"
    if len(expected_head) == 64:
        return "sha256"
    return None


def _read_direct_head_fence(
    root: Path,
    expected_head: str,
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> _HeadFence | None:
    marker = root / ".git"
    head_read = _read_owned_file(
        root,
        marker / "HEAD",
        _MAX_HEAD_FENCE_BYTES,
        deadline=deadline,
        cancelled=cancelled,
    )
    if head_read is None:
        return None
    try:
        value = head_read.content.decode("ascii", errors="strict").strip()
    except UnicodeError:
        return None
    reference_fence = None
    if value.startswith("ref: "):
        reference = value[5:]
        pure = PurePosixPath(reference)
        if (
            not reference.startswith("refs/")
            or "\\" in reference
            or pure.is_absolute()
            or any(part in {"", ".", ".."} for part in pure.parts)
        ):
            return None
        reference_read = _read_owned_file(
            root,
            marker.joinpath(*pure.parts),
            _MAX_HEAD_FENCE_BYTES,
            deadline=deadline,
            cancelled=cancelled,
        )
        if reference_read is None:
            return None
        try:
            oid = reference_read.content.decode("ascii", errors="strict").strip()
        except UnicodeError:
            return None
        reference_fence = reference_read.fence
    else:
        oid = value
    if _GIT_COMMIT_RE.fullmatch(oid.encode("ascii")) is None or oid != expected_head:
        return None
    return _HeadFence(oid, head_read.fence, reference_fence)


def _try_private_git_state(
    root: Path,
    expected: WorkspaceRevision,
    hashes: dict[str, _VerificationHash],
    directory_snapshots: dict[Path, _DirectorySnapshot],
    entry_snapshots: dict[Path, _StrongIdentity],
    worktree_ignore_paths: tuple[Path, ...],
    *,
    installation: _PrivateGitInstallation,
    index_path: Path,
    hash_name: str,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> _PrivateGitState | None:
    if expected.git_head is None:
        return None
    index_read = _read_owned_file(
        root,
        index_path,
        _MAX_PRIVATE_INDEX_BYTES,
        deadline=deadline,
        cancelled=cancelled,
    )
    if index_read is None:
        return None
    parsed = _parse_git_index(
        index_read.content,
        hash_name=hash_name,
        deadline=deadline,
        cancelled=cancelled,
    )
    if parsed is None:
        return None
    expected_entries = {entry.path: entry for entry in expected.entries}
    unmatched_count = 0
    unmatched_bytes = 0
    for entry in parsed.entries:
        if entry.path in hashes:
            continue
        unmatched_count += 1
        if unmatched_count > _MAX_PRIVATE_UNMATCHED_TRACKED_FILES:
            return None
        path = root / PurePosixPath(entry.path)
        try:
            info = path.lstat()
        except FileNotFoundError:
            expected_entry = expected_entries.get(entry.path)
            if expected_entry is None or expected_entry.kind != "deleted":
                return None
            continue
        except OSError:
            return None
        if _is_reparse(info) or not stat.S_ISREG(info.st_mode):
            return None
        if entry_snapshots.get(path) != _strong_identity(info):
            return None
        unmatched_bytes += info.st_size
        if unmatched_bytes > _MAX_PRIVATE_UNMATCHED_TRACKED_BYTES:
            return None
    for entry in parsed.entries:
        if PurePosixPath(entry.path).name != ".gitattributes":
            continue
        if not _owned_attributes_are_inert(
            root,
            root / PurePosixPath(entry.path),
            deadline=deadline,
            cancelled=cancelled,
        ):
            return None
    raw_semantics = _private_raw_semantics_safe(
        root,
        hash_name=hash_name,
        installation=installation,
        worktree_ignore_paths=worktree_ignore_paths,
        deadline=deadline,
        cancelled=cancelled,
    )
    if raw_semantics is None:
        return None
    head_fence = _read_direct_head_fence(
        root,
        expected.git_head,
        deadline=deadline,
        cancelled=cancelled,
    )
    if head_fence is None:
        return None
    directory_change_times = []
    for snapshot in directory_snapshots.values():
        _check_stop(deadline, cancelled)
        try:
            current = snapshot.path.lstat()
        except OSError as exc:
            raise PermissionError("workspace revision directory changed before Git state") from exc
        if (
            _identity(current) != snapshot.identity
            or current.st_ctime_ns != snapshot.change_time_ns
        ):
            raise PermissionError("workspace revision directory changed before Git state")
        directory_change_times.append((snapshot, snapshot.change_time_ns))
    private_content = _refresh_private_index(
        parsed,
        hashes,
        hash_name=hash_name,
        deadline=deadline,
        cancelled=cancelled,
    )
    try:
        descriptor = os.memfd_create(
            "llm-wiki-index",
            os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING,
        )
    except OSError:
        return None
    try:
        os.fchmod(descriptor, 0o600)
        written = 0
        private_view = memoryview(private_content)
        try:
            while written < len(private_view):
                _check_stop(deadline, cancelled)
                end = min(written + _PRIVATE_WRITE_CHUNK_BYTES, len(private_view))
                amount = os.write(descriptor, private_view[written:end])
                if amount <= 0:
                    raise OSError("private index write made no progress")
                written += amount
        finally:
            private_view.release()
        os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        fence_ns = max(
            time.time_ns(),
            max((value.info.st_mtime_ns for value in hashes.values()), default=0)
            + 1_000_000_000,
        )
        os.utime(descriptor, ns=(fence_ns, fence_ns))
        if _fcntl is None:
            return None
        required_seals = (
            _fcntl.F_SEAL_WRITE
            | _fcntl.F_SEAL_GROW
            | _fcntl.F_SEAL_SHRINK
            | _fcntl.F_SEAL_SEAL
        )
        try:
            _fcntl.fcntl(descriptor, _fcntl.F_ADD_SEALS, required_seals)
            applied_seals = _fcntl.fcntl(descriptor, _fcntl.F_GET_SEALS)
        except OSError:
            return None
        if applied_seals & required_seals != required_seals:
            return None
        current_head, status = _git_state_with_private_index(
            root,
            descriptor,
            git_executable=installation.executable,
            allow_missing_head=False,
            deadline=deadline,
            cancelled=cancelled,
        )
    finally:
        os.close(descriptor)
    proof = _PrivateGitProof(
        index_read.fence,
        head_fence,
        raw_semantics,
        tuple(directory_change_times),
        tuple((value.snapshot, value.change_time_ns) for value in hashes.values()),
    )
    return _PrivateGitState(current_head, status, proof)


def _validate_private_git_proof(
    proof: _PrivateGitProof,
    *,
    root: Path,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> bool:
    current_index = _read_owned_file(
        root,
        proof.index.path,
        _MAX_PRIVATE_INDEX_BYTES,
        deadline=deadline,
        cancelled=cancelled,
    )
    if current_index is None or current_index.fence != proof.index:
        return False
    current_head = _read_direct_head_fence(
        root,
        proof.head.oid,
        deadline=deadline,
        cancelled=cancelled,
    )
    if current_head != proof.head:
        return False
    try:
        executable_info = proof.raw_semantics.installation.executable.lstat()
    except OSError:
        return False
    if (
        _is_reparse(executable_info)
        or not stat.S_ISREG(executable_info.st_mode)
        or _strong_identity(executable_info) != proof.raw_semantics.installation.identity
    ):
        return False
    if _raw_semantics_environment() != proof.raw_semantics.environment:
        return False
    for semantic_file in proof.raw_semantics.files:
        _check_stop(deadline, cancelled)
        current = _read_owned_file(
            semantic_file.root,
            semantic_file.file.path,
            semantic_file.maximum_bytes,
            deadline=deadline,
            cancelled=cancelled,
        )
        if current is None or current.fence != semantic_file.file:
            return False
    for semantic_root, path in proof.raw_semantics.absent:
        _check_stop(deadline, cancelled)
        if _owned_path_exists_or_is_uncertain(semantic_root, path):
            return False
    for snapshot, change_time_ns in proof.directory_change_times:
        _check_stop(deadline, cancelled)
        try:
            current = snapshot.path.lstat()
        except OSError as exc:
            raise PermissionError("workspace revision directory changed after Git state") from exc
        if (
            _identity(current) != snapshot.identity
            or current.st_ctime_ns != change_time_ns
        ):
            return False
    for snapshot, change_time_ns in proof.file_change_times:
        _check_stop(deadline, cancelled)
        current = _validate_file_identity(snapshot)
        if current.st_ctime_ns != change_time_ns:
            return False
    return True


def compute_workspace_revision(
    repository: RepositoryScope,
    *,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> WorkspaceRevision:
    """Compute a bounded content manifest for one live checkout."""
    _check_stop(deadline, cancelled)
    root = Path(repository.checkout_root)
    resolved_root = root.resolve(strict=True)
    raw_entries: dict[str, tuple[str, Path | None]] = {}
    normalized_inputs: dict[str, str] = {}
    directory_snapshots: dict[Path, _DirectorySnapshot] = {}
    file_snapshots: list[_FileSnapshot] = []
    git_status: bytes | None = None
    inventory_entry_snapshots: dict[Path, _StrongIdentity] = {}
    inventory_prepared_files: dict[str, _FileSnapshot] = {}
    inventory_private_safe = [True]
    inventory_relevant_files: dict[str, Path] = {}

    def add(raw: str, kind: str, path: Path | None) -> None:
        normalized = _normalized_path(raw)
        previous = normalized_inputs.get(normalized)
        if previous is not None and previous != raw:
            raise ValueError("workspace revision contains a Unicode normalization collision")
        normalized_inputs[normalized] = raw
        if len(raw_entries) >= MAX_REVISION_FILES and normalized not in raw_entries:
            raise ValueError("workspace revision exceeds the file-count ceiling")
        if _is_configuration(normalized) and path is not None:
            kind = "configuration"
        raw_entries[normalized] = (kind, path)

    if repository.git_common_dir is not None:
        git_head, git_status = _git_state(
            root,
            allow_missing_head=repository.git_commit is None,
            deadline=deadline,
            cancelled=cancelled,
        )
        for raw, status in _status_paths(git_status):
            _check_stop(deadline, cancelled)
            normalized = _normalized_path(raw)
            path = root / PurePosixPath(normalized)
            if status == "deleted":
                add(raw, "deleted", None)
                continue
            try:
                info = path.lstat()
            except FileNotFoundError:
                add(raw, "deleted", None)
                continue
            unsafe = path.is_symlink() or _is_reparse(info)
            if unsafe:
                linked_directory = path.is_dir()
                if _is_relevant_path(normalized) or linked_directory or stat.S_ISDIR(info.st_mode):
                    raise PermissionError(
                        "workspace revision Git path is a relevant symlink or reparse directory"
                    )
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            try:
                path.resolve(strict=True).relative_to(resolved_root)
            except (OSError, ValueError) as exc:
                raise PermissionError("workspace revision source escapes checkout") from exc
            add(raw, status, path)

    else:
        git_head = None

    for path in _relevant_files(
        root,
        resolved_root=resolved_root,
        directory_snapshots=directory_snapshots,
        entry_snapshots=inventory_entry_snapshots,
        prepared_files=inventory_prepared_files,
        prepared_paths=set(raw_entries),
        private_inventory_safe=inventory_private_safe,
        deadline=deadline,
        cancelled=cancelled,
    ):
        _check_stop(deadline, cancelled)
        raw = path.relative_to(root).as_posix()
        normalized = _normalized_path(raw)
        previous = normalized_inputs.get(normalized)
        if previous is not None and previous != raw:
            raise ValueError("workspace revision contains a Unicode normalization collision")
        inventory_relevant_files[normalized] = path
        if normalized not in raw_entries:
            add(raw, "configuration" if _is_configuration(normalized) else "source", path)
        else:
            kind, existing = raw_entries[normalized]
            if existing is None:
                add(raw, kind, path)

    entries: list[RevisionEntry] = []
    total_bytes = 0
    for relative, (kind, path) in sorted(raw_entries.items()):
        _check_stop(deadline, cancelled)
        if path is None:
            entries.append(RevisionEntry(relative, "deleted", None, 0))
            continue
        sha256, size, snapshot = _hash_file(
            path,
            root=root,
            resolved_root=resolved_root,
            directory_snapshots=directory_snapshots,
            remaining_bytes=MAX_REVISION_BYTES - total_bytes,
            deadline=deadline,
            cancelled=cancelled,
        )
        total_bytes += size
        file_snapshots.append(snapshot)
        entries.append(RevisionEntry(relative, kind, sha256, size))

    if repository.git_common_dir is not None:
        final_head, final_status = _git_state(
            root,
            allow_missing_head=git_head is None,
            deadline=deadline,
            cancelled=cancelled,
        )
        if final_head != git_head:
            raise RuntimeError("Git HEAD changed during workspace revision")
        if final_status != git_status:
            raise RuntimeError("Git status changed during workspace revision")
    for snapshot in directory_snapshots.values():
        _check_stop(deadline, cancelled)
        _validate_directory_snapshot(root, snapshot)
    for snapshot in file_snapshots:
        _check_stop(deadline, cancelled)
        _validate_file_snapshot(root, snapshot)
    for relative, (_kind, path) in raw_entries.items():
        if path is None and os.path.lexists(root / PurePosixPath(relative)):
            raise PermissionError("workspace revision deleted path changed before consistency fence")

    values = {
        "repository_id": repository.repository_id,
        "checkout_id": repository.checkout_id,
        "git_head": git_head,
        "entries": [
            {"path": item.path, "kind": item.kind, "sha256": item.sha256, "size": item.size}
            for item in entries
        ],
    }
    result = WorkspaceRevision(
        repository_id=repository.repository_id,
        checkout_id=repository.checkout_id,
        git_head=git_head,
        entries=tuple(entries),
        revision_sha256=hashlib.sha256(canonical_json_bytes(values)).hexdigest(),
    )
    _publish_inventory_hint(
        _RevisionInventoryHint(
            repository.repository_id,
            repository.checkout_id,
            result.revision_sha256,
            root,
            resolved_root,
            tuple(directory_snapshots.values()),
            tuple(inventory_entry_snapshots.items()),
            tuple(inventory_prepared_files.items()),
            tuple(inventory_relevant_files.items()),
            inventory_private_safe[0],
        )
    )
    return result


def verify_workspace_revision_unchanged(
    repository: RepositoryScope,
    expected: WorkspaceRevision,
    *,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> bool:
    """Prove that a previously computed revision still describes the checkout."""
    return _verify_workspace_revision_unchanged(
        repository,
        expected,
        deadline=deadline,
        cancelled=cancelled,
        allow_private=True,
    )


# The entry kinds a stored workspace revision may name.
_REVISION_ENTRY_KINDS = frozenset(
    {"source", "configuration", "modified", "untracked", "deleted"}
)


def _expected_revision_digest(expected: WorkspaceRevision) -> str:
    """The digest the stored revision's own contents produce."""
    values = {
        "repository_id": expected.repository_id,
        "checkout_id": expected.checkout_id,
        "git_head": expected.git_head,
        "entries": [
            {
                "path": item.path,
                "kind": item.kind,
                "sha256": item.sha256,
                "size": item.size,
            }
            for item in expected.entries
        ],
    }
    return hashlib.sha256(canonical_json_bytes(values)).hexdigest()


def _require_matching_revision(
    repository: RepositoryScope,
    expected: WorkspaceRevision,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> None:
    """Refuse a revision that is not a sound record of this very checkout."""
    if not isinstance(repository, RepositoryScope):
        raise TypeError("repository must be a RepositoryScope")
    if not isinstance(expected, WorkspaceRevision):
        raise TypeError("expected must be a WorkspaceRevision")
    if (expected.repository_id, expected.checkout_id) != (
        repository.repository_id,
        repository.checkout_id,
    ):
        raise ValueError("expected revision belongs to a different checkout")
    _check_stop(deadline, cancelled)
    if len(expected.entries) > MAX_REVISION_FILES:
        raise ValueError("expected revision exceeds the file-count ceiling")
    if _expected_revision_digest(expected) != expected.revision_sha256:
        raise ValueError("expected revision digest is invalid")


def _require_valid_entry_digest(entry: RevisionEntry) -> None:
    """A deleted entry carries nothing; every other one carries a real digest."""
    if entry.kind == "deleted":
        if entry.sha256 is not None or entry.size != 0:
            raise ValueError("expected deleted revision entry is invalid")
        return
    if (
        not isinstance(entry.sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", entry.sha256) is None
    ):
        raise ValueError("expected revision entry digest is invalid")


def _require_revision_entry(entry: object) -> None:
    """Refuse anything that is not a revision entry at all."""
    if not isinstance(entry, RevisionEntry):
        raise TypeError("expected revision entries must be RevisionEntry values")


def _require_valid_entry(entry: RevisionEntry) -> None:
    """Refuse a stored entry whose fields are not a sound revision record."""
    if entry.kind not in _REVISION_ENTRY_KINDS:
        raise ValueError("expected revision entry kind is invalid")
    if isinstance(entry.size, bool) or not isinstance(entry.size, int) or entry.size < 0:
        raise ValueError("expected revision entry size is invalid")
    _require_valid_entry_digest(entry)


def _validated_expected_entries(
    expected: WorkspaceRevision,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> dict[str, RevisionEntry]:
    """The stored entries, keyed by normalized path, each one checked."""
    entries: dict[str, RevisionEntry] = {}
    for entry in expected.entries:
        _check_stop(deadline, cancelled)
        _require_revision_entry(entry)
        normalized = _normalized_path(entry.path)
        if normalized in entries:
            raise ValueError("expected revision contains duplicate normalized paths")
        _require_valid_entry(entry)
        entries[normalized] = entry
    return entries


@dataclass
class _PrivateIndexPlan:
    """The private-index fast path for one verification, while it stays usable."""

    installation: _PrivateGitInstallation | None
    index_path: Path | None
    hash_name: str | None = None
    entry_snapshots: dict[Path, _StrongIdentity] | None = None
    prepared_files: dict[str, _FileSnapshot] | None = None
    inventory_safe: list[bool] | None = None

    def disable(self) -> None:
        """Give up the fast path; the rest of the pass hashes the files itself."""
        self.hash_name = None
        self.entry_snapshots = None
        self.prepared_files = None
        self.inventory_safe = None


def _private_index_installation(
    repository: RepositoryScope, *, allow_private: bool
) -> _PrivateGitInstallation | None:
    """The private git installation this verification may use, if any."""
    if not allow_private or repository.git_common_dir is None:
        return None
    if not _private_index_platform_supported():
        return None
    return _private_git_installation()


def _candidate_index_size(candidate: Path | None) -> int | None:
    """The candidate index's size, or None when there is nothing to measure."""
    if candidate is None:
        return None
    try:
        return candidate.lstat().st_size
    except OSError:
        return None


def _arm_private_index(
    plan: _PrivateIndexPlan, expected: WorkspaceRevision, size: int
) -> None:
    """Arm the fast path when the index is small enough and the head is hashable."""
    if size > _MAX_PRIVATE_INDEX_BYTES:
        return
    hash_name = _object_hash_name(expected.git_head)
    if hash_name is None:
        return
    plan.hash_name = hash_name
    plan.entry_snapshots = {}
    plan.prepared_files = {}
    plan.inventory_safe = [True]


def _private_index_plan(
    repository: RepositoryScope,
    root: Path,
    expected: WorkspaceRevision,
    *,
    allow_private: bool,
) -> _PrivateIndexPlan:
    """The private-index fast path this verification may use, if any."""
    installation = _private_index_installation(repository, allow_private=allow_private)
    candidate = _ordinary_index_path(root) if installation is not None else None
    size = _candidate_index_size(candidate)
    if size is None:
        return _PrivateIndexPlan(installation, None)
    plan = _PrivateIndexPlan(installation, candidate)
    _arm_private_index(plan, expected, size)
    return plan


def _prepared_files_named(plan: _PrivateIndexPlan, name: str) -> tuple[Path, ...]:
    """The prepared files whose base name is exactly this one."""
    if plan.prepared_files is None:
        return ()
    return tuple(
        snapshot.path
        for path, snapshot in plan.prepared_files.items()
        if PurePosixPath(path).name == name
    )


def _prepared_attributes_inert(
    plan: _PrivateIndexPlan,
    root: Path,
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> bool:
    """Whether every prepared `.gitattributes` leaves the fast path's rules intact."""
    return all(
        _owned_attributes_are_inert(root, path, deadline=deadline, cancelled=cancelled)
        for path in _prepared_files_named(plan, ".gitattributes")
    )


@dataclass(frozen=True)
class _EntryHashContext:
    """The fixed inputs every entry in one verification pass is hashed against."""

    root: Path
    resolved_root: Path
    plan: _PrivateIndexPlan
    directory_snapshots: dict[Path, _DirectorySnapshot]
    current_relevant_paths: dict[str, Path]
    deadline: float | None
    cancelled: Callable[[], bool] | None
    inventory_hint_used: bool
    allow_private: bool


@dataclass
class _HashedEntries:
    """What the per-entry pass gathered, while every entry still matched."""

    file_snapshots: list[_FileSnapshot] = field(default_factory=list)
    verification_hashes: dict[str, _VerificationHash] = field(default_factory=dict)
    total_bytes: int = 0


def _refuse_relevant_link(path: Path, relative: str, info: os.stat_result) -> None:
    """A link or reparse point where it matters is a refusal, not a change."""
    if _is_relevant_path(relative) or path.is_dir() or stat.S_ISDIR(info.st_mode):
        raise PermissionError(
            "workspace revision expected path is a relevant symlink or reparse directory"
        )


def _regular_entry_stat(path: Path, relative: str) -> os.stat_result | None:
    """The entry's own metadata, or None when it is no longer a plain file."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if path.is_symlink() or _is_reparse(info):
        _refuse_relevant_link(path, relative, info)
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    return info


def _hash_direct_entry(
    relative: str, context: _EntryHashContext, remaining: int
) -> tuple[str, int, _FileSnapshot] | None:
    """Hash one entry straight from the working tree."""
    path = context.current_relevant_paths.get(
        relative, context.root / PurePosixPath(relative)
    )
    if _regular_entry_stat(path, relative) is None:
        return None
    return _hash_file(
        path,
        root=context.root,
        resolved_root=context.resolved_root,
        directory_snapshots=context.directory_snapshots,
        remaining_bytes=remaining,
        deadline=context.deadline,
        cancelled=context.cancelled,
    )


def _hash_prepared_entry(
    relative: str, entry: RevisionEntry, context: _EntryHashContext, remaining: int
) -> _VerificationHash | None:
    """Hash one entry through the private index the pass prepared."""
    plan = context.plan
    if plan.prepared_files is None:
        raise AssertionError("private-index file snapshots are unavailable")
    prepared = plan.prepared_files.get(relative)
    if prepared is None or prepared.identity[3] != entry.size:
        return None
    try:
        return _hash_file_for_verification(
            prepared.path,
            root=context.root,
            resolved_root=context.resolved_root,
            directory_snapshots=context.directory_snapshots,
            remaining_bytes=remaining,
            deadline=context.deadline,
            cancelled=context.cancelled,
            git_hash_name=plan.hash_name,
            snapshot=prepared,
            validate_parents=False,
        )
    except PermissionError:
        if context.inventory_hint_used:
            raise _RestartVerification(context.allow_private) from None
        raise


def _entry_hash(
    relative: str,
    entry: RevisionEntry,
    context: _EntryHashContext,
    gathered: _HashedEntries,
) -> tuple[str, int, _FileSnapshot] | None:
    """The digest, size and snapshot for one entry, by whichever path applies."""
    remaining = MAX_REVISION_BYTES - gathered.total_bytes
    if context.plan.hash_name is None:
        return _hash_direct_entry(relative, context, remaining)
    verification = _hash_prepared_entry(relative, entry, context, remaining)
    if verification is None:
        return None
    gathered.verification_hashes[relative] = verification
    return verification.sha256, verification.size, verification.snapshot


def _entry_still_matches(
    relative: str,
    entry: RevisionEntry,
    context: _EntryHashContext,
    gathered: _HashedEntries,
) -> bool:
    """Whether this entry still matches; its hash joins the pass when it does."""
    if entry.kind == "deleted":
        return not os.path.lexists(context.root / PurePosixPath(relative))
    hashed = _entry_hash(relative, entry, context, gathered)
    if hashed is None:
        return False
    sha256, size, snapshot = hashed
    gathered.total_bytes += size
    gathered.file_snapshots.append(snapshot)
    return size == entry.size and sha256 == entry.sha256


def _hash_expected_entries(
    entries: Mapping[str, RevisionEntry], context: _EntryHashContext
) -> _HashedEntries | None:
    """Hash every expected entry; None as soon as one no longer matches."""
    gathered = _HashedEntries()
    for relative, entry in sorted(entries.items()):
        _check_stop(context.deadline, context.cancelled)
        if not _entry_still_matches(relative, entry, context, gathered):
            return None
    return gathered


def _private_git_state(
    root: Path,
    expected: WorkspaceRevision,
    plan: _PrivateIndexPlan,
    verification_hashes: dict[str, _VerificationHash],
    directory_snapshots: dict[Path, _DirectorySnapshot],
    worktree_ignore_paths: tuple[Path, ...],
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> _PrivateGitState | None:
    """The git state read through the private index, or None if it was not armed.

    Once the fast path is armed it either produces a state or restarts the
    verification without it; it never falls back to the ordinary reader here.
    """
    if plan.hash_name is None:
        return None
    if plan.index_path is None:
        raise AssertionError("private-index path is unavailable")
    if plan.installation is None or plan.entry_snapshots is None:
        raise AssertionError("private-index installation proof is unavailable")
    try:
        state = _try_private_git_state(
            root,
            expected,
            verification_hashes,
            directory_snapshots,
            plan.entry_snapshots,
            worktree_ignore_paths,
            installation=plan.installation,
            index_path=plan.index_path,
            hash_name=plan.hash_name,
            deadline=deadline,
            cancelled=cancelled,
        )
    except _RevisionStopped:
        raise
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError):
        raise _RestartVerification(allow_private=False) from None
    if state is None:
        raise _RestartVerification(allow_private=False) from None
    return state


def _revision_git_state(
    root: Path,
    expected: WorkspaceRevision,
    private_state: _PrivateGitState | None,
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> tuple[str | None, object]:
    """The head and status this pass compares against, private or ordinary."""
    if private_state is not None:
        return private_state.head, private_state.status
    return _git_state(
        root,
        allow_missing_head=expected.git_head is None,
        deadline=deadline,
        cancelled=cancelled,
    )


def _revision_state_matches(
    current_head: str | None,
    git_state: object,
    expected: WorkspaceRevision,
    entries: Mapping[str, RevisionEntry],
    private_state: _PrivateGitState | None,
) -> bool:
    """Whether git agrees; a private-path failure restarts instead of raising."""
    try:
        return _git_state_matches_revision(current_head, git_state, expected, entries)
    except _RevisionStopped:
        raise
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError):
        if private_state is None:
            raise
        raise _RestartVerification(allow_private=False) from None


def _validate_pass_snapshots(
    root: Path,
    directory_snapshots: dict[Path, _DirectorySnapshot],
    file_snapshots: list[_FileSnapshot],
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> None:
    """Re-check every directory and file snapshot the pass took."""
    for directory in directory_snapshots.values():
        _check_stop(deadline, cancelled)
        _validate_directory_snapshot(root, directory)
    for snapshot in file_snapshots:
        _check_stop(deadline, cancelled)
        _validate_file_snapshot(root, snapshot)


def _tree_entry_unchanged(
    path: Path,
    identity: _StrongIdentity,
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> bool:
    """Whether this tree entry still has exactly the identity it was recorded with."""
    _check_stop(deadline, cancelled)
    try:
        current = path.lstat()
    except OSError as exc:
        raise PermissionError("workspace revision tree entry changed") from exc
    return _strong_identity(current) == identity


def _tree_entries_unchanged(
    plan: _PrivateIndexPlan,
    file_snapshots: list[_FileSnapshot],
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> bool:
    """Whether every prepared tree entry the pass did not hash is still identical."""
    if plan.entry_snapshots is None:
        return True
    hashed_paths = {snapshot.path for snapshot in file_snapshots}
    return all(
        _tree_entry_unchanged(path, identity, deadline=deadline, cancelled=cancelled)
        for path, identity in plan.entry_snapshots.items()
        if path not in hashed_paths
    )


def _deleted_entries_absent(
    root: Path, entries: Mapping[str, RevisionEntry]
) -> bool:
    """Whether every entry the revision records as deleted is still absent."""
    return not any(
        entry.kind == "deleted" and os.path.lexists(root / PurePosixPath(path))
        for path, entry in entries.items()
    )


def _require_private_proof(
    private_state: _PrivateGitState | None,
    root: Path,
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> None:
    """A private pass stands only while its own proof still validates."""
    if private_state is None:
        return
    try:
        proof_valid = _validate_private_git_proof(
            private_state.proof, root=root, deadline=deadline, cancelled=cancelled
        )
    except _RevisionStopped:
        raise
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError):
        proof_valid = False
    if not proof_valid:
        raise _RestartVerification(allow_private=False) from None


class _RestartVerification(Exception):
    """Raised when a verification pass has to be redone without a fast path."""

    def __init__(self, allow_private: bool) -> None:
        super().__init__("restart workspace revision verification")
        self.allow_private = allow_private


def _verify_workspace_revision_unchanged(
    repository: RepositoryScope,
    expected: WorkspaceRevision,
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
    allow_private: bool,
    allow_inventory_hint: bool = True,
) -> bool:
    """Whether the checkout still matches the stored revision, exactly."""
    try:
        return _verify_revision_pass(
            repository,
            expected,
            deadline=deadline,
            cancelled=cancelled,
            allow_private=allow_private,
            allow_inventory_hint=allow_inventory_hint,
        )
    except _RestartVerification as restart:
        return _verify_workspace_revision_unchanged(
            repository,
            expected,
            deadline=deadline,
            cancelled=cancelled,
            allow_private=restart.allow_private,
            allow_inventory_hint=False,
        )


@dataclass
class _CurrentState:
    """What a verification pass found in the checkout, and how it found it."""

    relevant: set[str] = field(default_factory=set)
    paths: dict[str, Path] = field(default_factory=dict)
    from_hint: bool = False


def _restore_matching_hint(
    repository: RepositoryScope,
    expected: WorkspaceRevision,
    plan: _PrivateIndexPlan,
    state: _CurrentState,
    *,
    root: Path,
    resolved_root: Path,
    directory_snapshots: dict[Path, _DirectorySnapshot],
    allow_inventory_hint: bool,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> bool:
    """Whether a stored inventory hint supplied this pass's current state."""
    if not allow_inventory_hint:
        return False
    hint = _matching_inventory_hint(
        repository, expected, root=root, resolved_root=resolved_root
    )
    if hint is None:
        return False
    return _restore_inventory_hint(
        hint,
        root=root,
        directory_snapshots=directory_snapshots,
        entry_snapshots=plan.entry_snapshots,
        prepared_files=plan.prepared_files,
        private_inventory_safe=plan.inventory_safe,
        current_relevant=state.relevant,
        current_relevant_paths=state.paths,
        deadline=deadline,
        cancelled=cancelled,
    )


def _walk_relevant_files(
    root: Path,
    resolved_root: Path,
    entries: Mapping[str, RevisionEntry],
    plan: _PrivateIndexPlan,
    state: _CurrentState,
    *,
    directory_snapshots: dict[Path, _DirectorySnapshot],
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> None:
    """Walk the checkout for what it holds now, keyed by normalized path."""
    for current_path in _relevant_files(
        root,
        resolved_root=resolved_root,
        directory_snapshots=directory_snapshots,
        entry_snapshots=plan.entry_snapshots,
        prepared_files=plan.prepared_files,
        prepared_paths=set(entries),
        private_inventory_safe=plan.inventory_safe,
        relevant_paths=state.relevant,
        deadline=deadline,
        cancelled=cancelled,
    ):
        normalized = _normalized_path(current_path.relative_to(root).as_posix())
        if normalized in state.paths:
            raise ValueError(
                "workspace revision contains a Unicode normalization collision"
            )
        state.paths[normalized] = current_path


def _current_checkout_state(
    repository: RepositoryScope,
    expected: WorkspaceRevision,
    entries: Mapping[str, RevisionEntry],
    plan: _PrivateIndexPlan,
    *,
    root: Path,
    resolved_root: Path,
    directory_snapshots: dict[Path, _DirectorySnapshot],
    allow_inventory_hint: bool,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> _CurrentState:
    """What the checkout currently holds, from a stored hint or a fresh walk."""
    state = _CurrentState()
    state.from_hint = _restore_matching_hint(
        repository,
        expected,
        plan,
        state,
        root=root,
        resolved_root=resolved_root,
        directory_snapshots=directory_snapshots,
        allow_inventory_hint=allow_inventory_hint,
        deadline=deadline,
        cancelled=cancelled,
    )
    if not state.from_hint:
        _walk_relevant_files(
            root,
            resolved_root,
            entries,
            plan,
            state,
            directory_snapshots=directory_snapshots,
            deadline=deadline,
            cancelled=cancelled,
        )
    return state


def _settle_private_plan(
    plan: _PrivateIndexPlan,
    root: Path,
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> None:
    """Give up the fast path if the prepared tree broke the rules it relies on."""
    if not _prepared_attributes_inert(
        plan, root, deadline=deadline, cancelled=cancelled
    ):
        plan.disable()
        return
    if plan.inventory_safe is not None and not plan.inventory_safe[0]:
        plan.disable()


def _expected_relevant_paths(entries: Mapping[str, RevisionEntry]) -> set[str]:
    """The stored paths a walk of the current checkout is expected to find."""
    return {
        path
        for path, entry in entries.items()
        if entry.kind != "deleted" and _is_relevant_path(path)
    }


def _verified_after_git(
    entries: Mapping[str, RevisionEntry],
    plan: _PrivateIndexPlan,
    gathered: _HashedEntries,
    private_state: _PrivateGitState | None,
    *,
    root: Path,
    directory_snapshots: dict[Path, _DirectorySnapshot],
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> bool:
    """Re-check the snapshots, the recorded deletions, and the private proof."""
    if private_state is None:
        _validate_pass_snapshots(
            root,
            directory_snapshots,
            gathered.file_snapshots,
            deadline=deadline,
            cancelled=cancelled,
        )
    elif not _tree_entries_unchanged(
        plan, gathered.file_snapshots, deadline=deadline, cancelled=cancelled
    ):
        return False
    if not _deleted_entries_absent(root, entries):
        return False
    _require_private_proof(private_state, root, deadline=deadline, cancelled=cancelled)
    _check_stop(deadline, cancelled)
    return True


def _verified_against_git(
    repository: RepositoryScope,
    expected: WorkspaceRevision,
    entries: Mapping[str, RevisionEntry],
    plan: _PrivateIndexPlan,
    gathered: _HashedEntries,
    worktree_ignore_paths: tuple[Path, ...],
    *,
    root: Path,
    directory_snapshots: dict[Path, _DirectorySnapshot],
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> bool:
    """The second half of a pass: git agreement, then everything after it."""
    private_state = None
    if repository.git_common_dir is not None:
        private_state = _private_git_state(
            root,
            expected,
            plan,
            gathered.verification_hashes,
            directory_snapshots,
            worktree_ignore_paths,
            deadline=deadline,
            cancelled=cancelled,
        )
        current_head, git_state = _revision_git_state(
            root, expected, private_state, deadline=deadline, cancelled=cancelled
        )
        if not _revision_state_matches(
            current_head, git_state, expected, entries, private_state
        ):
            return False
    elif expected.git_head is not None:
        return False
    return _verified_after_git(
        entries,
        plan,
        gathered,
        private_state,
        root=root,
        directory_snapshots=directory_snapshots,
        deadline=deadline,
        cancelled=cancelled,
    )


def _verify_revision_pass(
    repository: RepositoryScope,
    expected: WorkspaceRevision,
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
    allow_private: bool,
    allow_inventory_hint: bool,
) -> bool:
    """One verification attempt, with whatever fast paths it was allowed."""
    _require_matching_revision(repository, expected, deadline, cancelled)
    root = Path(repository.checkout_root)
    resolved_root = root.resolve(strict=True)
    entries = _validated_expected_entries(expected, deadline, cancelled)
    directory_snapshots: dict[Path, _DirectorySnapshot] = {}
    plan = _private_index_plan(repository, root, expected, allow_private=allow_private)
    state = _current_checkout_state(
        repository,
        expected,
        entries,
        plan,
        root=root,
        resolved_root=resolved_root,
        directory_snapshots=directory_snapshots,
        allow_inventory_hint=allow_inventory_hint,
        deadline=deadline,
        cancelled=cancelled,
    )
    worktree_ignore_paths = _prepared_files_named(plan, ".gitignore")
    _settle_private_plan(plan, root, deadline=deadline, cancelled=cancelled)
    if state.relevant != _expected_relevant_paths(entries):
        return False
    gathered = _hash_expected_entries(
        entries,
        _EntryHashContext(
            root=root,
            resolved_root=resolved_root,
            plan=plan,
            directory_snapshots=directory_snapshots,
            current_relevant_paths=state.paths,
            deadline=deadline,
            cancelled=cancelled,
            inventory_hint_used=state.from_hint,
            allow_private=allow_private,
        ),
    )
    if gathered is None:
        return False
    return _verified_against_git(
        repository,
        expected,
        entries,
        plan,
        gathered,
        worktree_ignore_paths,
        root=root,
        directory_snapshots=directory_snapshots,
        deadline=deadline,
        cancelled=cancelled,
    )

def diff_workspace_revisions(
    before: WorkspaceRevision, after: WorkspaceRevision
) -> WorkspaceDelta:
    """Return a deterministic delta with only unambiguous content renames paired."""
    if (before.repository_id, before.checkout_id) != (
        after.repository_id,
        after.checkout_id,
    ):
        raise ValueError("workspace revisions must describe the same checkout")
    before_entries = {entry.path: entry for entry in before.entries if entry.sha256 is not None}
    after_entries = {entry.path: entry for entry in after.entries if entry.sha256 is not None}
    created = set(after_entries) - set(before_entries)
    deleted = set(before_entries) - set(after_entries)
    changed = {
        path
        for path in set(before_entries) & set(after_entries)
        if before_entries[path].sha256 != after_entries[path].sha256
    }
    configuration_changed = any(
        _is_configuration(path) for path in created | changed | deleted
    )
    renames: list[tuple[str, str]] = []
    deleted_by_hash: dict[str, list[str]] = {}
    created_by_hash: dict[str, list[str]] = {}
    for path in deleted:
        deleted_by_hash.setdefault(str(before_entries[path].sha256), []).append(path)
    for path in created:
        created_by_hash.setdefault(str(after_entries[path].sha256), []).append(path)
    for digest in sorted(set(deleted_by_hash) & set(created_by_hash)):
        old = deleted_by_hash[digest]
        new = created_by_hash[digest]
        if len(old) == len(new) == 1:
            renames.append((old[0], new[0]))
            configuration_changed = configuration_changed or any(
                _is_configuration(path) for path in (old[0], new[0])
            )
            deleted.remove(old[0])
            created.remove(new[0])
    return WorkspaceDelta(
        created=tuple(sorted(created)),
        changed=tuple(sorted(changed)),
        renamed=tuple(sorted(renames)),
        deleted=tuple(sorted(deleted)),
        configuration_changed=configuration_changed,
    )
